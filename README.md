# Distiller

[English](#english) | [繁體中文](#繁體中文)

---

## English

A Python web scraper project designed to extract liquor reviews and spirit profiles from [Distiller.com](https://distiller.com).

### Features

- Supports scraping multiple categories including Whiskey, Gin, Rum, Vodka, Brandy, Tequila/Mezcal, and Liqueurs (9 sub-styles).
- Extracts comprehensive data: scores, flavor profiles, tasting notes, etc.
- Employs a Headless Chrome mode for stability and efficiency.
- Smart pagination scraping with automatic end-condition detection.
- Automatic API endpoint discovery, significantly boosting scraping speed.
- Multiple storage backends: CSV / SQLite / Both combined.
- **Difford's Guide Scraper**: lightweight scraper (requests + BeautifulSoup, no Chrome) for cocktail recipes from diffordsguide.com — sitemap-driven incremental updates.
- **Cocktail Query System**: Search, filter, and cross-query 6000+ cocktail recipes from Difford's Guide database — by ingredient, tag, rating, ABV, or spirit availability.
- **`recipe` Bot Command**: query full cocktail recipes (ingredients, instructions, history, review) from the Difford's Guide database.
- 488 automated tests with GitHub Actions CI/CD pipelines.

### Project Structure

```text
distiller/
├── distiller_scraper/         # Core Scraper Modules
│   ├── scraper.py             # Main Scraper Class (DistillerScraperV2)
│   ├── selectors.py           # CSS Selectors & SearchURLBuilder
│   ├── config.py              # Configuration (constants, pagination logic)
│   ├── storage.py             # Storage Backends (SQLiteStorage, CSVStorage)
│   ├── api_client.py          # API Endpoint Discovery Client
│   ├── diffords_scraper.py    # Difford's Guide Scraper (requests + BeautifulSoup)
│   ├── diffords_selectors.py  # HTML / JSON-LD Extractor for Difford's Guide
│   ├── diffords_storage.py    # SQLite Storage for Difford's cocktail recipes
│   ├── diffords_config.py      # Difford's Guide Configuration Constants
│   └── flavor_parser.py       # Natural Language Flavor Preference Parser
├── data/                      # Centralized CSV Outputs (Auto-generated)
├── bot.py                     # LINE Bot (Flask webhook, port 8000)
├── run.py                     # Distiller.com Scraper Entry Point
├── run_diffords.py            # Difford's Guide Scraper Entry Point
├── query.py                   # CLI Query Tool
├── Dockerfile.scraper         # Scraper container (Chrome + Selenium)
├── Dockerfile.diffords        # Difford's scraper container (lightweight, ~200 MB)
├── Dockerfile.bot             # LINE Bot container
├── ingredient_mapping.json     # Ingredient-to-Spirit-Type Mapping (117 entries)
├── scripts/
│   ├── run_scraper.sh         # Scheduled Scraping Script (manual / Cloud Run)
│   ├── run_diffords.sh         # Difford's Scheduled Scraping Script (manual / Cloud Run)
│   └── run_bot.sh             # LINE Bot Launch Script
├── AGENTS.md                  # Multi-agent Collaboration Logs
└── CHANGELOG.md               # Changelog
```

### Installation

```bash
uv sync
```

### Execution

```bash
# Test Mode (5 items, quick validation)
python run.py --mode test

# Medium Scale (~200 items, 4 categories)
python run.py --mode medium

# Full Scrape (1000+ items, 7 categories)
python run.py --mode full
```

#### Output Formats

```bash
# CSV Output (Default), files are saved into the `data/` directory
python run.py --mode test

# SQLite Output
python run.py --mode medium --output sqlite --db-path spirits.db

# Dual CSV + SQLite Output, CSV goes to `data/`
python run.py --mode full --output both
```

#### Advanced Options

```bash
# Enable API mode (Automatic endpoint discovery, much faster)
python run.py --mode medium --use-api

# Disable Pagination, using traditional infinite scroll scraping fallback
python run.py --mode test --no-pagination

# Combinations
python run.py --mode full --output both --db-path spirits.db --use-api
```

#### Full CLI Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--mode` | Scrape mode: `test` (5 items) / `medium` (~200 items) / `full` (1000+ items) | `test` |
| `--output` | Output format: `csv` / `sqlite` / `both` | `csv` |
| `--db-path` | Path to the SQLite DB | `distiller.db` |
| `--no-pagination` | Disables pagination mode, falls back to continuous scrolling | Active |
| `--use-api` | Enables API mode (automatic endpoint discovery) | Disabled |

### LINE Bot

`bot.py` is a Flask Webhook service acting on port 8000. It receives LINE messages, queries the local SQLite spirit database, and sends replies back.

```bash
# Start Bot
uv run python bot.py

# Healthcheck
curl http://localhost:8000/health
```

Supported Commands: `烈酒排行`, `烈酒搜尋`, `烈酒詳情`, `烈酒列表`, `烈酒統計`, `烈酒風味`, `雞尾酒酒譜`, `雞尾酒搜尋`, `雞尾酒詳情`, `雞尾酒統計`, `雞尾酒列表`, `雞尾酒推薦`, `說明`

#### Difford's Guide Scraper

Scrapes cocktail recipes from [diffordsguide.com](https://www.diffordsguide.com) without Chrome (requests + BeautifulSoup).

```bash
# Incremental update (sitemap lastmod comparison)
python run_diffords.py --mode incremental

# Full scrape
python run_diffords.py --mode full

# Test run (10 recipes)
python run_diffords.py --mode test
```

#### Cocktail Query Commands (LINE Bot)

| Command | Description |
|---------|-------------|
| `雞尾酒酒譜 <name>` | Full recipe: ingredients, steps, history |
| `雞尾酒搜尋 <keyword>` | Search cocktails by name |
| `雞尾酒詳情 <name>` | Full recipe details with rating |
| `雞尾酒統計` | Database statistics |
| `雞尾酒列表 [--ingredient\|--tag\|--rating\|--abv <value>]` | Filter cocktails |
| `雞尾酒推薦` | Recommend based on your spirits |

### Testing

```bash
# Run Unit Tests and Integration Tests (Default, Fast)
uv run pytest

# Run only Unit Tests
uv run pytest tests/unit

# Run only Integration Tests
uv run pytest tests/integration

# Run all tests (Including End-to-End, Requires Network & Chrome)
uv run pytest -m ""

# Check coverage
uv run pytest -v
```

### Data Fields

| Field | Description | Example |
|-------|-------------|---------|
| `name` | Spirit Name | Highland Park 18 Year |
| `spirit_type` | Type | Single Malt |
| `brand` | Brand | Highland Park |
| `country` | Country of Origin | Scotland |
| `age` | Age | 18 Year |
| `abv` | ABV (Alcohol by Volume) | 43.0 |
| `expert_score` | Expert Rating | 99 |
| `community_score` | Community Rating | 4.47 |
| `flavor_data` | Flavor Profile (JSON) | `{"smoky": 40, "sweet": 35}` |
| `description` | Description | Rich and complex. |
| `tasting_notes` | Tasting Notes | Honey and smoke. |

### Architecture Outline

#### Scraping Workflow

```text
scrape() Starts
  ├─ start_driver()         Starts Headless Chrome
  ├─ discover_api()         [Optional] Automatically discovers API endpoints
  └─ scrape_category()      For each category
       ├─ Pagination Mode (Default)
       │    ├─ _get_search_queries()           Generates URLs for searching
       │    ├─ _fetch_spirit_urls()             Fetches results list using API/Selenium
       │    └─ scrape_spirit_detail()           Fetches spirit details using API/Selenium
       └─ Scroll Mode (--no-pagination)
            ├─ scroll_and_collect()             Collect URLs via infinite scrolling
            └─ scrape_spirit_detail()           Scrapes items one by one
```

#### 3-Tier Fallback Mechanism

When using `--use-api`, the system will automatically utilize backoff and logic cascading:

1. **API Mode** (Fastest): Direct requests to unauthenticated JSON API endpoints.
2. **Selenium Mode** (Reliable): Uses automated browser emulation.

API Discovery Process:
- Intercepts and parses XHR Network requests in Chrome Performance Log.
- Analyzes candidate payloads (such as `/search.json`, `/api/v1/spirits/search`).
- Probes and validates whether the found endpoints return functional JSON.

#### Storage Backends

| Backend | Applicable Scenarios |
|---------|----------------------|
| `CSVStorage` | Fast exports, tabular data analysis |
| `SQLiteStorage` | Deduplication (upsert), joins/querying, persistent storage |

The SQLite schema involves:
- `spirits` Primary table (All fields + timestamps)
- `flavor_profiles` Secondary table (Normalized flavor vectors)
- `scrape_runs` Execution logs table

#### Scheduling and Deduplication Pipeline

Scheduling is handled exclusively by **Cloud Run** via **Cloud Scheduler** for each scraper (Distiller and Difford's Guide). Run intervals are controlled entirely by the scheduler, with no in-process time-window guard.
To ensure idempotence and prevent overlapping data, a 6-layer deduplication logic is applied:

| Layer | Mechanism | Description |
|-------|-----------|-------------|
| 1 | `seen_urls` preloading | Upon boot, all collected URLs form an in-memory Set from SQLite |
| 2 | Item-level bypass | Before firing HTTP requests, verifies URL existence against `seen_urls` |
| 3 | First-page early stop | Page 1 duplicate rate ≥ 95% → skip entire category immediately (no further pages loaded) |
| 4 | Duplication density | From page 2, paginating stops if the duplicate hitrate reaches ≥ 80% |
| 5 | Consecutive duplicate pages | After 3 consecutive all-duplicate pages, stops pagination |
| 6 | SQLite Upsert | Uses `UNIQUE` URL constraints; writes invoke an `ON CONFLICT` update |

> **Note**: CSV output is handled inside an isolated scope and is distinct across runs inside the `data/` folder (only deduplicated intra-scrape). `SQLite` acts as the persistent global deduplication source constraint.

### Collaboration Notice

This project is built using a collaborative workflow of several AI agents. See [AGENTS.md](AGENTS.md).

### Caveats and Constraints

- Distiller.com populates its DOM dynamically through JavaScript; hence `Selenium` is required.
- Please adhere to the terms and conditions outlined by the site owner. Keep request intervals reasonable without aggressive polling.
- Make sure a standard Chrome browser is installed. The script leverages `webdriver-manager` to ensure the ChromeDriver is matching.

### License

MIT

---

## 繁體中文

從 [Distiller.com](https://distiller.com) 爬取烈酒評論資料的 Python 爬蟲專案。

### 功能特點

- 支援 Whiskey、Gin、Rum、Vodka、Brandy、Tequila/Mezcal、Liqueurs（9 個子風格）等多類別爬取
- 提取完整資料：評分、風味圖譜、品飲筆記等
- Headless Chrome 模式，穩定高效
- 智能分頁爬取，自動偵測停止條件
- API 端點自動探索，大幅提升爬取速度
- 多儲存後端：CSV / SQLite / 雙輸出
- **Difford's Guide 爬蟲**：輕量爬蟲（requests + BeautifulSoup，無需 Chrome），從 diffordsguide.com 爬取雞尾酒酒譜，Sitemap 驅動增量更新
- **調酒查詢系統**：搜尋、篩選、交叉查詢 Difford's Guide 資料庫 6000+ 雞尾酒酒譜 — 依材料、標籤、評分、ABV 或可用烈酒
- **`酒譜` Bot 指令**：查詢 Difford's Guide 資料庫的完整酒譜（食材、作法、歷史、評語）
- 488 個自動化測試，GitHub Actions CI/CD

### 專案結構

```text
distiller/
├── distiller_scraper/         # 核心爬蟲模組
│   ├── scraper.py             # 主爬蟲類別 DistillerScraperV2
│   ├── selectors.py           # CSS 選擇器 & SearchURLBuilder
│   ├── config.py              # 爬蟲配置（含分頁常數）
│   ├── storage.py             # 儲存後端 (SQLiteStorage, CSVStorage)
│   ├── api_client.py          # API 端點探索客戶端
│   ├── diffords_scraper.py    # Difford's Guide 爬蟲（requests + BeautifulSoup）
│   ├── diffords_selectors.py  # Difford's HTML / JSON-LD 資料擷取器
│   ├── diffords_storage.py    # Difford's 雞尾酒酒譜 SQLite 儲存層
│   ├── diffords_config.py      # Difford's Guide 設定常數
│   └── flavor_parser.py       # 自然語言風味偏好解析器
├── data/                      # CSV 輸出集中處（自動建立）
├── bot.py                     # LINE Bot（Flask webhook，port 8000）
├── run.py                     # Distiller.com 爬蟲進入點
├── run_diffords.py            # Difford's Guide 爬蟲進入點
├── query.py                   # CLI 查詢工具
├── Dockerfile.scraper         # 爬蟲容器（Chrome + Selenium，~800 MB）
├── Dockerfile.diffords        # Difford's 爬蟲容器（輕量，~200 MB）
├── Dockerfile.bot             # LINE Bot 容器
├── ingredient_mapping.json     # 材料-烈酒類型對應表（117 筆）
├── scripts/
│   ├── run_scraper.sh         # 排程爬取腳本（手動執行 / Cloud Run）
│   ├── run_diffords.sh         # Difford's 排程爬取腳本（手動執行 / Cloud Run）
│   └── run_bot.sh             # Bot 啟動腳本
├── AGENTS.md                  # 多代理協作紀錄
└── CHANGELOG.md               # 變更紀錄
```

### 安裝

```bash
uv sync
```

### 執行

```bash
# 測試模式（5 筆，快速驗證）
python run.py --mode test

# 中等規模（~200 筆，4 個類別）
python run.py --mode medium

# 完整爬取（1000+ 筆，7 個類別）
python run.py --mode full
```

#### 輸出格式

```bash
# CSV 輸出（預設），檔案儲存於 data/ 目錄
python run.py --mode test

# SQLite 輸出
python run.py --mode medium --output sqlite --db-path spirits.db

# CSV + SQLite 雙輸出，CSV 儲存於 data/
python run.py --mode full --output both
```

#### 進階選項

```bash
# 啟用 API 模式（自動探測端點，速度更快）
python run.py --mode medium --use-api

# 停用分頁，改用傳統滾動爬取
python run.py --mode test --no-pagination

# 組合使用
python run.py --mode full --output both --db-path spirits.db --use-api
```

#### 完整 CLI 參數

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `--mode` | 爬取模式：`test` (5 筆) / `medium` (~200 筆) / `full` (1000+ 筆) | `test` |
| `--output` | 輸出格式：`csv` / `sqlite` / `both` | `csv` |
| `--db-path` | SQLite 資料庫路徑 | `distiller.db` |
| `--no-pagination` | 停用分頁模式，改用傳統滾動爬取 | 啟用分頁 |
| `--use-api` | 啟用 API 模式（自動探測端點） | 停用 |

### LINE Bot

`bot.py` 是一個 Flask Webhook 服務（port 8000），接收 LINE 訊息並查詢烈酒資料庫回覆。

```bash
# 啟動 Bot
uv run python bot.py

# 健康檢查
curl http://localhost:8000/health
```

支援指令：`烈酒排行`、`烈酒搜尋`、`烈酒詳情`、`烈酒列表`、`烈酒統計`、`烈酒風味`、`雞尾酒酒譜`、`雞尾酒搜尋`、`雞尾酒詳情`、`雞尾酒統計`、`雞尾酒列表`、`雞尾酒推薦`、`說明`

#### Difford's Guide 爬蟲

從 [diffordsguide.com](https://www.diffordsguide.com) 爬取雞尾酒酒譜，不需要 Chrome（requests + BeautifulSoup）。

```bash
# 增量更新（比對 sitemap lastmod，預設模式）
python run_diffords.py --mode incremental

# 全量爬取
python run_diffords.py --mode full

# 測試模式（僅爬 10 筆）
python run_diffords.py --mode test
```

#### 調酒查詢指令（LINE Bot）

| 指令 | 說明 |
|------|------|
| `雞尾酒酒譜 <酒名>` | 完整酒譜：食材、作法、歷史 |
| `雞尾酒搜尋 <關鍵字>` | 名稱模糊搜尋 |
| `雞尾酒詳情 <名稱>` | 完整酒譜詳情與評分 |
| `雞尾酒統計` | 資料庫統計概覽 |
| `雞尾酒列表 [--ingredient\|--tag\|--rating\|--abv <值>]` | 篩選查詢 |
| `雞尾酒推薦` | 根據已有烈酒推薦可調製項目 |

### 測試

```bash
# 執行單元測試與整合測試（預設，快速）
uv run pytest

# 只執行單元測試
uv run pytest tests/unit

# 只執行整合測試
uv run pytest tests/integration

# 執行所有測試（包含 E2E，需要網路與 Chrome）
uv run pytest -m ""

# 查看覆蓋率
uv run pytest -v
```

### 資料欄位

| 欄位 | 說明 | 範例 |
|------|------|------|
| `name` | 品名 | Highland Park 18 Year |
| `spirit_type` | 類型 | Single Malt |
| `brand` | 品牌 | Highland Park |
| `country` | 產地 | Scotland |
| `age` | 年份 | 18 Year |
| `abv` | 酒精濃度 | 43.0 |
| `expert_score` | 專家評分 | 99 |
| `community_score` | 社群評分 | 4.47 |
| `flavor_data` | 風味圖譜 (JSON) | `{"smoky": 40, "sweet": 35}` |
| `description` | 描述 | Rich and complex. |
| `tasting_notes` | 品飲筆記 | Honey and smoke. |

### 架構說明

#### 爬取流程

```text
scrape() 啟動
  ├─ start_driver()         啟動 Headless Chrome
  ├─ discover_api()         [可選] 自動探測 API 端點
  └─ scrape_category()      每個類別
       ├─ 分頁模式 (預設)
       │    ├─ _get_search_queries()           產生搜尋 URL
       │    ├─ _fetch_spirit_urls()             API 或 Selenium 取得列表
       │    └─ scrape_spirit_detail()           API 或 Selenium 取得詳情
       └─ 滾動模式 (--no-pagination)
            ├─ scroll_and_collect()             無限滾動收集 URL
            └─ scrape_spirit_detail()           逐筆爬取詳情
```

#### 三層備援機制

當啟用 `--use-api` 時，系統自動嘗試：

1. **API 模式**（最快）：直接呼叫 JSON API 端點
2. **Selenium 模式**（可靠）：透過瀏覽器自動化爬取

API 端點探索流程：
- 擷取 Chrome Performance Log 中的 XHR 請求
- 分析候選路徑（`/search.json`, `/api/v1/spirits/search` 等）
- 自動驗證端點是否回傳有效 JSON

#### 儲存後端

| 後端 | 適用場景 |
|------|----------|
| `CSVStorage` | 快速匯出、資料分析 |
| `SQLiteStorage` | 去重（upsert）、關聯查詢、持久化儲存 |

SQLite schema 包含：
- `spirits` 主表（所有欄位 + 時間戳）
- `flavor_profiles` 副表（正規化風味資料）
- `scrape_runs` 執行記錄表

#### 排程與去重機制

排程統一由 **Cloud Run** 透過 **Cloud Scheduler** 管理，Distiller 爬蟲與 Difford's Guide 爬蟲各有獨立排程。執行間隔完全由排程器控制，程式本身不設時間窗口限制。
系統透過六層去重機制確保不會重複爬取已有資料：

| 層級 | 機制 | 說明 |
|------|------|------|
| 1 | `seen_urls` 預載 | 啟動時從 SQLite 載入所有已存 URL 至記憶體 |
| 2 | 逐筆跳過 | 爬取詳情前檢查 `seen_urls`，已存在則跳過（不發 HTTP 請求）|
| 3 | 首頁早停 | 第 1 頁重複率 ≥ 95% 時直接跳過整個類別（不再載入後續頁面）|
| 4 | 重複率閾值 | 第 2 頁起重複率 ≥ 80% 時自動停止分頁 |
| 5 | 連續重複頁 | 連續 3 頁全為重複 URL 時停止分頁 |
| 6 | SQLite upsert | URL 為 `UNIQUE` 欄位，重複寫入時更新而非新增 |

> **注意**：CSV 輸出為每次執行獨立檔案，儲存於 `data/` 目錄，僅在單次執行內去重。SQLite 為跨執行的唯一去重來源。

### 協作說明

本專案由多個 AI 代理協作開發，詳見 [AGENTS.md](AGENTS.md)。

### 注意事項

- Distiller.com 使用 JavaScript 動態載入，需使用 Selenium
- 請遵守網站使用條款，合理控制爬取頻率
- 需要 Chrome 瀏覽器（webdriver-manager 會自動管理 ChromeDriver）

### 授權條款

MIT
