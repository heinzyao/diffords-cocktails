# 彈性排序與搜尋設計

日期：2026-09-13
狀態：已核准，待實作

## 問題

查詢介面寫死了，不是資料或效能的問題：

| 面向 | 現況 |
|---|---|
| 搜尋 | 只比對 `cocktails.name` 的 `LIKE %kw%`，`description` 搜不到 |
| 篩選 | `query.py:110-123` 與 `bot.py:318-331` 都是 `if/elif` 互斥鏈，材料／標籤／評分／ABV 一次只能選一個 |
| 排序 | 全部硬寫死 `rating_value DESC`，不能換 key 也不能換方向 |
| 標籤篩選 | `storage.filter_by_tag()` 撈全表 6946 列進 Python 逐筆 `json.loads` 掃描 |

`storage.py` 的 `search_cocktails` / `get_top_rated` / `filter_by_ingredient` /
`filter_by_tag` / `filter_by_rating` / `filter_by_abv` 六個方法，本質是同一句 SQL
換不同 WHERE 加同一句寫死的 ORDER BY。

## 目標

1. **多條件疊加** — 材料 + 評分 + ABV + 標籤 + 描述 可同時下
2. **可選排序** — 評分／ABV／卡路里／日期／名稱／評分數，可升可降
3. **搜尋範圍擴大** — 新增可疊加的 `description` 條件

CLI 與 LINE bot 兩個介面都要吃到。

## 環境前提（已實測）

- 本機 SQLite 3.53.4；線上 `python:3.12-slim`（Debian bookworm）為 SQLite 3.40
- `NULLS LAST` 需 3.30+、`json_each` 需 3.9+ — 兩邊都成立
- `json_each(c.tags)` 遇到 271 筆 `tags IS NULL` 不會拋錯，靜默回傳零列
- 三條件疊加查詢實測 **4.8ms** / 6946 列 — 不需要新索引

## 設計

### 1. `storage.query_cocktails()` — 取代那六個方法

```python
_SORT_COLUMNS = {  # 白名單 = SQL injection 的信任邊界，不可省
    "rating":   "c.rating_value",
    "abv":      "c.abv",
    "calories": "c.calories",
    "date":     "c.date_published",
    "name":     "c.name",
    "count":    "c.rating_count",
}

def query_cocktails(
    self, *,
    keyword=None, description=None, ingredient=None, tag=None,
    min_rating=None, max_rating=None, min_abv=None, max_abv=None,
    min_count=None,
    sort="rating", desc=True, limit=20,
) -> list[dict[str, Any]]:
```

- 所有條件可疊加，`None` 者不進 WHERE
- 標籤：`EXISTS (SELECT 1 FROM json_each(c.tags) t WHERE LOWER(t.value) = LOWER(?))`
- 材料：`EXISTS (SELECT 1 FROM cocktail_ingredients ci WHERE ci.cocktail_id = c.id AND LOWER(ci.item) LIKE LOWER(?))`
- 排序：`ORDER BY {col} {DESC|ASC} NULLS LAST, c.id`
  尾端 `c.id` 是 tie-breaker，否則同分項目在不同次查詢間會跳動
- `sort` 不在白名單 → 拋 `ValueError`，由呼叫端轉成友善訊息。
  這是唯一把字串拼進 SQL 的地方，驗證不可省。
- 回傳一律經 `_attach_ingredients()`，與現況一致

刪掉 `search_cocktails` / `get_top_rated` / `filter_by_ingredient` /
`filter_by_tag` / `filter_by_rating` / `filter_by_abv`。**不留相容 wrapper** —
呼叫點只有 `query.py`、`bot.py`、`tests/unit/test_diffords.py:106`，全在改動範圍內。
`get_cocktail_by_id` / `get_cocktail_by_name` / `get_stats` 不動。

### 2. `min_count` 預設必須是 `None`（會靜默吃掉資料的坑）

現況只有 `get_top_rated`（裸列表）與 `filter_by_rating` 有 `rating_count >= 5`；
`filter_by_ingredient` / `filter_by_tag` / `filter_by_abv` **沒有**。

DB 中 `rating_count < 5 或 NULL` 有 **994 筆**。若把 `min_count=5` 設成
`query_cocktails` 的預設值，這 994 筆會從材料／標籤／ABV 查詢裡無聲消失。

因此：`min_count` 預設 `None`；由裸列表路徑（「社群高分精選」）與評分篩選路徑
明確傳入 `min_count=5`。語意上 5 票門檻屬於「高分精選」，不屬於查詢引擎。

### 3. CLI（`query.py`）

`list` 的 flag 從互斥改為可疊加；`search` 子指令保留，內部改呼叫
`query_cocktails(keyword=...)`。

```bash
uv run python query.py list --ingredient gin --rating 4.2 --abv 20 --sort abv --desc --limit 15
uv run python query.py list --tag Classic/vintage --sort calories --asc
uv run python query.py list --description citrus --sort date
```

