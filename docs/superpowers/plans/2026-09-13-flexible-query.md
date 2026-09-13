# 彈性排序與搜尋 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `storage.py` 六個寫死的查詢方法收斂成單一組合式 `query_cocktails()`，讓 CLI 與 LINE bot 都能疊加多條件並自選排序鍵與方向。

**Architecture:** 一個 query builder 在 `storage.py` 動態組 WHERE 與 ORDER BY（sort key 走白名單防注入），`query.py` 與 `bot.py` 各自只留薄轉接層。標籤篩選從「撈全表進 Python 掃描」改成 SQLite 內建 `json_each`。不加任何新依賴。

**Tech Stack:** Python 3.12、SQLite（stdlib `sqlite3`，用到 `json_each` 與 `NULLS LAST`）、Flask、pytest、uv

## Global Constraints

- 套件管理一律 `uv`；測試指令 `uv run python -m pytest tests/ -q`
- 註解與 docstring 用繁體中文（專案慣例，見 CLAUDE.md）
- 不新增任何第三方依賴；不使用 FTS5
- SQLite 版本下限 3.30（`NULLS LAST`）— 本機 3.53.4、線上 `python:3.12-slim` 為 3.40，皆符合
- `sort` 參數只接受白名單 key，這是唯一把字串拼進 SQL 的地方，驗證不可省略
- `min_count` 預設必須是 `None`。設成 `5` 會讓 994 筆 `rating_count < 5 或 NULL` 的資料從材料／標籤／ABV 查詢中無聲消失
- 材料條件必須同時比對 `ci.item` 與 `ci.item_generic`（沿用現行行為）
- `bot.py` 的 `RESULT_LIMIT_MAX = 20` 上限不動
- 向下相容的承諾是**使用者輸入的指令字串仍可用**，不是 `parse_command` 的內部回傳形狀不變。
  `tests/unit/test_bot.py::test_parse_cocktail_commands` 是這個承諾的回歸測試，Task 3 結束時必須通過。
  其中只有 `search` 的三行（第 10、11、13 行）因回傳改為 kwargs dict 而需要更新斷言；
  所有 `list` 的斷言（第 15-27 行）與指令字串本身一律不得修改 — 它們一改就失去回歸測試的意義

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `diffords_guide/storage.py` | 唯一的查詢引擎 `query_cocktails()` | 修改：新增 1 個方法、刪除 6 個 |
| `query.py` | CLI 轉接：argparse flag → kwargs | 修改：`cmd_search` / `cmd_list` / `build_parser` |
| `bot.py` | LINE 轉接：token 掃描 → kwargs | 修改：`parse_command` / `fmt_cocktail_list` / `fmt_cocktail_search` / `fmt_help` |
| `tests/unit/test_query_cocktails.py` | query builder 的行為測試 | 新增 |
| `tests/unit/test_bot.py` | token 掃描與向下相容 | 修改：新增測試 |
| `tests/unit/test_diffords.py` | 既有 storage 測試 | 修改：第 106 行呼叫換名 |

Task 1 是其他所有 task 的前提。Task 2（CLI）與 Task 3（LINE）彼此獨立，可任意順序。

---

### Task 1: `storage.query_cocktails()` 查詢引擎

**Files:**
- Modify: `diffords_guide/storage.py:288-429`（`search_cocktails`、`get_top_rated`、`filter_by_ingredient`、`filter_by_tag`、`filter_by_rating`、`filter_by_abv`）
- Modify: `tests/unit/test_diffords.py:106`
- Test: `tests/unit/test_query_cocktails.py`（新增）

**Interfaces:**
- Consumes: 既有的 `DiffordsStorage.__init__`、`_attach_ingredients()`、`_DDL` schema
- Produces:
  - `DiffordsStorage.query_cocktails(*, keyword: str | None = None, description: str | None = None, ingredient: str | None = None, tag: str | None = None, min_rating: float | None = None, max_rating: float | None = None, min_abv: float | None = None, max_abv: float | None = None, min_count: int | None = None, sort: str = "rating", desc: bool = True, limit: int = 20) -> list[dict[str, Any]]`
  - 模組層常數 `SORT_KEYS: tuple[str, ...]`，值為 `("rating", "abv", "calories", "date", "name", "count")`，供 `query.py` 的 argparse `choices` 與 `bot.py` 的錯誤訊息使用
  - 非法 `sort` 拋 `ValueError`，訊息格式：`不支援的排序鍵：{sort}（可用：rating, abv, calories, date, name, count）`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/unit/test_query_cocktails.py`：

```python
"""query_cocktails() 組合查詢的行為測試。"""

import pytest

from diffords_guide.storage import SORT_KEYS, DiffordsStorage


def _cocktail(cid, name, *, rating=None, count=None, abv=None,
              calories=None, date=None, tags=None, desc=None,
              ingredients=None):
    """組出 save_cocktail() 吃的最小 payload。

    兩個非顯而易見的要求：
    - id 是從 url 的 /cocktails/recipe/{id}/{slug} 解析出來的，payload 的 "id" 鍵不被讀取
    - ingredients_html 的每個元素必須有 sort_order，缺了會 KeyError，
      而 save_cocktail 會吞掉例外只回 False，導致測試拿到空 DB
    """
    return {
        "name": name,
        "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/{name.lower().replace(' ', '-')}",
        "description": desc,
        "rating_value": rating,
        "rating_count": count,
        "abv": abv,
        "calories": calories,
        "date_published": date,
        "tags": tags or [],
        "ingredients_html": [
            {"sort_order": i, "item": item, "amount": "30ml"}
            for i, item in enumerate(ingredients or [])
        ],
    }


