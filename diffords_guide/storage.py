"""
Difford's Guide SQLite 儲存層

Schema 設計說明
--------------
cocktails              — 雞尾酒主表（以 Difford's 原始 ID 為 PK）
cocktail_ingredients   — 食材正規化表（1:N，含通用名/品牌名雙欄）
diffords_scrape_runs   — 爬取執行記錄

增量更新設計
-----------
- cocktails.lastmod 儲存 sitemap 的 <lastmod> 日期
- 爬取前比對 sitemap lastmod vs DB lastmod：相同則跳過
- cocktail_ingredients 採「先刪後插」策略，確保食材順序與份量總是與最新爬取結果一致
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS cocktails (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT,
    description     TEXT,
    glassware       TEXT,
    garnish         TEXT,
    prepare         TEXT,
    instructions    TEXT,
    review          TEXT,
    history         TEXT,
    tags            TEXT,
    rating_value    REAL,
    rating_count    INTEGER,
    calories        INTEGER,
    prep_time_min   INTEGER,
    abv             REAL,
    strength        INTEGER,
    sweet_sour      INTEGER,
    date_published  DATE,
    url             TEXT UNIQUE NOT NULL,
    lastmod         DATE,
    scraped_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cocktail_ingredients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cocktail_id  INTEGER NOT NULL REFERENCES cocktails(id) ON DELETE CASCADE,
    sort_order   INTEGER NOT NULL,
    item         TEXT NOT NULL,
    amount       TEXT,
    item_generic TEXT
);

CREATE TABLE IF NOT EXISTS diffords_scrape_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    total_scraped  INTEGER DEFAULT 0,
    total_skipped  INTEGER DEFAULT 0,
    total_failed   INTEGER DEFAULT 0,
    mode           TEXT,
    status         TEXT DEFAULT 'running'
);

CREATE INDEX IF NOT EXISTS idx_cocktails_name    ON cocktails(name);
CREATE INDEX IF NOT EXISTS idx_cocktails_rating  ON cocktails(rating_value);
CREATE INDEX IF NOT EXISTS idx_ci_cocktail       ON cocktail_ingredients(cocktail_id);
CREATE INDEX IF NOT EXISTS idx_ci_item_generic   ON cocktail_ingredients(item_generic);
"""

# 排序鍵白名單。這是唯一會把字串拼進 SQL 的地方，不可改成動態欄位名。
_SORT_COLUMNS = {
    "rating": "c.rating_value",
    "abv": "c.abv",
    "sweet_sour": "c.sweet_sour",
    "calories": "c.calories",
    "date": "c.date_published",
    "name": "c.name",
    "count": "c.rating_count",
}
SORT_KEYS = tuple(_SORT_COLUMNS)