新增 flag：`--keyword` `--description` `--max-rating` `--max-abv` `--min-count`
`--sort {rating,abv,calories,date,name,count}` `--desc/--asc`（`--desc` 為預設）。
`cmd_list` 的 `if/elif` 鏈換成收集非 `None` 的 flag 組成 kwargs，標題字串由有下
的條件組出來。

### 4. LINE bot（`bot.py`）

`parse_command()` 的「雞尾酒列表／雞尾酒搜尋」兩條從 regex 改成 token 掃描。

```
雞尾酒列表 材料 gin 評分 4.2 酒精濃度 20 排序 abv 降序 15筆
雞尾酒列表 標籤 Classic/vintage 排序 卡路里 升序
雞尾酒搜尋 negroni 排序 日期
雞尾酒列表 材料 dry vermouth 描述 citrus
```

**現有指令是新文法的合法子集**，`雞尾酒列表 材料 gin` 照舊可用，不需要並存兩套 parser。

**關鍵詞的值元數（arity）** — 這條規則消掉所有歧義：

| 關鍵詞 | 取值方式 |
|---|---|
| `材料` `標籤` `描述` | 貪婪：吃到下一個關鍵詞為止（支援 `dry vermouth` 這種多詞值） |
| `評分` `最高評分` `酒精濃度`/`abv` `最高酒精濃度` | 恰好一個 token，需可轉 float |

`評分` / `酒精濃度` 是**下限**（對應 `min_rating` / `min_abv`），沿用現有語意；
`最高評分` / `最高酒精濃度` 是上限（`max_rating` / `max_abv`）。
| `排序` | 恰好一個 token（sort key 別名） |
| `升序` `降序` | 零個 token（flag） |

`排序` 取「恰好一個 token」是必要的：`雞尾酒列表 酒精濃度 20 排序 酒精濃度`
中第二個 `酒精濃度` 是 sort key 而非新的篩選條件。

其他規則：
- `N筆` 由既有的 `_split_limit()` 在 token 掃描前先切掉。用「筆」當單位而非裸數字，
  是因為 280 款酒名以數字結尾（No. 2、Apollo 8）— 這個既有設計不可動。
- 中文關鍵詞對英文資料天然不衝突（酒名／食材／標籤皆為英文），所以貪婪取值安全。
- `雞尾酒搜尋 X ...` 視為糖衣：開頭的裸文字當 `keyword`，其餘按 `列表` 規則解析。
  沒有開頭裸文字（如 `雞尾酒搜尋 排序 abv`）→ 回錯誤，`搜尋` 必須有關鍵字。
- `雞尾酒列表` **不允許**開頭裸文字；認不得的 token 回
  `⚠️ 不認識的條件：X`（附可用條件清單），不靜默忽略。
- sort key 接受中英別名：`評分/rating`、`酒精濃度/abv`、`卡路里/calories`、
  `日期/date`、`名稱/name`、`評分數/count`。

`fmt_cocktail_list()` 簽名改成收 kwargs dict 直接轉呼叫 `query_cocktails`；
標題字串由實際下的條件組出來（例：`含有「gin」・評分 ≥ 4.2・依 ABV 降序`）。
`RESULT_LIMIT_MAX = 20` 上限不動。
`fmt_help()` 更新指令表。

## 刻意不做

**不用 FTS5。** 本機與線上都有 FTS5，但它需要一張虛擬表 + 三個 trigger 維持同步，
還要改 scraper 的寫入路徑；多欄位 `LIKE` 在 6946 列上是幾毫秒的一行 SQL。
程式碼留註解：
`# ponytail: LIKE 掃描，6946 列夠用；要相關性排序或詞幹處理再換 FTS5`

**不把 `搜尋` 擴大成全文搜。** 食材已有專用篩選，keyword 再搜食材是重複；
`review` / `history` 是長文，搜進去噪音大過訊號。改為新增可疊加的 `描述` 條件
只搜 `description` — 兩個各自單純的 filter，不需要「搜尋範圍開關」這種抽象。

**不加新索引、不加分頁、不放寬 `RESULT_LIMIT_MAX`。** 都沒有實測需求。

## 測試

新增 `tests/unit/test_query_cocktails.py`（in-memory DB + 固定資料）：

- 多條件交集正確（材料 + 評分 + ABV 同時下）
- 每個 sort key 的升序與降序皆正確
- NULL 值排在最後（`abv` / `calories` 有大量 NULL）
- 同分時以 `c.id` 穩定排序
- 非法 sort key 拋 `ValueError`
- `min_count=None` 時 `rating_count < 5` 的資料仍在結果中

`tests/unit/test_bot.py` 補 `parse_command` 的 token 掃描：

- 多條件組合、`排序` 後接與篩選同名的 token、`N筆` 與條件並存
- 舊指令（`雞尾酒列表 材料 gin`）仍解析成功 — 向下相容回歸測試
- 未知 token 回 `unknown` 或錯誤訊息

`tests/unit/test_diffords.py:106` 的 `filter_by_ingredient` 呼叫改為
`query_cocktails(ingredient=...)`。