@pytest.fixture
def storage(tmp_path):
    """五筆固定資料，涵蓋 NULL、同分、多標籤與多食材。"""
    with DiffordsStorage(str(tmp_path / "t.db")) as st:
        # save_cocktail 吞例外只回 False，所以每筆都要斷言，否則失敗會靜默
        assert st.save_cocktail(_cocktail(
            1, "Negroni", rating=4.5, count=100, abv=24.0, calories=200,
            date="2020-01-01", tags=["Classic/vintage"], desc="bitter citrus",
            ingredients=["Gin", "Campari"])) is True
        assert st.save_cocktail(_cocktail(
            2, "Martini", rating=4.5, count=50, abv=30.0, calories=180,
            date="2021-01-01", tags=["Classic/vintage"], desc="dry and cold",
            ingredients=["Gin", "Dry Vermouth"])) is True
        assert st.save_cocktail(_cocktail(
            3, "Daiquiri", rating=4.0, count=80, abv=20.0, calories=150,
            date="2019-01-01", tags=["Sour"], desc="fresh citrus",
            ingredients=["Rum", "Lime Juice"])) is True
        # abv / calories 為 NULL，用來驗證 NULLS LAST
        assert st.save_cocktail(_cocktail(
            4, "Mystery", rating=3.0, count=10, tags=["Sour"],
            ingredients=["Gin"])) is True
        # rating_count < 5，用來驗證 min_count 預設不吃掉資料
        assert st.save_cocktail(_cocktail(
            5, "Obscure Gin Thing", rating=5.0, count=1, abv=40.0,
            calories=90, date="2022-01-01", ingredients=["Gin"])) is True
        yield st


def test_conditions_are_combined_not_exclusive(storage):
    """材料 + 評分 + ABV 三個條件同時生效（舊版是 if/elif 只會套用一個）。

    Negroni 被 abv 28 濾掉、Daiquiri 被評分 4.2 濾掉、Mystery 無 abv 被濾掉。
    剩下兩筆依評分降序。
    """
    rows = storage.query_cocktails(ingredient="gin", min_rating=4.2, min_abv=28)
    assert [r["name"] for r in rows] == ["Obscure Gin Thing", "Martini"]


def test_no_filters_returns_everything(storage):
    rows = storage.query_cocktails(limit=100)
    assert len(rows) == 5


def test_min_count_defaults_to_none_and_keeps_low_vote_rows(storage):
    """min_count 預設 None：只有 1 票的 Obscure Gin Thing 必須留著。"""
    names = [r["name"] for r in storage.query_cocktails(ingredient="gin", limit=100)]
    assert "Obscure Gin Thing" in names
    filtered = storage.query_cocktails(ingredient="gin", min_count=5, limit=100)
    assert "Obscure Gin Thing" not in [r["name"] for r in filtered]


def test_tag_filter_uses_json_each(storage):
    rows = storage.query_cocktails(tag="Classic/vintage", limit=100)
    assert sorted(r["name"] for r in rows) == ["Martini", "Negroni"]


