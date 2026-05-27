"""
Difford's Guide 爬蟲專用配置檔

設計理由
--------
採用模組級常數而非類別設定，原因：
- 爬蟲參數在執行期間不需要動態修改（非 mutable state）
- 模組級常數搭配 import 語句更清楚
- 集中管理 Difford's Guide 相關常數，避免分散至多個檔案
"""

import os

# ── 爬蟲延遲設定 ──
# 尊重網站頻率限制，避免被視為異常流量
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 4.0

# ── Sitemap 設定 ──
SITEMAP_URL = "https://www.diffordsguide.com/sitemap/cocktail.xml"

# ── HTTP 請求 Header ──
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── 資料庫設定 ──
DIFFORDS_DB_DEFAULT = "diffords.db"
DIFFORDS_DB_PATH = "diffords.db"

# ── 通知設定 ──
DIFFORDS_NOTIFY_SOURCE = "Difford's Guide"

# ── GCS 設定 ──
# 在 Cloud Run 環境中使用 GCS 儲存，本機開發時使用本地檔案系統
GCS_DIFFORDS_DB_BLOB = os.getenv("GCS_DIFFORDS_DB_BLOB", "diffords.db")
