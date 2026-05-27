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

    def _init_schema(self):
        self.conn.executescript(_DDL)
        self.conn.commit()

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
        row = self._prepare_row(data)
        existing = cur.execute(
            "SELECT id FROM cocktails WHERE url = ?", (row["url"],)
        ).fetchone()

        if existing:
            cocktail_id = existing[0]
            cur.execute(
                """
                UPDATE cocktails SET
                    name=:name, slug=:slug, description=:description,
                    glassware=:glassware, garnish=:garnish, prepare=:prepare,
                    instructions=:instructions, review=:review, history=:history,
                    tags=:tags, rating_value=:rating_value, rating_count=:rating_count,
                    calories=:calories, prep_time_min=:prep_time_min, abv=:abv,
                    date_published=:date_published, lastmod=:lastmod,
                    scraped_at=CURRENT_TIMESTAMP
                WHERE url=:url
            """,
                row,
            )
        else:
            cur.execute(
                """
                INSERT INTO cocktails
                    (id, name, slug, description, glassware, garnish, prepare,
                     instructions, review, history, tags, rating_value, rating_count,
                     calories, prep_time_min, abv, date_published, url, lastmod)
                VALUES
                    (:id, :name, :slug, :description, :glassware, :garnish, :prepare,
                     :instructions, :review, :history, :tags, :rating_value, :rating_count,
                     :calories, :prep_time_min, :abv, :date_published, :url, :lastmod)
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

    def search_cocktails(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT c.*,
                   GROUP_CONCAT(ci.amount || ' ' || ci.item, ', ') AS ingredients_text
            FROM cocktails c
            LEFT JOIN cocktail_ingredients ci ON c.id = ci.cocktail_id
            WHERE LOWER(c.name) LIKE LOWER(?)
            GROUP BY c.id
            ORDER BY c.rating_value DESC NULLS LAST
            LIMIT ?
        """,
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def get_top_rated(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
             SELECT * FROM cocktails
             WHERE rating_value IS NOT NULL AND rating_count >= 5
             ORDER BY rating_value DESC, rating_count DESC
             LIMIT ?
         """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def filter_by_ingredient(
        self, ingredient: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search cocktail_ingredients.item OR item_generic LIKE %ingredient%."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.*
            FROM cocktails c
            JOIN cocktail_ingredients ci ON c.id = ci.cocktail_id
            WHERE LOWER(ci.item) LIKE LOWER(?)
               OR LOWER(ci.item_generic) LIKE LOWER(?)
            ORDER BY c.rating_value DESC NULLS LAST
            LIMIT ?
            """,
            (f"%{ingredient}%", f"%{ingredient}%", limit),
        ).fetchall()
        return [self._attach_ingredients(dict(r)) for r in rows]

    def filter_by_tag(self, tag: str, limit: int = 20) -> list[dict[str, Any]]:
        """Filter cocktails where tags JSON contains the given tag string."""
        rows = self.conn.execute(
            "SELECT * FROM cocktails WHERE tags IS NOT NULL"
        ).fetchall()
        results: list[dict[str, Any]] = []
        target = tag.strip().lower()
        for row in rows:
            cocktail = dict(row)
            try:
                tags = json.loads(cocktail.get("tags") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if any(str(t).lower() == target for t in tags):
                results.append(self._attach_ingredients(cocktail))
                if len(results) >= limit:
                    break
        return results

    def filter_by_rating(
        self,
        min_rating: float = 0.0,
        max_rating: float = 5.0,
        min_count: int = 5,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Filter by rating range with minimum vote count."""
        rows = self.conn.execute(
            """
            SELECT * FROM cocktails
            WHERE rating_value IS NOT NULL
              AND rating_value BETWEEN ? AND ?
              AND rating_count >= ?
            ORDER BY rating_value DESC, rating_count DESC
            LIMIT ?
            """,
            (min_rating, max_rating, min_count, limit),
        ).fetchall()
        return [self._attach_ingredients(dict(r)) for r in rows]

    def filter_by_abv(
        self,
        min_abv: float = 0.0,
        max_abv: float = 100.0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Filter by ABV range."""
        rows = self.conn.execute(
            """
            SELECT * FROM cocktails
            WHERE abv IS NOT NULL
              AND abv BETWEEN ? AND ?
            ORDER BY abv DESC NULLS LAST
            LIMIT ?
            """,
            (min_abv, max_abv, limit),
        ).fetchall()
        return [self._attach_ingredients(dict(r)) for r in rows]

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