def test_tag_filter_survives_null_tags(storage):
    """id=5 沒有 tags；json_each 不該炸，也不該誤中。"""
    rows = storage.query_cocktails(tag="Sour", limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri", "Mystery"]


def test_description_filter(storage):
    rows = storage.query_cocktails(description="citrus", limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri", "Negroni"]


def test_keyword_matches_name_only(storage):
    """keyword 只比對 name — description 含 'dry' 的 Martini 不該靠 keyword 中。"""
    rows = storage.query_cocktails(keyword="mart", limit=100)
    assert [r["name"] for r in rows] == ["Martini"]


def test_max_bounds(storage):
    rows = storage.query_cocktails(max_abv=22, limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri"]
    rows = storage.query_cocktails(max_rating=4.0, min_rating=4.0, limit=100)
    assert [r["name"] for r in rows] == ["Daiquiri"]


@pytest.mark.parametrize("sort,expected_first", [
    ("rating", "Obscure Gin Thing"),
    ("abv", "Obscure Gin Thing"),
    ("calories", "Negroni"),
    ("date", "Obscure Gin Thing"),
    ("name", "Obscure Gin Thing"),
    ("count", "Negroni"),
])
def test_every_sort_key_descending(storage, sort, expected_first):
    rows = storage.query_cocktails(sort=sort, desc=True, limit=100)
    assert rows[0]["name"] == expected_first


def test_ascending_reverses_order(storage):
    asc = [r["name"] for r in storage.query_cocktails(sort="calories", desc=False, limit=100)]
    assert asc[0] == "Obscure Gin Thing"  # 90 卡最低


def test_nulls_sort_last_in_both_directions(storage):
    """Mystery 的 abv 是 NULL，升序降序都該墊底。"""
    for desc in (True, False):
        names = [r["name"] for r in storage.query_cocktails(sort="abv", desc=desc, limit=100)]
        assert names[-1] == "Mystery"


def test_ties_broken_by_id_for_stable_order(storage):
    """Negroni 與 Martini 同為 4.5 分，順序必須固定。"""
    first = [r["name"] for r in storage.query_cocktails(sort="rating", limit=100)]
    second = [r["name"] for r in storage.query_cocktails(sort="rating", limit=100)]
    assert first == second
    assert first.index("Negroni") < first.index("Martini")  # id 1 在 id 2 前


def test_limit_is_applied(storage):
    assert len(storage.query_cocktails(limit=2)) == 2


def test_invalid_sort_key_raises(storage):
    """惡意 sort 值必須在拼進 SQL 前就被白名單擋下。"""
    with pytest.raises(ValueError) as exc:
        storage.query_cocktails(sort="rating; DELETE FROM cocktails")
    assert "不支援的排序鍵" in str(exc.value)


def test_sort_keys_constant_is_exported():
    assert SORT_KEYS == ("rating", "abv", "calories", "date", "name", "count")


def test_results_include_attached_ingredients(storage):
    rows = storage.query_cocktails(keyword="negroni")
    assert sorted(i["item"] for i in rows[0]["ingredients"]) == ["Campari", "Gin"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run python -m pytest tests/unit/test_query_cocktails.py -q`
Expected: 收集階段就 FAIL — `ImportError: cannot import name 'SORT_KEYS' from 'diffords_guide.storage'`

- [ ] **Step 3: 實作 `query_cocktails()`**

在 `diffords_guide/storage.py` 的 `_DDL` 常數之後、`_to_real` 之前，加入模組層常數：

```python
# 排序鍵白名單。這是唯一會把字串拼進 SQL 的地方，不可改成動態欄位名。
_SORT_COLUMNS = {
    "rating": "c.rating_value",
    "abv": "c.abv",
    "calories": "c.calories",
    "date": "c.date_published",
    "name": "c.name",
    "count": "c.rating_count",
}
SORT_KEYS = tuple(_SORT_COLUMNS)
```

在 `# 查詢（供 bot.py 使用）` 註解區塊下方，用下列方法**取代** `search_cocktails`（第 292-306 行）：

```python
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
            params.append(tag)
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

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        column = _SORT_COLUMNS[sort]
        direction = "DESC" if desc else "ASC"
        params.append(limit)

        # 尾端的 c.id 是 tie-breaker：少了它，同分項目在不同次查詢間會跳動
        rows = self.conn.execute(
            f"""
            SELECT c.* FROM cocktails c
            {clause}
            ORDER BY {column} {direction} NULLS LAST, c.id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._attach_ingredients(dict(r)) for r in rows]
```

刪除 `get_top_rated`（原第 330-340 行）、`filter_by_ingredient`、`filter_by_tag`、`filter_by_rating`、`filter_by_abv`（原第 354-429 行）。保留 `get_cocktail_by_id`、`get_cocktail_by_name`、`get_stats`、`_attach_ingredients`。

不要動 `import json` — `_prepare_row` 與 `_attach_ingredients` 仍需要它。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run python -m pytest tests/unit/test_query_cocktails.py -q`
Expected: PASS，21 passed（15 個測試函式，其中 `test_every_sort_key_descending` 參數化 6 次）

- [ ] **Step 5: 修好既有測試的呼叫點**

`tests/unit/test_diffords.py:106` 從：

```python
        by_ingredient = storage.filter_by_ingredient("campari")
```

改成：

```python
        by_ingredient = storage.query_cocktails(ingredient="campari")
```

- [ ] **Step 6: 確認 storage 測試綠燈**

Run: `uv run python -m pytest tests/unit/test_diffords.py tests/unit/test_query_cocktails.py -q`
Expected: PASS

Run: `uv run python -m pytest tests/ -q`
Expected: `test_query.py` 與 `test_bot.py` 會 FAIL（它們還在呼叫已刪除的方法）— 這是預期的，Task 2 與 Task 3 會修。

- [ ] **Step 7: 提交**

```bash
git add diffords_guide/storage.py tests/unit/test_query_cocktails.py tests/unit/test_diffords.py
git commit -m "feat(storage): 以組合式 query_cocktails 取代六個寫死的查詢方法

支援多條件疊加、六種排序鍵與升降序，標籤篩選改用 SQLite json_each
取代撈全表進 Python 掃描。sort 走白名單防注入，min_count 預設 None
以免低票數酒譜在材料／標籤查詢中無聲消失。"
```

---

### Task 2: CLI 可疊加 flag

**Files:**
- Modify: `query.py:41-46`（`cmd_search`）、`query.py:106-127`（`cmd_list`）、`query.py:129-160`（`build_parser`）
- Test: `tests/unit/test_query.py`

**Interfaces:**
- Consumes: Task 1 的 `DiffordsStorage.query_cocktails(**kwargs)` 與 `SORT_KEYS`
- Produces: 無下游 task 依賴

- [ ] **Step 1: 先看既有測試怎麼寫的**

Run: `uv run python -m pytest tests/unit/test_query.py -q`
Expected: FAIL（呼叫已刪除的 storage 方法）

讀 `tests/unit/test_query.py` 全文，記下它用什麼方式建 DB 與斷言輸出，後續測試沿用同樣風格；順手把它裡面對已刪除方法的呼叫改成 `query_cocktails(...)` 對應寫法。

- [ ] **Step 2: 寫失敗測試**

在 `tests/unit/test_query.py` 末尾新增：

```python
def test_list_flags_combine_and_sort(capsys, tmp_path):
    """--ingredient 與 --rating 疊加，且 --sort abv 生效。"""
    import query
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, rating, abv in [
            (1, "Low Gin", 4.5, 10.0),
            (2, "High Gin", 4.5, 40.0),
            (3, "Bad Gin", 2.0, 50.0),
        ]:
            # url 決定 id；ingredients_html 缺 sort_order 會讓 save 靜默失敗
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": rating, "rating_count": 20, "abv": abv,
                "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
            }) is True

    parser = query.build_parser()
    args = parser.parse_args([
        "--db", str(db), "list",
        "--ingredient", "gin", "--rating", "4.0", "--sort", "abv",
    ])
    args.func(args)

    out = capsys.readouterr().out
    assert "Bad Gin" not in out                          # 被 --rating 4.0 濾掉
    assert out.index("High Gin") < out.index("Low Gin")  # --sort abv 降序


def test_list_asc_flag(capsys, tmp_path):
    import query
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, abv in [(1, "Weak", 5.0), (2, "Strong", 50.0)]:
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": 4.0, "rating_count": 10, "abv": abv,
                "ingredients_html": [],
            }) is True

    parser = query.build_parser()
    args = parser.parse_args(["--db", str(db), "list", "--sort", "abv", "--asc"])
    args.func(args)

    out = capsys.readouterr().out
    assert out.index("Weak") < out.index("Strong")


