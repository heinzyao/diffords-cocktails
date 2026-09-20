# Difford's Guide Cocktails

[English](#english) | [繁體中文](#繁體中文)

---

## English

A Python data pipeline for collecting and querying cocktail recipes from [Difford's Guide](https://www.diffordsguide.com).

### What It Does

- Scrapes Difford's Guide cocktail recipe pages from the public cocktail sitemap.
- Parses recipe metadata from JSON-LD and supplements it with static HTML fields.
- Stores recipes, ingredients, ratings, ABV, glassware, garnish, instructions, review, history, and sitemap `lastmod` values in SQLite.
- Supports incremental updates by comparing sitemap `lastmod` values with the local database.
- Provides a CLI query tool and a LINE Bot for recipe lookup.
- Deploys as a lightweight Cloud Run scraper job and a Cloud Run bot service.

### Project Structure

```text
.
├── diffords_guide/
│   ├── config.py          # Difford's Guide constants and GCS defaults
│   ├── scraper.py         # Sitemap-driven scraper
│   ├── selectors.py       # JSON-LD and HTML extraction
│   ├── storage.py         # SQLite schema, writes, and queries
│   ├── gcs_storage.py     # Cloud Run DB sync helpers
│   └── notify.py          # LINE push notifications for scraper runs
├── bot.py                 # LINE webhook service
├── query.py               # Local CLI query tool
├── run_diffords.py        # Scraper entry point
├── Dockerfile.diffords    # Scraper container
├── Dockerfile.bot         # Bot container
└── scripts/
    ├── run_diffords.sh
    └── deploy_gcp.sh
```

### Install

```bash
uv sync
```

### Scrape

```bash
uv run python run_diffords.py --mode test
uv run python run_diffords.py --mode incremental
uv run python run_diffords.py --mode full
```

Options:

| Argument | Description | Default |
|---|---|---|
| `--mode` | `test`, `incremental`, or `full` | `incremental` |
| `--db-path` | SQLite database path | `diffords.db` |
| `--notify-line` | Send LINE push notification after the run | disabled |

### Query

```bash
uv run python query.py stats
uv run python query.py search negroni
uv run python query.py info "Negroni"
uv run python query.py list --ingredient gin --rating 4.2 --sort abv --limit 15
uv run python query.py list --tag Classic/vintage --sort calories --asc
uv run python query.py list --description citrus --sort date
```

### LINE Bot

```bash
uv run python bot.py
curl http://localhost:8000/health
```

Supported message commands:

| Command | Description |
|---|---|
| `雞尾酒搜尋 <keyword> [N筆]` | Search cocktails by name (default 5, max 20) |
| `雞尾酒酒譜 <name>` | Show ingredients and instructions |
| `雞尾酒詳情 <name>` | Alias for full recipe lookup |
| `雞尾酒列表 [N筆]` | Show top-rated recipes (default 10, max 20) |
| `雞尾酒列表 材料 <ingredient>` | Filter by ingredient |
| `雞尾酒列表 標籤 <tag>` | Filter by tag |
| `雞尾酒列表 描述 <keyword>` | Filter by description |
| `雞尾酒列表 評分 <n>` / `最高評分 <n>` | Rating lower / upper bound |
| `雞尾酒列表 酒精濃度 <n>` / `最高酒精濃度 <n>` | ABV lower / upper bound |
| `雞尾酒列表 排序 <key> [升序\|降序]` | Sort by 評分/酒精濃度/卡路里/日期/名稱/評分數 |
| `雞尾酒統計` | Show database stats |
| `雞尾酒爬蟲 <test\|incremental\|full>` | Trigger scraper |
| `狀態` | Show scraper status |
| `說明` | Show help |

Conditions stack freely, e.g. `雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆`.

**Natural-language queries.** Anything that doesn't match a command above is sent to
Gemini, which turns it into the same filters (e.g. 睡前喝的，3筆 → tag
`Nightcap/sipping`, limit 3). Requires `GEMINI_API_KEY`; without it, or if parsing
fails, the bot falls back to the usual "unrecognised command" reply. Known commands
never hit the API.
Any query accepts a trailing `N筆` to set the result count.

### Data Model

SQLite tables:

- `cocktails`: one row per Difford's Guide recipe.
- `cocktail_ingredients`: ordered ingredients for each recipe.
- `diffords_scrape_runs`: scraper run history.

Primary recipe fields:

| Field | Description |
|---|---|
| `id` | Difford's Guide recipe ID |
| `name` | Cocktail name |
| `description` | Recipe description |
| `glassware` | Glass type |
| `garnish` | Garnish |
| `prepare` | Preparation notes |
| `instructions` | Method |
| `review` | Difford's review |
| `history` | Historical notes |
| `tags` | JSON encoded tags |
| `rating_value`, `rating_count` | Rating summary |
| `calories`, `abv` | Nutrition/alcohol metadata |
| `url`, `lastmod` | Source URL and sitemap update marker |

### Tests

```bash
uv run pytest
```

---

## 繁體中文

這是一個以 [Difford's Guide](https://www.diffordsguide.com) 雞尾酒酒譜為核心的 Python 資料管線。

### 功能

- 從 Difford's Guide cocktail sitemap 收集酒譜 URL。
- 以 JSON-LD 為主要資料來源，靜態 HTML 為補充來源。
- 儲存酒譜、食材、評分、ABV、杯型、裝飾、作法、評語、歷史與 sitemap `lastmod`。
- 透過 `lastmod` 比對做增量更新。
- 提供本機 CLI 查詢工具與 LINE Bot。
- 部署時只需要輕量 Cloud Run 爬蟲 Job 與 Bot Service。

### 執行

```bash
uv sync
uv run python run_diffords.py --mode test
uv run python query.py search negroni
uv run python bot.py
```

### CLI 範例

```bash
uv run python query.py stats
uv run python query.py info "Negroni"
uv run python query.py list --ingredient gin --rating 4.2 --sort abv
uv run python query.py list --tag Classic/vintage --sort calories --asc
```

### LINE Bot 指令

| 指令 | 說明 |
|---|---|
| `雞尾酒搜尋 <關鍵字> [N筆]` | 搜尋雞尾酒名稱（預設 5 筆，上限 20 筆）|
| `雞尾酒酒譜 <名稱>` | 顯示食材與作法 |
| `雞尾酒詳情 <名稱>` | 顯示完整酒譜 |
| `雞尾酒列表 [N筆]` | 顯示高評分酒譜（預設 10 筆，上限 20 筆）|
| `雞尾酒列表 材料 <材料>` | 依材料篩選 |
| `雞尾酒列表 標籤 <標籤>` | 依標籤篩選 |
| `雞尾酒列表 描述 <關鍵字>` | 依描述篩選 |
| `雞尾酒列表 評分 <n>` / `最高評分 <n>` | 評分下限／上限 |
| `雞尾酒列表 酒精濃度 <n>` / `最高酒精濃度 <n>` | 酒精濃度下限／上限 |
| `雞尾酒列表 排序 <依據> [升序\|降序]` | 依 評分／酒精濃度／卡路里／日期／名稱／評分數 排序 |
| `雞尾酒統計` | 顯示資料庫摘要 |
| `雞尾酒爬蟲 <test\|incremental\|full>` | 啟動爬蟲 |
| `狀態` | 查看爬蟲狀態 |
| `說明` | 顯示指令 |

條件可自由疊加，例如 `雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆`。

**自然語言查詢。** 不符合上述任何指令的訊息，會交給 Gemini 轉成同一組篩選條件
（例如「睡前喝的，3筆」→ 標籤 `Nightcap/sipping`、3 筆）。需要 `GEMINI_API_KEY`；
未設定或解析失敗時，回覆與過去一樣的「無法識別此指令」。已知指令完全不會呼叫 API。
任一查詢皆可在句尾加「N筆」指定顯示筆數。

### 已移除範圍

本專案不再包含舊版烈酒評論資料、瀏覽器自動化爬蟲、烈酒風味圖譜或以個人烈酒收藏推導可調製酒譜的功能。
