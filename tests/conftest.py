import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 測試必須與本機 .env 隔離。bot.py 在 import 時就 load_dotenv() 並把
# GCS_BUCKET 讀進模組層級變數，不先清掉的話 _ensure_db_from_gcs 會真的去
# GCS 下載 diffords.db —— 測試在本機通過、在 CI（沒有 .env）失敗，而且
# 跑一次單元測試要抓 21 MB。
#
# 設成空字串而不是 pop：load_dotenv 預設不覆蓋已存在的鍵，pop 掉反而會讓
# .env 的值補回來。需要金鑰的測試自己用 monkeypatch.setenv 覆蓋。
for _leaky in ("GCS_BUCKET", "GEMINI_API_KEY"):
    os.environ[_leaky] = ""