def test_invalid_sort_key_rejected_by_argparse():
    import pytest as _pytest

    import query

    parser = query.build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["list", "--sort", "nope"])
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run python -m pytest tests/unit/test_query.py -q`
Expected: FAIL — `argparse: unrecognized arguments: --sort`

- [ ] **Step 4: 改 `cmd_search`**

`query.py:43-46` 從：

```python
    with _open_storage(args.db) as storage:
        rows = storage.search_cocktails(args.keyword, limit=args.limit)
```

改成：

```python
    with _open_storage(args.db) as storage:
        rows = storage.query_cocktails(
            keyword=args.keyword, sort=args.sort, desc=not args.asc, limit=args.limit
        )
```

- [ ] **Step 5: 改 `cmd_list`**

用下列內容**整段取代** `query.py:106-127` 的 `cmd_list`：

```python
def cmd_list(args: argparse.Namespace) -> None:
    filters = {
        "keyword": args.keyword,
        "description": args.description,
        "ingredient": args.ingredient,
        "tag": args.tag,
        "min_rating": args.rating,
        "max_rating": args.max_rating,
        "min_abv": args.abv,
        "max_abv": args.max_abv,
        "min_count": args.min_count,
    }
    active = {k: v for k, v in filters.items() if v is not None}

    labels = {
        "keyword": "名稱含", "description": "描述含", "ingredient": "材料含",
        "tag": "標籤", "min_rating": "評分 >=", "max_rating": "評分 <=",
        "min_abv": "ABV >=", "max_abv": "ABV <=", "min_count": "評分數 >=",
    }
    parts = [f"{labels[k]} {v}" for k, v in active.items()]

    # 沒下任何條件時沿用舊的「社群高分精選」語意：5 票門檻
    if not active:
        active["min_count"] = 5
        parts = ["社群高分精選"]

    parts.append(f"依 {args.sort} {'升序' if args.asc else '降序'}")
    title = "、".join(parts)

    with _open_storage(args.db) as storage:
        rows = storage.query_cocktails(
            **active, sort=args.sort, desc=not args.asc, limit=args.limit
        )

    print(f"\n雞尾酒列表（{title}，顯示 {len(rows)} 筆）\n")
    _print_rows(rows)
```

- [ ] **Step 6: 改 `build_parser`**

`query.py` 頂端的 import 從：

```python
from diffords_guide.storage import DiffordsStorage
```

改成：

```python
from diffords_guide.storage import SORT_KEYS, DiffordsStorage
```

在 `build_parser()` 中，`p_search.add_argument("--limit", type=int, default=20)` 之後補上：

```python
    p_search.add_argument("--sort", choices=SORT_KEYS, default="rating")
    p_search.add_argument("--asc", action="store_true", help="改為升序（預設降序）")
```

`p_list` 的 flag 區塊整段換成：

```python
    p_list.add_argument("--keyword", help="依名稱關鍵字篩選")
    p_list.add_argument("--description", help="依描述關鍵字篩選")
    p_list.add_argument("--ingredient", help="依食材篩選")
    p_list.add_argument("--tag", help="依標籤篩選")
    p_list.add_argument("--rating", type=float, help="最低評分")
    p_list.add_argument("--max-rating", type=float, dest="max_rating", help="最高評分")
    p_list.add_argument("--abv", type=float, help="最低 ABV")
    p_list.add_argument("--max-abv", type=float, dest="max_abv", help="最高 ABV")
    p_list.add_argument("--min-count", type=int, dest="min_count", help="最低評分人數")
    p_list.add_argument("--sort", choices=SORT_KEYS, default="rating")
    p_list.add_argument("--asc", action="store_true", help="改為升序（預設降序）")
    p_list.add_argument("--limit", type=int, default=20)
```

parser 的 `epilog` 換成：

```python
        epilog="""範例:
  uv run python query.py stats
  uv run python query.py search negroni
  uv run python query.py info "Negroni"
  uv run python query.py list --ingredient gin --rating 4.2 --abv 20 --sort abv --limit 15
  uv run python query.py list --tag Classic/vintage --sort calories --asc
  uv run python query.py list --description citrus --sort date
""",
```

- [ ] **Step 7: 跑測試確認通過**

Run: `uv run python -m pytest tests/unit/test_query.py -q`
Expected: PASS

- [ ] **Step 8: 對真實 DB 手動驗一次**

Run: `uv run python query.py list --ingredient gin --rating 4.2 --abv 20 --sort abv --limit 5`
Expected: 印出 5 筆，ABV 由高到低，標題含「材料含 gin、評分 >= 4.2、ABV >= 20.0、依 abv 降序」

Run: `uv run python query.py list --tag Classic/vintage --sort calories --asc --limit 5`
Expected: 印出 5 筆，卡路里由低到高

- [ ] **Step 9: 提交**

```bash
git add query.py tests/unit/test_query.py
git commit -m "feat(cli): list 的篩選 flag 改為可疊加並支援自選排序