# 欄位轉型輔助
def _to_real(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _to_int(value) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _to_text(value) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip() or None


class DiffordsStorage:
    """Difford's Guide 雞尾酒資料的 SQLite 儲存後端。"""

    def __init__(self, db_path: str = "diffords.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    # 後來才加的欄位。CREATE TABLE IF NOT EXISTS 不會改動既有的表，
    # 所以現存的 DB（GCS 上那顆）要靠 ALTER TABLE 補上。
    _ADDED_COLUMNS = (
        ("strength", "INTEGER"),
        ("sweet_sour", "INTEGER"),
    )

    def _init_schema(self):
        self.conn.executescript(_DDL)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """把新欄位補進既有的 cocktails 表（冪等）。"""
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(cocktails)")}
        for column, decl in self._ADDED_COLUMNS:
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE cocktails ADD COLUMN {column} {decl}"
                )
                logger.info("已新增欄位 cocktails.%s", column)

    # ------------------------------------------------------------------
    # 查詢輔助（供爬蟲決策）
    # ------------------------------------------------------------------

    def get_existing_urls(self) -> set[str]:
        """取得所有已爬 URL（O(1) 查詢用 Set）。"""
        rows = self.conn.execute("SELECT url FROM cocktails").fetchall()
        return {r[0] for r in rows}

    def get_url_lastmod_map(self) -> dict[str, str]:
        """取得 {url: lastmod} 映射，用於增量更新比對。"""
        rows = self.conn.execute(
            "SELECT url, lastmod FROM cocktails WHERE lastmod IS NOT NULL"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------
    # 儲存
    # ------------------------------------------------------------------

    def save_cocktail(self, data: dict[str, Any]) -> bool:
        """儲存單筆雞尾酒（upsert），回傳是否成功。"""
        try:
            with self.conn:
                cur = self.conn.cursor()
                cocktail_id = self._upsert_cocktail(cur, data)
                self._save_ingredients(cur, cocktail_id, data)
            return True
        except Exception as e:
            logger.error("DiffordsStorage.save_cocktail 失敗: %s", e)
            return False

    def _upsert_cocktail(self, cur: sqlite3.Cursor, data: dict[str, Any]) -> int:
        """寫入單筆酒譜。

        HTML 來源的欄位用 COALESCE 保護：抓不到（None）時保留既有值，不清空。
        2026-08 網站改版移除了 prepare/history 區塊，若直接覆寫，一次 full
        scrape 就會清掉 4,645 筆改版前抓到的 prepare。JSON-LD 來源的欄位
        （description/tags/rating…）穩定有值，維持直接覆寫以便更新。
        """
        row = self._prepare_row(data)
        existing = cur.execute(
            "SELECT id FROM cocktails WHERE id = ?", (row["id"],)
        ).fetchone()

        if existing:
            cocktail_id = existing[0]
            cur.execute(
                """
                UPDATE cocktails SET
                    name=:name, slug=:slug, description=:description,
                    glassware=COALESCE(:glassware, glassware),
                    garnish=COALESCE(:garnish, garnish),
                    prepare=COALESCE(:prepare, prepare),
                    instructions=COALESCE(:instructions, instructions),
                    review=COALESCE(:review, review),
                    history=COALESCE(:history, history),
                    abv=COALESCE(:abv, abv),
                    strength=COALESCE(:strength, strength),
                    sweet_sour=COALESCE(:sweet_sour, sweet_sour),
                    tags=:tags, rating_value=:rating_value, rating_count=:rating_count,
                    calories=:calories, prep_time_min=:prep_time_min,
                    date_published=:date_published, url=:url, lastmod=:lastmod,
                    scraped_at=CURRENT_TIMESTAMP
                WHERE id=:id
            """,
                row,
            )
        else:
            cur.execute(
                """
                INSERT INTO cocktails
                    (id, name, slug, description, glassware, garnish, prepare,
                     instructions, review, history, tags, rating_value, rating_count,
                     calories, prep_time_min, abv, strength, sweet_sour,
                     date_published, url, lastmod)
                VALUES
                    (:id, :name, :slug, :description, :glassware, :garnish, :prepare,
                     :instructions, :review, :history, :tags, :rating_value, :rating_count,
                     :calories, :prep_time_min, :abv, :strength, :sweet_sour,
                     :date_published, :url, :lastmod)
            """,
                row,
            )
            cocktail_id = cur.lastrowid
            if cocktail_id is None:
                raise ValueError("Failed to insert cocktail row")
        return cocktail_id

    def _save_ingredients(
        self, cur: sqlite3.Cursor, cocktail_id: int, data: dict[str, Any]
    ):
        """先刪後插，確保與最新爬取結果一致。"""
        cur.execute(
            "DELETE FROM cocktail_ingredients WHERE cocktail_id = ?", (cocktail_id,)
        )
        # HTML 食材（品牌名）為主，JSON-LD 通用名作補充
        html_ings = data.get("ingredients_html") or []
        gen_ings = data.get("ingredients_generic") or []
        items = html_ings if html_ings else gen_ings
        generic_map = {ing["sort_order"]: ing["item"] for ing in gen_ings}

        cur.executemany(
            """
            INSERT INTO cocktail_ingredients
                (cocktail_id, sort_order, item, amount, item_generic)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    cocktail_id,
                    ing["sort_order"],
                    ing.get("item", ""),
                    ing.get("amount", ""),
                    generic_map.get(ing["sort_order"]) if html_ings else None,
                )
                for ing in items
            ],
        )

    def _prepare_row(self, data: dict[str, Any]) -> dict[str, Any]:
        url = data.get("url", "")
        parts = url.rstrip("/").split("/")
        # URL 格式：/cocktails/recipe/{id}/{slug}
        raw_id = parts[-2] if len(parts) >= 2 else None
        cocktail_id = int(raw_id) if raw_id and raw_id.isdigit() else None
        slug = parts[-1] if parts else None
        tags = data.get("tags")
        return {
            "id": cocktail_id,
            "name": _to_text(data.get("name")) or "",
            "slug": _to_text(slug),
            "description": _to_text(data.get("description")),
            "glassware": _to_text(data.get("glassware")),
            "garnish": _to_text(data.get("garnish")),
            "prepare": _to_text(data.get("prepare")),
            "instructions": _to_text(data.get("instructions")),
            "review": _to_text(data.get("review")),
            "history": _to_text(data.get("history")),
            "tags": json.dumps(tags, ensure_ascii=False) if tags else None,
            "rating_value": _to_real(data.get("rating_value")),
            "rating_count": _to_int(data.get("rating_count")),
            "calories": _to_int(data.get("calories")),
            "prep_time_min": _to_int(data.get("prep_time_minutes")),
            "abv": _to_real(data.get("abv")),
            "strength": _to_int(data.get("strength")),
            "sweet_sour": _to_int(data.get("sweet_sour")),
            "date_published": _to_text(data.get("date_published")),
            "url": url,
            "lastmod": _to_text(data.get("lastmod")),
        }

    # ------------------------------------------------------------------
    # 爬取紀錄
    # ------------------------------------------------------------------

    def record_scrape_run(self, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO diffords_scrape_runs (started_at, mode) VALUES (?, ?)",
            (datetime.now().isoformat(), mode),
        )
        self.conn.commit()
        if cur.lastrowid is None:
            raise ValueError("Failed to insert scrape run")
        return cur.lastrowid

    def finish_scrape_run(
        self, run_id: int, scraped: int, skipped: int, failed: int, status: str
    ):
        self.conn.execute(
            """
            UPDATE diffords_scrape_runs
            SET finished_at=?, total_scraped=?, total_skipped=?, total_failed=?, status=?
            WHERE id=?
            """,
            (datetime.now().isoformat(), scraped, skipped, failed, status, run_id),
        )
        self.conn.commit()

    def get_last_successful_run(self) -> Optional[str]:
        """取得最近一次成功執行的 started_at 時間戳。"""
        row = self.conn.execute("""
            SELECT started_at FROM diffords_scrape_runs
            WHERE status IN ('completed', 'completed_with_errors')
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # 查詢（供 bot.py 使用）
    # ------------------------------------------------------------------

    def query_cocktails(
        self,
        *,
        keyword: Optional[str] = None,
        description: Optional[str] = None,
        ingredient: Optional[str] = None,
        tag: Optional[str] = None,
        min_rating: Optional[float] = None,
        max_rating: Optional[float] = None,
        min_abv: Optional[float] = None,
        max_abv: Optional[float] = None,
        min_count: Optional[int] = None,
        min_sweet_sour: Optional[int] = None,
        max_sweet_sour: Optional[int] = None,
        sort: str = "rating",
        desc: bool = True,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """組合式查詢：所有條件皆可疊加，None 者不進 WHERE。

        min_count 預設 None（不設票數門檻）。設成 5 會讓約 994 筆低票數酒譜
        從材料／標籤／ABV 查詢中無聲消失，票數門檻屬於「高分精選」語意，
        應由呼叫端明確傳入。
        """
        if sort not in _SORT_COLUMNS:
            raise ValueError(
                f"不支援的排序鍵：{sort}（可用：{', '.join(SORT_KEYS)}）"
            )

        where: list[str] = []
        params: list[Any] = []

        # ponytail: LIKE 掃描，6946 列夠用；要相關性排序或詞幹處理再換 FTS5
        if keyword:
            where.append("LOWER(c.name) LIKE LOWER(?)")
            params.append(f"%{keyword}%")
        if description:
            where.append("LOWER(c.description) LIKE LOWER(?)")
            params.append(f"%{description}%")
        if ingredient:
            where.append(
                "EXISTS (SELECT 1 FROM cocktail_ingredients ci"
                " WHERE ci.cocktail_id = c.id"
                " AND (LOWER(ci.item) LIKE LOWER(?)"
                "      OR LOWER(ci.item_generic) LIKE LOWER(?)))"
            )
            params.extend([f"%{ingredient}%", f"%{ingredient}%"])
        if tag:
            # json_each 對 tags IS NULL 會靜默回傳零列，不需額外防護
            where.append(
                "EXISTS (SELECT 1 FROM json_each(c.tags) t"
                " WHERE LOWER(t.value) = LOWER(?))"
            )
            params.append(tag.strip())
        if min_rating is not None:
            where.append("c.rating_value >= ?")
            params.append(min_rating)
        if max_rating is not None:
            where.append("c.rating_value <= ?")
            params.append(max_rating)
        if min_abv is not None:
            where.append("c.abv >= ?")
            params.append(min_abv)
        if max_abv is not None:
            where.append("c.abv <= ?")
            params.append(max_abv)
        if min_count is not None:
            where.append("c.rating_count >= ?")
            params.append(min_count)
        # sweet_sour 0-10，數值越高越偏酸/乾（甜點調酒約 4-5、Sours 約 7-8）
        if min_sweet_sour is not None:
            where.append("c.sweet_sour >= ?")
            params.append(min_sweet_sour)
        if max_sweet_sour is not None:
            where.append("c.sweet_sour <= ?")
            params.append(max_sweet_sour)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        column = _SORT_COLUMNS[sort]
        direction = "DESC" if desc else "ASC"
        params.append(limit)

        # 兩層 tie-breaker：主排序鍵並列時（例如 288 筆同為 5.0 分），
        # 先比 c.rating_count（票數多者代表性更高，優先於字母序），
        # 最後才是 c.id ——純粹保證完全確定性，沒有排序意義。
        rows = self.conn.execute(
            f"""
            SELECT c.* FROM cocktails c
            {clause}
            ORDER BY {column} {direction} NULLS LAST, c.rating_count DESC, c.id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._attach_ingredients(dict(r)) for r in rows]

    def get_cocktail_by_id(self, cocktail_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM cocktails WHERE id = ?", (cocktail_id,)
        ).fetchone()
        if not row:
            return None
        return self._attach_ingredients(dict(row))

    def get_cocktail_by_name(self, name: str) -> Optional[dict[str, Any]]:
        """精確比對（大小寫不分），若無結果則回退至部分比對。"""
        row = self.conn.execute(
            "SELECT * FROM cocktails WHERE LOWER(name) = LOWER(?) LIMIT 1", (name,)
        ).fetchone()
        if not row:
            row = self.conn.execute(
                "SELECT * FROM cocktails WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
                (f"%{name}%",),
            ).fetchone()
        if not row:
            return None
        return self._attach_ingredients(dict(row))

    def get_stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) FROM cocktails").fetchone()[0]
        avg_rating = self.conn.execute(
            "SELECT ROUND(AVG(rating_value), 2) FROM cocktails WHERE rating_value IS NOT NULL"
        ).fetchone()[0]
        last_run = self.get_last_successful_run()
        return {
            "總雞尾酒數": total,
            "平均評分": avg_rating,
            "最後爬取": last_run or "從未",
        }

    def _attach_ingredients(self, cocktail: dict[str, Any]) -> dict[str, Any]:
        ings = self.conn.execute(
            "SELECT * FROM cocktail_ingredients WHERE cocktail_id = ? ORDER BY sort_order",
            (cocktail["id"],),
        ).fetchall()
        cocktail["ingredients"] = [dict(i) for i in ings]
        if cocktail.get("tags"):
            try:
                cocktail["tags"] = json.loads(cocktail["tags"])
            except (json.JSONDecodeError, TypeError):
                pass
        return cocktail

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