新增 --keyword/--description/--max-rating/--max-abv/--min-count/--sort/--asc，
移除原本 if/elif 互斥鏈。無條件時仍套用 5 票門檻以維持高分精選語意。"
```

---

### Task 3: LINE bot token 掃描

**Files:**
- Modify: `bot.py:178-184`（`fmt_cocktail_search`）、`bot.py:304-336`（`fmt_cocktail_list`）、`bot.py:362-397`（`fmt_help`）、`bot.py:400-453`（`_scan_conditions` 與 `parse_command`）、`bot.py:455-465`（`handle_message`）
- Test: `tests/unit/test_bot.py`

**Interfaces:**
- Consumes: Task 1 的 `DiffordsStorage.query_cocktails(**kwargs)`
- Produces: 無下游 task 依賴
- `parse_command` 新增回傳型態 `("error", [訊息字串])`；`search` 的 args 從 `[keyword, limit]` 改為 `[kwargs_dict]`

- [ ] **Step 1: 寫失敗測試**

先把 `tests/unit/test_bot.py` 第 10、11、13 行的 `search` 斷言改成新的 kwargs dict 形狀（`search` 的回傳形狀變了，指令字串不變）：

```python
    assert bot.parse_command("雞尾酒搜尋 negroni") == (
        "search", [{"keyword": "negroni", "limit": 5}])
    assert bot.parse_command("雞尾酒搜尋 negroni 12筆") == (
        "search", [{"keyword": "negroni", "limit": 12}])
    # 酒名以數字結尾時不該被當成筆數
    assert bot.parse_command("雞尾酒搜尋 Apollo 8") == (
        "search", [{"keyword": "Apollo 8", "limit": 5}])
```

`test_parse_cocktail_commands` 的其餘每一行（尤其第 15-27 行所有 `list` 斷言）**一個字都不要動** — 它們就是向下相容的回歸測試。

然後在 `test_parse_cocktail_commands` **之後**新增：

```python
def test_parse_combined_conditions():
    assert bot.parse_command("雞尾酒列表 材料 gin 評分 4.2 酒精濃度 20 排序 abv 降序 15筆") == (
        "list",
        [{"ingredient": "gin", "min_rating": 4.2, "min_abv": 20.0,
          "sort": "abv", "desc": True, "limit": 15}],
    )


def test_parse_multiword_value():
    """材料值吃到下一個關鍵詞為止，支援多詞。"""
    assert bot.parse_command("雞尾酒列表 材料 dry vermouth 描述 citrus") == (
        "list",
        [{"ingredient": "dry vermouth", "description": "citrus"}],
    )


def test_parse_sort_value_shadowing_a_filter_keyword():
    """排序後恰好取一個 token：第二個「酒精濃度」是 sort key 不是篩選條件。"""
    assert bot.parse_command("雞尾酒列表 酒精濃度 20 排序 酒精濃度") == (
        "list",
        [{"min_abv": 20.0, "sort": "abv"}],
    )


def test_parse_ascending_flag():
    assert bot.parse_command("雞尾酒列表 標籤 Sour 排序 卡路里 升序") == (
        "list",
        [{"tag": "Sour", "sort": "calories", "desc": False}],
    )


def test_parse_search_with_sort():
    assert bot.parse_command("雞尾酒搜尋 negroni 排序 日期") == (
        "search",
        [{"keyword": "negroni", "sort": "date", "limit": 5}],
    )


def test_parse_search_without_keyword_is_error():
    command, args = bot.parse_command("雞尾酒搜尋 排序 abv")
    assert command == "error"
    assert "關鍵字" in args[0]


def test_parse_unknown_token_reports_error():
    command, args = bot.parse_command("雞尾酒列表 顏色 紅色")
    assert command == "error"
    assert "顏色" in args[0]


def test_parse_non_numeric_value_reports_error():
    command, args = bot.parse_command("雞尾酒列表 評分 高")
    assert command == "error"
    assert "評分" in args[0]


def test_parse_unknown_sort_key_reports_error():
    command, args = bot.parse_command("雞尾酒列表 排序 顏色")
    assert command == "error"
    assert "排序" in args[0]


def test_handle_message_surfaces_parse_error(tmp_path):
    result = bot.handle_message("雞尾酒列表 顏色 紅色", str(tmp_path / "t.db"))
    assert "顏色" in result


def test_fmt_cocktail_list_combines_and_sorts(tmp_path):
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, abv in [(1, "Weak Gin", 10.0), (2, "Strong Gin", 45.0)]:
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": 4.5, "rating_count": 20, "abv": abv,
                "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
            }) is True

    result = bot.fmt_cocktail_list(str(db), ingredient="gin", sort="abv", desc=True)
    assert result.index("Strong Gin") < result.index("Weak Gin")
    assert "gin" in result
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run python -m pytest tests/unit/test_bot.py -q`
Expected: 新測試全 FAIL，且既有的 `test_parse_cocktail_commands` 也 FAIL（`fmt_*` 還在呼叫已刪除的 storage 方法）

- [ ] **Step 3: 加 token 掃描器**

在 `bot.py` 的 `_LIMIT_RE = re.compile(r"\s+(\d+)\s*筆$")`（第 400 行）**之後**、`_split_limit` 之前，加入：

```python
# 中文關鍵詞對英文資料（酒名／食材／標籤皆為英文）天然不衝突，
# 所以貪婪取值是安全的。
_GREEDY_KEYS = {"材料": "ingredient", "標籤": "tag", "描述": "description"}
_NUMERIC_KEYS = {
    "評分": "min_rating",
    "最高評分": "max_rating",
    "酒精濃度": "min_abv",
    "abv": "min_abv",
    "最高酒精濃度": "max_abv",
}
_SORT_ALIASES = {
    "評分": "rating", "rating": "rating",
    "酒精濃度": "abv", "abv": "abv",
    "卡路里": "calories", "calories": "calories",
    "日期": "date", "date": "date",
    "名稱": "name", "name": "name",
    "評分數": "count", "count": "count",
}
_FLAG_KEYS = {"升序": False, "降序": True}
_ALL_KEYS = set(_GREEDY_KEYS) | set(_NUMERIC_KEYS) | {"排序"} | set(_FLAG_KEYS)


def _scan_conditions(tokens: list[str]) -> dict[str, Any]:
    """把 token 串解析成 query_cocktails 的 kwargs。

    值元數（arity）是消歧義的關鍵：
      材料/標籤/描述 → 貪婪吃到下一個關鍵詞（支援 "dry vermouth"）
      評分等數值鍵   → 恰好一個 token
      排序           → 恰好一個 token（否則「排序 酒精濃度」會被當成新條件）
      升序/降序      → 零個 token
    解析失敗一律拋 ValueError，由 parse_command 轉成使用者訊息。
    只放實際出現在指令中的鍵，不塞預設值 — 否則舊指令的相容測試會壞。
    """
    out: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        key = tokens[i].lower()
        if key in _GREEDY_KEYS:
            i += 1
            start = i
            while i < len(tokens) and tokens[i].lower() not in _ALL_KEYS:
                i += 1
            if i == start:
                raise ValueError(f"「{tokens[start - 1]}」後面缺少值。")
            out[_GREEDY_KEYS[key]] = " ".join(tokens[start:i])
        elif key in _NUMERIC_KEYS:
            if i + 1 >= len(tokens):
                raise ValueError(f"「{tokens[i]}」後面缺少數值。")
            raw = tokens[i + 1].rstrip("%")
            try:
                out[_NUMERIC_KEYS[key]] = float(raw)
            except ValueError:
                raise ValueError(
                    f"「{tokens[i]}」的值必須是數字，收到「{tokens[i + 1]}」。"
                ) from None
            i += 2
        elif key == "排序":
            if i + 1 >= len(tokens):
                raise ValueError("「排序」後面缺少排序依據。")
            alias = tokens[i + 1].lower()
            if alias not in _SORT_ALIASES:
                raise ValueError(
                    f"「排序」不支援「{tokens[i + 1]}」，可用："
                    "評分、酒精濃度、卡路里、日期、名稱、評分數。"
                )
            out["sort"] = _SORT_ALIASES[alias]
            i += 2
        elif key in _FLAG_KEYS:
            out["desc"] = _FLAG_KEYS[key]
            i += 1
        else:
            raise ValueError(
                f"不認識的條件：「{tokens[i]}」。可用條件："
                "材料、標籤、描述、評分、最高評分、酒精濃度、最高酒精濃度、排序、升序、降序。"
            )
    return out
```

- [ ] **Step 4: 改 `parse_command` 的搜尋分支**

`bot.py:428-430` 從：

```python
    match = re.match(r"^(?:雞尾酒搜尋|cocktail search|search)\s+(.+)$", text, re.I)
    if match:
        return "search", [match.group(1).strip(), limit or SEARCH_LIMIT_DEFAULT]
```

改成：

```python
    match = re.match(r"^(?:雞尾酒搜尋|cocktail search|search)\s+(.+)$", text, re.I)
    if match:
        tokens = match.group(1).split()
        head = 0
        while head < len(tokens) and tokens[head].lower() not in _ALL_KEYS:
            head += 1
        if head == 0:
            return "error", ["🔍 「雞尾酒搜尋」後面需要關鍵字，例如「雞尾酒搜尋 negroni」。"]
        try:
            args = _scan_conditions(tokens[head:])
        except ValueError as exc:
            return "error", [f"⚠️ {exc}"]
        args["keyword"] = " ".join(tokens[:head])
        args["limit"] = limit or SEARCH_LIMIT_DEFAULT
        return "search", [args]
```

- [ ] **Step 5: 改 `parse_command` 的列表分支**

刪掉 `extra = {"limit": limit} if limit else {}`、四條 `雞尾酒列表 材料/標籤/評分/酒精濃度` 的 regex 分支，以及 `if lower in ("雞尾酒列表", "cocktail list", "list"):` 分支（`bot.py:436-450`），整段換成：

```python
    match = re.match(r"^(?:雞尾酒列表|cocktail list|list)(?:\s+(.*))?$", text, re.I)
    if match:
        try:
            args = _scan_conditions((match.group(1) or "").split())
        except ValueError as exc:
            return "error", [f"⚠️ {exc}"]
        if limit:
            args["limit"] = limit
        return "list", [args]
```

- [ ] **Step 6: `handle_message` 接上 error 與新的 search 簽名**

`bot.py:462-463` 從：

```python
    if command == "search":
        return fmt_cocktail_search(db_path, args[0], args[1])
```

改成：

```python
    if command == "error":
        return args[0]
    if command == "search":
        return fmt_cocktail_search(db_path, **args[0])
```

- [ ] **Step 7: 改 `fmt_cocktail_search`**

`bot.py:178-184` 從：

```python
def fmt_cocktail_search(db_path: str, keyword: str, limit: int = SEARCH_LIMIT_DEFAULT) -> str:
    limit = max(1, min(limit, RESULT_LIMIT_MAX))
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        rows = storage.search_cocktails(keyword, limit=limit)
    finally:
        storage.close()
```

改成：

```python
def fmt_cocktail_search(
    db_path: str,
    keyword: str,
    *,
    sort: str = "rating",
    desc: bool = True,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> str:
    limit = max(1, min(limit, RESULT_LIMIT_MAX))
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        rows = storage.query_cocktails(keyword=keyword, sort=sort, desc=desc, limit=limit)
    finally:
        storage.close()
```

其下的「找不到」訊息與輸出格式一行都不動。

- [ ] **Step 8: 改 `fmt_cocktail_list`**

用下列內容取代 `bot.py:304-336`（從 `def fmt_cocktail_list(` 到 `finally: storage.close()` 為止；其下的 `if not rows:` 與格式化區塊原樣保留）：

```python
_LIST_LABELS = {
    "keyword": "名稱含「{}」",
    "description": "描述含「{}」",
    "ingredient": "含有「{}」",
    "tag": "標籤「{}」",
    "min_rating": "評分 ≥ {} ★",
    "max_rating": "評分 ≤ {} ★",
    "min_abv": "ABV ≥ {}%",
    "max_abv": "ABV ≤ {}%",
    "min_count": "評分數 ≥ {}",
}
_SORT_LABELS = {
    "rating": "評分", "abv": "ABV", "calories": "卡路里",
    "date": "日期", "name": "名稱", "count": "評分數",
}


def fmt_cocktail_list(
    db_path: str,
    *,
    sort: str = "rating",
    desc: bool = True,
    limit: int = LIST_LIMIT_DEFAULT,
    **filters: Any,
) -> str:
    limit = max(1, min(limit, RESULT_LIMIT_MAX))
    active = {k: v for k, v in filters.items() if v is not None}
    title_parts = [_LIST_LABELS[k].format(v) for k, v in active.items() if k in _LIST_LABELS]

    # 沒下任何條件時沿用舊的「社群高分精選」語意：5 票門檻
    if not active:
        active["min_count"] = 5
        title_parts = ["社群高分精選"]

    title_parts.append(f"依{_SORT_LABELS[sort]}{'降序' if desc else '升序'}")
    title = "・".join(title_parts)

    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        rows = storage.query_cocktails(**active, sort=sort, desc=desc, limit=limit)
    except ValueError as exc:
        return f"⚠️ {exc}"
    finally:
        storage.close()
```

- [ ] **Step 9: 更新 `fmt_help`**

`bot.py` 的「📋 【精選與篩選】」區塊，從 `"▪ 雞尾酒列表 [N筆]",` 到 `"  篩選酒精濃度高於指定濃度的酒譜",` 換成：

```python
            "▪ 雞尾酒列表 [N筆]",
            f"  列出社群高分經典雞尾酒（預設 {LIST_LIMIT_DEFAULT} 筆，上限 {RESULT_LIMIT_MAX} 筆）",
            "▪ 條件可自由疊加：",
            "  材料 <材料>／標籤 <標籤>／描述 <關鍵字>",
            "  評分 <最低>／最高評分 <最高>",
            "  酒精濃度 <最低%>／最高酒精濃度 <最高%>",
            "▪ 排序 <評分|酒精濃度|卡路里|日期|名稱|評分數> [升序|降序]",
            "  預設依評分降序",
            "",
            "  例：雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆",
            "  例：雞尾酒列表 標籤 Classic/vintage 排序 卡路里 升序",
            "  例：雞尾酒搜尋 negroni 排序 日期",
```

- [ ] **Step 10: 跑測試確認通過**

Run: `uv run python -m pytest tests/unit/test_bot.py -q`
Expected: PASS，包含未修改的 `test_parse_cocktail_commands`

若 `test_parse_cocktail_commands` 失敗，代表向下相容破了 — 回頭檢查 `_scan_conditions` 是否多塞了 `sort`／`desc` 等預設鍵。它只該放實際出現在指令中的鍵。

- [ ] **Step 11: 全測試綠燈**

Run: `uv run python -m pytest tests/ -q`
Expected: 全部 PASS

- [ ] **Step 12: lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: 無錯誤。有 format 差異就跑 `uv run ruff format .` 後重跑上一行。

- [ ] **Step 13: 對真實 DB 手動驗一次**

```bash
uv run python -c "
import bot
db = 'diffords.db'
for cmd in [
    '雞尾酒列表 材料 gin 評分 4.2 酒精濃度 20 排序 abv 降序 5筆',
    '雞尾酒列表 標籤 Classic/vintage 排序 卡路里 升序 5筆',
    '雞尾酒搜尋 negroni 排序 日期',
    '雞尾酒列表 材料 gin',
    '雞尾酒列表 顏色 紅色',
]:
    print('>>>', cmd); print(bot.handle_message(cmd, db)); print()
"
```

Expected: 前三條各回傳排序正確的清單；第四條（舊語法）正常運作；第五條回「不認識的條件：「顏色」」加可用條件清單。

- [ ] **Step 14: 提交**

```bash
git add bot.py tests/unit/test_bot.py
git commit -m "feat(bot): 雞尾酒列表/搜尋改用 token 掃描，支援疊加條件與自選排序

新增 描述／最高評分／最高酒精濃度／排序／升序／降序 關鍵詞，
可與既有條件任意組合。舊指令為新文法的合法子集，不需並存兩套 parser。
認不得的 token 回明確錯誤而非靜默忽略。"
```

---

### Task 4: 文件同步

**Files:**
- Modify: `CLAUDE.md:16`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Task 2 與 Task 3 的最終指令語法
- Produces: 無

- [ ] **Step 1: 找出所有需要更新的位置**

Run: `grep -n "query.py list\|雞尾酒列表\|雞尾酒搜尋" README.md CLAUDE.md`
README 是雙語，中英兩份指令表都要改。

- [ ] **Step 2: 更新 CLAUDE.md**

`CLAUDE.md:16` 從：

```
uv run python query.py list --ingredient gin --rating 4.5   # also --tag, --abv, --limit
```

改成：

```
uv run python query.py list --ingredient gin --rating 4.2 --sort abv --limit 15
# 條件可疊加：--keyword --description --ingredient --tag --rating --max-rating
#             --abv --max-abv --min-count
# 排序：--sort {rating,abv,calories,date,name,count} [--asc]
```

- [ ] **Step 3: 更新 README 英文段**

`README.md:69-71` 的 CLI 範例換成：

```
uv run python query.py list --ingredient gin --rating 4.2 --sort abv --limit 15
uv run python query.py list --tag Classic/vintage --sort calories --asc
uv run python query.py list --description citrus --sort date
```

`README.md:88-94` 的指令表換成：

```markdown
| `雞尾酒列表 [N筆]` | Show top-rated recipes (default 10, max 20) |
| `雞尾酒列表 材料 <ingredient>` | Filter by ingredient |
| `雞尾酒列表 標籤 <tag>` | Filter by tag |
| `雞尾酒列表 描述 <keyword>` | Filter by description |
| `雞尾酒列表 評分 <n>` / `最高評分 <n>` | Rating lower / upper bound |
| `雞尾酒列表 酒精濃度 <n>` / `最高酒精濃度 <n>` | ABV lower / upper bound |
| `雞尾酒列表 排序 <key> [升序\|降序]` | Sort by 評分/酒精濃度/卡路里/日期/名稱/評分數 |

Conditions stack freely, e.g. `雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆`.
Any query accepts a trailing `N筆` to set the result count.
```

- [ ] **Step 4: 更新 README 中文段**

`README.md:162-163` 的 CLI 範例換成：

```
uv run python query.py list --ingredient gin --rating 4.2 --sort abv
uv run python query.py list --tag Classic/vintage --sort calories --asc
```

`README.md:173-179` 的指令表換成：

```markdown
| `雞尾酒列表 [N筆]` | 顯示高評分酒譜（預設 10 筆，上限 20 筆）|
| `雞尾酒列表 材料 <材料>` | 依材料篩選 |
| `雞尾酒列表 標籤 <標籤>` | 依標籤篩選 |
| `雞尾酒列表 描述 <關鍵字>` | 依描述篩選 |
| `雞尾酒列表 評分 <n>` / `最高評分 <n>` | 評分下限／上限 |
| `雞尾酒列表 酒精濃度 <n>` / `最高酒精濃度 <n>` | 酒精濃度下限／上限 |
| `雞尾酒列表 排序 <依據> [升序\|降序]` | 依 評分／酒精濃度／卡路里／日期／名稱／評分數 排序 |

條件可自由疊加，例如 `雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆`。
任一查詢皆可在句尾加「N筆」指定顯示筆數。
```

- [ ] **Step 5: 更新 CHANGELOG**

在 CHANGELOG 最上方新增一節：

```markdown
## 彈性排序與搜尋

- `storage`：六個寫死的查詢方法收斂為單一 `query_cocktails()`，條件可疊加、
  排序鍵與方向可選；標籤篩選改用 SQLite `json_each` 取代全表 Python 掃描。
- `query.py`：`list` 的 flag 從互斥改為可疊加，新增 `--keyword` `--description`
  `--max-rating` `--max-abv` `--min-count` `--sort` `--asc`。
- LINE bot：`雞尾酒列表` / `雞尾酒搜尋` 支援疊加條件與 `排序`／`升序`／`降序`，
  新增 `描述`／`最高評分`／`最高酒精濃度`。舊指令完全相容。
```

- [ ] **Step 6: 確認文件與程式一致**

Run: `uv run python -c "import bot; print(bot.fmt_help())"`
把輸出與 README 兩份指令表逐條對照，可用條件與排序依據的清單必須一致。

- [ ] **Step 7: 提交**

```bash
git add README.md CHANGELOG.md CLAUDE.md
git commit -m "docs: 同步彈性查詢的指令說明"
```

---

## 完成後

全測試與 lint 綠燈後，使用 superpowers:finishing-a-development-branch 決定合併方式。

注意：`.github/workflows/deploy.yml` 在 push 到 main 時會部署 bot 與 scraper。
本次改動涉及 `bot.py`，合併進 main 會觸發實際部署。
