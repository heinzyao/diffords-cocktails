#!/usr/bin/env python3

"""
Distiller LINE Bot

收到 LINE 訊息後查詢 SQLite 資料庫並回覆結果。

啟動方式：
    python bot.py

本機開發需透過 ngrok 建立公開 URL：
    ngrok http 5000
    → 將 Webhook URL 設定於 LINE Developers Console

支援指令（直接在 LINE 傳送）：
    烈酒排行 [N]           評分 Top N（預設 10）
    烈酒搜尋 <關鍵字>      搜尋品名、品牌、描述
    烈酒詳情 <名稱>        單筆完整資訊（含風味圖譜）
    烈酒統計              資料庫統計摘要
    烈酒風味              所有風味維度排行
    烈酒風味 <名稱>        特定風味最強排行
    烈酒列表              列出所有（前 10 筆）
    烈酒列表 <產地>        依產地篩選
    烈酒列表 <產地> <分數>  依產地與最低分數篩選
    雞尾酒酒譜 <酒名>      食材、作法、歷史
    雞尾酒搜尋 <關鍵字>    搜尋雞尾酒名稱
    雞尾酒詳情 <名稱>      完整酒譜與評分
    雞尾酒統計            雞尾酒資料庫摘要
    雞尾酒列表            篩選調酒
    雞尾酒推薦            根據收藏推薦可調製項目
    說明                  顯示指令說明
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import cast


import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

_ = load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from distiller_scraper.diffords_config import DIFFORDS_DB_DEFAULT, GCS_DIFFORDS_DB_BLOB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# GCS 設定：設定 GCS_BUCKET 時啟用雲端模式，否則維持本機行為
GCS_BUCKET = os.getenv("GCS_BUCKET", "")
GCS_DB_BLOB = os.getenv("GCS_DB_BLOB", "distiller.db")

LINE_TOKEN_URL = "https://api.line.me/v2/oauth/accessToken"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
DB_DEFAULT = "distiller.db"
MSG_LIMIT = 4900  # LINE 單則訊息字元上限（官方上限 5000，保留 100 字元緩衝）

# 視覺元素：統一的分隔線與獎牌圖示，讓 LINE 訊息格式一致且易讀
_SEP = "━━━━━━━━━━━━━━"
_SEP_LIGHT = "──────────────"
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}  # Top 3 排行獎牌


def _truncate(text: str, max_len: int) -> str:
    return text[:max_len] + "…" if len(text) > max_len else text


# Access Token 快取：避免每次 Webhook 請求都重新取得 Token
# 結構：{"token": str, "expires_at": float（UNIX timestamp）}
# 設計理由：LINE Channel Access Token 有效期 30 天，但短期 token 有效期約 30 天
# 此處使用 23 小時 TTL（82800 秒），在到期前 60 秒自動更新（_get_cached_token 邏輯）
_token_cache: dict[str, str | float] = {}

# 爬蟲執行狀態：追蹤背景執行的爬蟲進程
_scrape_lock = threading.Lock()
_scrape_state: dict = {
    "running": False,
    "mode": None,
    "started_at": None,
}

# Difford's Guide 爬蟲執行狀態
_diffords_lock = threading.Lock()
_diffords_state: dict = {
    "running": False,
    "mode": None,
    "started_at": None,
}


# ---------------------------------------------------------------------------
# LINE API 輔助
# ---------------------------------------------------------------------------


def _get_access_token(channel_id: str, channel_secret: str) -> str | None:
    """用 Channel ID + Secret 取得短期 Access Token。"""
    try:
        resp = requests.post(
            LINE_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": channel_id,
                "client_secret": channel_secret,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                token = data.get("access_token")
                if isinstance(token, str):
                    return token
            return None
        logger.warning("Token 取得失敗：%s", resp.text)
        return None
    except requests.RequestException as exc:
        logger.error("Token 請求失敗：%s", exc)
        return None


def _get_cached_token(channel_id: str, channel_secret: str) -> str | None:
    """取得有效的 Access Token（優先使用快取，逾期前 60 秒自動更新）。"""
    now = time.time()
    token = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at", 0.0)
    if isinstance(token, str) and isinstance(expires_at, (int, float)):
        if now < expires_at - 60:
            return token
    token = _get_access_token(channel_id, channel_secret)
    if token:
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + 82800  # 23 小時 TTL
    return token


def _verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """驗證 LINE Webhook 簽名（HMAC-SHA256）。"""
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == signature


def _reply(reply_token: str, text: str, access_token: str) -> bool:
    """透過 Reply API 回覆訊息（自動拆分超長訊息，最多 5 則）。"""
    chunks = [text[i : i + MSG_LIMIT] for i in range(0, len(text), MSG_LIMIT)][:5]
    messages = [{"type": "text", "text": chunk} for chunk in chunks]
    try:
        resp = requests.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"replyToken": reply_token, "messages": messages},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Reply 失敗：%s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Reply 請求失敗：%s", exc)
        return False


def _launch_scraper_thread(
    cmd: list[str], lock: threading.Lock, state: dict
) -> None:
    """在背景執行緒中執行爬蟲指令；完成後清除執行狀態。"""
    def _run() -> None:
        try:
            subprocess.run(cmd, capture_output=False)
        finally:
            with lock:
                state["running"] = False
                state["mode"] = None

    threading.Thread(target=_run, daemon=True).start()


def _launch_cloud_run_job(
    job_name_env: str, default_job: str, cmd_args: list[str]
) -> None:
    """觸發 Cloud Run Job（Cloud Run 環境專用）。"""
    project = os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCLOUD_PROJECT", ""))
    region = os.getenv("CLOUD_RUN_REGION", "asia-east1")
    job_name = os.getenv(job_name_env, default_job)

    from google.cloud import run_v2  # type: ignore[import]

    client = run_v2.JobsClient()
    name = f"projects/{project}/locations/{region}/jobs/{job_name}"
    overrides = run_v2.RunJobRequest.Overrides(
        container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(args=cmd_args)
        ]
    )
    client.run_job(request=run_v2.RunJobRequest(name=name, overrides=overrides))
    logger.info("Cloud Run Job 已觸發：%s", name)


def _start_scraper(mode: str, db_path: str) -> None:
    """啟動 Distiller 爬蟲；狀態已由呼叫端在 lock 內設定。"""
    if GCS_BUCKET and os.getenv("GOOGLE_CLOUD_PROJECT"):
        _launch_cloud_run_job(
            "SCRAPER_JOB_NAME",
            "distiller-scraper",
            ["--mode", mode, "--output", "sqlite", "--use-api", "--notify-line"],
        )
    else:
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "run.py"),
            "--mode", mode, "--output", "sqlite", "--db-path", db_path, "--notify-line",
        ]
        _launch_scraper_thread(cmd, _scrape_lock, _scrape_state)


def _start_diffords(mode: str, db_path: str) -> None:
    """啟動 Difford's 爬蟲；狀態已由呼叫端在 lock 內設定。"""
    if GCS_BUCKET and os.getenv("GOOGLE_CLOUD_PROJECT"):
        _launch_cloud_run_job(
            "DIFFORDS_JOB_NAME",
            "distiller-diffords",
            ["--mode", mode, "--notify-line"],
        )
    else:
        cmd = [
            sys.executable,
            str(Path(__file__).parent / "run_diffords.py"),
            "--mode", mode, "--db-path", db_path, "--notify-line",
        ]
        _launch_scraper_thread(cmd, _diffords_lock, _diffords_state)


# GCS 資料庫更新檢查：記錄各 DB 最後與 GCS 比對的時間（UNIX timestamp）
_db_gcs_checked: dict[str, float] = {}
_DB_GCS_CHECK_INTERVAL = 300  # 每 5 分鐘最多檢查一次 GCS


def _ensure_db_from_gcs(db_path: str, blob_name: str = GCS_DB_BLOB) -> bool:
    """確保本地資料庫為最新版本。

    行為：
    1. 本地 DB 不存在 → 從 GCS 下載
    2. 本地 DB 存在且距離上次檢查不足 5 分鐘 → 直接使用本地
    3. 本地 DB 存在但超過 5 分鐘 → 比對 GCS blob 更新時間，若 GCS 較新則重新下載
    """
    if not GCS_BUCKET:
        return Path(db_path).exists()

    from distiller_scraper import gcs_storage

    if Path(db_path).exists():
        # 節流：避免每次請求都查詢 GCS metadata
        now = time.time()
        last_checked = _db_gcs_checked.get(db_path, 0.0)
        if now - last_checked < _DB_GCS_CHECK_INTERVAL:
            return True

        # 比對 GCS blob 時間戳與本地檔案 mtime
        blob_updated = gcs_storage.get_blob_updated_time(GCS_BUCKET, blob_name)
        _db_gcs_checked[db_path] = now  # 無論結果，都標記為已檢查

        if blob_updated is None:
            # 無法取得 GCS 資訊（網路問題等），使用本地版本
            return True

        local_mtime = Path(db_path).stat().st_mtime
        if blob_updated.timestamp() > local_mtime:
            logger.info(
                "GCS 版本較新，重新下載：%s（GCS: %s, 本地: %s）",
                db_path,
                blob_updated.isoformat(),
                datetime.fromtimestamp(local_mtime).isoformat(),
            )
            return gcs_storage.download_db(GCS_BUCKET, blob_name, db_path)

        return True

    # 本地 DB 不存在，從 GCS 下載
    logger.info("資料庫不存在，嘗試從 GCS 下載：%s", db_path)
    result = gcs_storage.download_db(GCS_BUCKET, blob_name, db_path)
    if result:
        _db_gcs_checked[db_path] = time.time()
    return result


# ---------------------------------------------------------------------------
# 資料庫查詢（回傳字串，不 print）
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _score_bar(score: int | None, width: int = 8) -> str:
    if score is None:
        return ""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_score(score_value: int | float | None) -> str:
    bar = _score_bar(
        int(score_value) if isinstance(score_value, (int, float)) else None
    )
    return f"{bar} {score_value}分" if score_value else "（暫無評分）"


def fmt_top(db_path: str, n: int = 10) -> str:
    conn = _connect(db_path)
    rows = cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT name, spirit_type, country, expert_score FROM spirits "
            "WHERE expert_score IS NOT NULL ORDER BY expert_score DESC LIMIT ?",
            (n,),
        ).fetchall(),
    )
    rows = [dict(r) for r in rows]
    conn.close()
    if not rows:
        return "📭 資料庫目前沒有符合的評分資料。"
    lines: list[str] = [f"🏆 Distiller 專家評分榜 Top {n}", _SEP]
    for i, r in enumerate(rows, 1):
        rank = _MEDALS.get(i, f"{i:>2}.")
        score = r.get("expert_score")
        score_val = int(score) if isinstance(score, (int, float)) else 0
        lines.append(f"{rank} 【{r['name']}】")
        lines.append(f"   └ 🥃 {r['spirit_type']} ({score_val}分)")
        lines.append(f"   └ 🌍 {r['country']}")
        lines.append("")
    lines.append("💡 傳送『詳情 <酒名>』查看風味圖譜")
    return "\n".join(lines)


def fmt_search(db_path: str, keyword: str, limit: int = 10) -> str:
    conn = _connect(db_path)
    kw = f"%{keyword}%"
    rows = cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT name, spirit_type, country, expert_score FROM spirits "
            "WHERE name LIKE ? OR brand LIKE ? OR description LIKE ? "
            "ORDER BY expert_score DESC NULLS LAST LIMIT ?",
            (kw, kw, kw, limit),
        ).fetchall(),
    )
    rows = [dict(r) for r in rows]
    conn.close()
    if not rows:
        return f"❓ 找不到符合「{keyword}」的烈酒，請嘗試縮短關鍵字。"
    lines: list[str] = [f"🔍 搜尋「{keyword}」：{len(rows)} 筆", _SEP_LIGHT]
    for r in rows:
        score = r.get("expert_score")
        score_val = f" ({int(score)}分)" if isinstance(score, (int, float)) else ""
        lines.append(f"・【{r['name']}】")
        lines.append(f"  └ 🥃 {r['spirit_type']}{score_val}")
        lines.append(f"  └ 🌍 {r['country']}")
        lines.append("")
    lines.append("💡 傳送『詳情 <酒名>』了解更多")
    return "\n".join(lines)


def fmt_info(db_path: str, name: str) -> str:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM spirits WHERE name LIKE ? LIMIT 1", (f"%{name}%",)
    ).fetchone()
    if not row:
        conn.close()
        return f"❓ 找不到名稱包含「{name}」的烈酒。"
    row = dict(row)
    cost_level = row.get("cost_level")
    price_text = "$" * cost_level if isinstance(cost_level, int) else "—"
    score_value = row.get("expert_score")
    score_text = score_value if isinstance(score_value, (int, float)) else "—"
    score_bar = _score_bar(
        int(score_value) if isinstance(score_value, (int, float)) else None
    )

    # 依類型決定圖示
    s_type = (row.get("spirit_type") or "").lower()
    icon = "🥃"
    if "gin" in s_type: icon = "🧊"
    elif "vodka" in s_type: icon = "❄️"
    elif "rum" in s_type: icon = "🏴‍☠️"
    elif "tequila" in s_type: icon = "🌵"
    elif "brandy" in s_type or "cognac" in s_type: icon = "🍷"

    lines: list[str] = [
        f"✨ 【{row['name']}】 ✨",
        _SEP,
        "📜 基本資料",
        f"  • 類型：{row.get('spirit_type') or '—'}",
        f"  • 品牌：{row.get('brand') or '—'} / {row.get('age') or 'NAS'}",
        f"  • 規格：{row.get('abv') or '—'}% ABV / {price_text}",
        f"  • 產地：{row.get('country') or '—'}",
        "",
        f"🎖️ 專家評分：{score_text} 分",
        f"  📊 {score_bar}",
        f"👥 社群評分：{row.get('community_score') or '—'} ({row.get('review_count') or 0} 評論)",
    ]
    tasting_notes = row.get("tasting_notes")
    if isinstance(tasting_notes, str) and tasting_notes:
        lines.append("")
        lines.append("👃 品飲筆記")
        lines.append(f"{_truncate(tasting_notes, 200)}")

    flavors = cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT flavor_name, flavor_value FROM flavor_profiles "
            "WHERE spirit_id = ? ORDER BY flavor_value DESC LIMIT 8",
            (row["id"],),
        ).fetchall(),
    )
    flavors = [dict(f) for f in flavors]
    conn.close()
    if flavors:
        lines.append("")
        lines.append("🎨 風味圖譜")
        lines.append(_SEP_LIGHT)
        for f in flavors:
            value = f.get("flavor_value", 0)
            bar = _score_bar(int(value), width=6)
            lines.append(f"  {f['flavor_name']:<10} {bar} {int(value)}%")
    return "\n".join(lines)


def fmt_stats(db_path: str) -> str:
    conn = _connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM spirits").fetchone()[0]
    avg = conn.execute(
        "SELECT ROUND(AVG(expert_score),1) FROM spirits WHERE expert_score IS NOT NULL"
    ).fetchone()[0]
    hi = conn.execute("SELECT MAX(expert_score) FROM spirits").fetchone()[0]
    lo = conn.execute(
        "SELECT MIN(expert_score) FROM spirits WHERE expert_score IS NOT NULL"
    ).fetchone()[0]
    types = cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT spirit_type, COUNT(*) c FROM spirits GROUP BY spirit_type ORDER BY c DESC LIMIT 6"
        ).fetchall(),
    )
    countries = cast(
        list[sqlite3.Row],
        conn.execute(
            "SELECT country, COUNT(*) c FROM spirits WHERE country IS NOT NULL GROUP BY country ORDER BY c DESC LIMIT 5"
        ).fetchall(),
    )
    types = [tuple(r) for r in types]
    countries = [tuple(r) for r in countries]
    conn.close()

    lines: list[str] = [
        "📊 Distiller 數據概覽",
        _SEP,
        f"📈 總藏酒量：{total} 筆",
        f"⭐ 平均得分：{avg} 分",
        f"📏 分數區間：{lo} ~ {hi} 分",
        "",
        "📋 熱門類型",
        _SEP_LIGHT,
    ]
    for r in types:
        lines.append(f"  • {r[0]:<12} {r[1]:>4} 筆")
    lines.append("")
    lines.append("🌍 主要產地")
    lines.append(_SEP_LIGHT)
    for r in countries:
        lines.append(f"  • {r[0]:<12} {r[1]:>4} 筆")
    return "\n".join(lines)


def fmt_flavors(db_path: str, flavor_name: str | None = None, limit: int = 10) -> str:
    conn = _connect(db_path)
    if flavor_name:
        rows = cast(
            list[sqlite3.Row],
            conn.execute(
                "SELECT s.name, fp.flavor_value, s.expert_score "
                "FROM flavor_profiles fp JOIN spirits s ON s.id = fp.spirit_id "
                "WHERE fp.flavor_name = ? ORDER BY fp.flavor_value DESC LIMIT ?",
                (flavor_name, limit),
            ).fetchall(),
        )
        rows = [dict(r) for r in rows]
        conn.close()
        if not rows:
            return f"❓ 找不到風味「{flavor_name}」的相關資料。"
        lines: list[str] = [f"🎨 風味「{flavor_name}」榜單", _SEP]
        for i, r in enumerate(rows, 1):
            value = r.get("flavor_value", 0)
            bar = _score_bar(int(value), width=6)
            lines.append(f"{i:>2}. 【{r['name']}】")
            lines.append(f"    └ {bar} {int(value)}% | ⭐ {r['expert_score']}分")
            lines.append("")
        return "\n".join(lines)
    else:
        rows = cast(
            list[sqlite3.Row],
            conn.execute(
                "SELECT flavor_name, ROUND(AVG(flavor_value),0) avg "
                "FROM flavor_profiles GROUP BY flavor_name ORDER BY avg DESC"
            ).fetchall(),
        )
        rows = [dict(r) for r in rows]
        conn.close()
        lines: list[str] = ["🎨 風味維度平均值", _SEP]
        for r in rows:
            avg_value = r.get("avg", 0)
            bar = _score_bar(int(avg_value), width=6)
            lines.append(f"  • {r['flavor_name']:<12} {bar} {int(avg_value)}%")
        return "\n".join(lines)


def fmt_list(
    db_path: str,
    country: str | None = None,
    min_score: int | None = None,
    limit: int = 10,
) -> str:
    conn = _connect(db_path)
    conds: list[str] = []
    params: list[object] = []
    if country:
        conds.append("country LIKE ?")
        params.append(f"%{country}%")
    if min_score is not None:
        conds.append("expert_score >= ?")
        params.append(min_score)
    where = " WHERE " + " AND ".join(conds) if conds else ""
    total = conn.execute(f"SELECT COUNT(*) FROM spirits{where}", params).fetchone()[0]
    params.append(limit)
    rows = cast(
        list[sqlite3.Row],
        conn.execute(
            f"SELECT name, spirit_type, country, expert_score FROM spirits{where} "
            f"ORDER BY expert_score DESC NULLS LAST LIMIT ?",
            params,
        ).fetchall(),
    )
    rows = [dict(r) for r in rows]
    conn.close()
    if not rows:
        return "📭 找不到符合條件的烈酒。"
    title_parts = []
    if country:
        title_parts.append(country)
    if min_score:
        title_parts.append(f"{min_score}分以上")
    title = "・".join(title_parts) if title_parts else "全部"
    lines: list[str] = [f"📋 烈酒列表 ({title})", _SEP_LIGHT]
    for r in rows:
        score = r.get("expert_score")
        score_val = f" ({int(score)}分)" if isinstance(score, (int, float)) else ""
        lines.append(f"・【{r['name']}】")
        lines.append(f"  └ 🥃 {r['spirit_type']}{score_val}")
        lines.append(f"  └ 🌍 {r['country']}")
        lines.append("")
    if total > limit:
        lines.append(f"（顯示前 {limit} 筆 / 共 {total} 筆）")
    return "\n".join(lines)


def _fmt_scraper_status(label: str, lock: threading.Lock, state: dict) -> str:
    """格式化單一爬蟲的狀態行。"""
    with lock:
        running = state.get("running", False)
        mode = state.get("mode")
        started_at = state.get("started_at")
    if running:
        elapsed_str = "—"
        if started_at:
            try:
                elapsed = datetime.now() - datetime.fromisoformat(started_at)
                m, s = divmod(int(elapsed.total_seconds()), 60)
                elapsed_str = f"{m} 分 {s} 秒"
            except (ValueError, TypeError):
                pass
        return f"🔄 {label}：執行中 ({mode} 模式)\n   └ 已耗時 {elapsed_str}"
    return f"💤 {label}：閒置中"


def fmt_run_status() -> str:
    """回傳兩個爬蟲目前的執行狀態。"""
    distiller_line = _fmt_scraper_status("Distiller", _scrape_lock, _scrape_state)
    diffords_line = _fmt_scraper_status("Difford's", _diffords_lock, _diffords_state)
    return "\n".join(["📡 系統執行狀態", _SEP, distiller_line, "", diffords_line])


def fmt_recipe(diffords_db_path: str, query: str) -> str:
    """從 Difford's Guide DB 查詢雞尾酒酒譜並格式化輸出。"""
    from distiller_scraper.diffords_storage import DiffordsStorage

    with DiffordsStorage(diffords_db_path) as storage:
        cocktail = storage.get_cocktail_by_name(query)
        if not cocktail:
            # 嘗試搜尋
            results = storage.search_cocktails(query, limit=5)
            if not results:
                return f"❓ 找不到「{query}」的酒譜。\n試試「酒譜 Negroni」或「酒譜 Daiquiri」。"
            if len(results) == 1:
                cocktail = storage._attach_ingredients(results[0])
            else:
                lines = [f"🔎 找到 {len(results)} 筆相關酒譜：", _SEP_LIGHT]
                for r in results:
                    rating = (
                        f" ⭐{r['rating_value']:.1f}" if r.get("rating_value") else ""
                    )
                    lines.append(f"• {r['name']}{rating}")
                lines.append("")
                lines.append("💡 提示：傳送「酒譜 <確切名稱>」查看詳情。")
                return "\n".join(lines)

        return _fmt_recipe_detail(cocktail)


def _fmt_recipe_detail(c: dict) -> str:
    """將單筆 Difford's 雞尾酒資料格式化為 LINE 訊息。"""
    lines = [f"🍸 【{c['name']}】", _SEP]

    if c.get("description"):
        lines.append(f"『{c['description']}』")
        lines.append("")

    # 評分與基本資訊
    meta_parts = []
    if c.get("rating_value"):
        meta_parts.append(f"⭐ {c['rating_value']:.1f}/5")
    if c.get("abv"):
        meta_parts.append(f"🍺 {c['abv']:.1f}%")
    if c.get("calories"):
        meta_parts.append(f"🔥 {c['calories']} kcal")
    
    if meta_parts:
        lines.append(" | ".join(meta_parts))

    if c.get("glassware") or c.get("prepare"):
        glass = f"🥂 {c['glassware']}" if c.get("glassware") else ""
        prep = f"🧊 {c['prepare']}" if c.get("prepare") else ""
        lines.append(f"{glass}  {prep}".strip())
    
    lines.append("")

    # 食材
    ingredients = c.get("ingredients") or []
    if ingredients:
        lines.append("📋 調製配方")
        for ing in ingredients:
            amount = ing.get("amount", "").strip()
            item = ing.get("item", "").strip()
            generic = ing.get("item_generic", "")
            generic_str = f" ({generic})" if generic and generic != item else ""
            lines.append(f"  • {amount} {item}{generic_str}".rstrip())
        lines.append("")

    # 調製方式
    if c.get("instructions"):
        lines.append("📝 作法步驟")
        lines.append(c["instructions"])
    if c.get("garnish"):
        lines.append(f"🌿 裝飾：{c['garnish']}")
    lines.append("")

    # 歷史與評論（節錄）
    if c.get("history"):
        lines.append("📖 經典背景")
        lines.append(f"{_truncate(c['history'], 200)}")
        lines.append("")
    if c.get("review"):
        lines.append("💬 專業評語")
        lines.append(f"{_truncate(c['review'], 150)}")
        lines.append("")

    if c.get("url"):
        lines.append(f"🔗 完整網頁：{c['url']}")

    return "\n".join(lines)


_DIFFORDS_DB_MISSING = (
    "⚠️ Difford's Guide 資料庫尚未建立。\n"
    "請先執行 Difford's 爬蟲：\n"
    "uv run python run_diffords.py --mode test"
)


def fmt_cocktail_search(diffords_db_path: str, keyword: str) -> str:
    if not Path(diffords_db_path).exists():
        return _DIFFORDS_DB_MISSING
    from distiller_scraper.diffords_storage import DiffordsStorage

    with DiffordsStorage(diffords_db_path) as storage:
        results = storage.search_cocktails(keyword, limit=20)
    if not results:
        return f"❓ 找不到含有「{keyword}」的調酒，請嘗試縮短關鍵字。"
    lines: list[str] = [f"🔍 搜尋「{keyword}」：{len(results)} 筆", _SEP_LIGHT]
    for r in results:
        lines.append(f"・{r['name']}")
        if r.get("rating_value"):
            lines.append(f"  ⭐ {r['rating_value']:.1f}/5")
        if r.get("description"):
            lines.append(f"  {_truncate(r['description'], 100)}")
        lines.append("")
    lines.append("💡 傳送『調酒詳情 <名稱>』查看完整酒譜")
    return "\n".join(lines)


def _get_diffords_reference(diffords_db_path: str | None, cocktail_name: str) -> str:
    """從 Difford's DB 取得補充資訊（history/review）。若無 DB 或無資料則回傳空字串。"""
    if not diffords_db_path or not Path(diffords_db_path).exists():
        return ""
    try:
        from distiller_scraper.diffords_storage import DiffordsStorage

        with DiffordsStorage(diffords_db_path) as storage:
            c = storage.get_cocktail_by_name(cocktail_name)
        if not c:
            return ""
        parts = []
        if c.get("history"):
            parts.append(f"📖 歷史\n{_truncate(c['history'], 200)}")
        if c.get("review"):
            parts.append(f"💬 Difford's 評語\n{_truncate(c['review'], 150)}")
        if not parts:
            return ""
        return "\n\n" + _SEP_LIGHT + "\n" + "\n\n".join(parts)
    except Exception as e:
        logger.debug("Difford's 參考資料查詢失敗：%s", e)
        return ""


def fmt_cocktail_info(diffords_db_path: str, name: str) -> str:
    if not Path(diffords_db_path).exists():
        return _DIFFORDS_DB_MISSING
    from distiller_scraper.diffords_storage import DiffordsStorage

    with DiffordsStorage(diffords_db_path) as storage:
        cocktail = storage.get_cocktail_by_name(name)
        if not cocktail:
            results = storage.search_cocktails(name, limit=5)
            if not results:
                return f"❓ 找不到「{name}」的調酒。\n試試『調酒搜尋 <關鍵字>』"
            if len(results) == 1:
                cocktail = storage._attach_ingredients(results[0])
            else:
                lines = [f"🔎 找到 {len(results)} 筆相關調酒：", _SEP_LIGHT]
                for r in results:
                    rating = (
                        f" ⭐{r['rating_value']:.1f}" if r.get("rating_value") else ""
                    )
                    lines.append(f"• {r['name']}{rating}")
                return "\n".join(lines)
    return _fmt_recipe_detail(cocktail)


def fmt_cocktail_stats(diffords_db_path: str) -> str:
    if not Path(diffords_db_path).exists():
        return _DIFFORDS_DB_MISSING
    import sqlite3 as _sqlite3

    from distiller_scraper.diffords_storage import DiffordsStorage

    with DiffordsStorage(diffords_db_path) as storage:
        stats = storage.get_stats()

    with _sqlite3.connect(diffords_db_path) as conn:
        rows = conn.execute(
            "SELECT item_generic, COUNT(*) c FROM cocktail_ingredients "
            "WHERE item_generic IS NOT NULL GROUP BY item_generic ORDER BY c DESC LIMIT 5"
        ).fetchall()

    total = stats["總雞尾酒數"]
    avg_rating = stats["平均評分"]
    last_scrape = stats["最後爬取"]
    avg_str = f"{avg_rating:.1f}" if avg_rating is not None else "—"

    lines: list[str] = [
        "📊 Difford's Guide 數據概覽",
        _SEP,
        f"🍸 總調酒數　{total} 筆",
        f"⭐ 平均評分　{avg_str}/5",
        f"🕐 最後爬取　{last_scrape or '尚未爬取'}",
        "",
        "📋 最常見材料 Top 5",
        _SEP_LIGHT,
    ]
    for ingredient, count in rows:
        lines.append(f"  {ingredient:<15} {count:>4} 次")
    return "\n".join(lines)


def fmt_cocktail_list(
    diffords_db_path: str,
    filter_type: str | None = None,
    filter_value: str | None = None,
) -> str:
    if not Path(diffords_db_path).exists():
        return (
            "⚠️ Difford's Guide 資料庫尚未建立。\n"
            "請先執行 Difford's 爬蟲：\n"
            "uv run python run_diffords.py --mode test"
        )
    from distiller_scraper.diffords_storage import DiffordsStorage

    with DiffordsStorage(diffords_db_path) as storage:
        if filter_type == "ingredient":
            results = storage.filter_by_ingredient(filter_value, limit=20)
        elif filter_type == "tag":
            results = storage.filter_by_tag(filter_value, limit=20)
        elif filter_type == "rating":
            results = storage.filter_by_rating(min_rating=float(filter_value), limit=20)
        else:
            results = storage.get_top_rated(limit=20)

    if not results:
        return "📭 沒有符合條件的調酒。"

    if filter_type is None:
        header = "🍸 Difford's 調酒列表 Top 20"
    else:
        header = f"🍸 {filter_type}: {filter_value} 調酒列表"

    lines: list[str] = [header, _SEP, ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['name']}")
        if r.get("rating_value"):
            lines.append(f"   ⭐ {r['rating_value']:.1f}/5")
        else:
            lines.append("   （暫無評分）")
        lines.append("")
    return "\n".join(lines)


def fmt_cocktail_makeable(diffords_db_path: str, distiller_db_path: str) -> str:
    if not Path(diffords_db_path).exists():
        return (
            "⚠️ Difford's Guide 資料庫尚未建立。\n"
            "請先執行 Difford's 爬蟲：\n"
            "uv run python run_diffords.py --mode test"
        )
    if not Path(distiller_db_path).exists():
        return (
            "⚠️ Distiller 資料庫不存在，請先執行爬蟲："
            "uv run python run.py --mode test --output sqlite"
        )
    from distiller_scraper.diffords_storage import (
        DiffordsStorage,
        get_user_spirit_types,
        load_ingredient_mapping,
    )

    user_spirit_types = get_user_spirit_types(distiller_db_path)
    mapping = load_ingredient_mapping()

    with DiffordsStorage(diffords_db_path) as storage:
        results = storage.get_makeable_cocktails(user_spirit_types, mapping)

    if not results:
        spirit_preview = ", ".join(user_spirit_types[:5])
        if len(user_spirit_types) > 5:
            spirit_preview += "..."
        return (
            f"🍸 根據您收藏的 {len(user_spirit_types)} 款烈酒，"
            f"目前沒有找到可完全調製的雞尾酒。\n\n"
            f"收藏的烈酒類型：{spirit_preview}"
        )

    lines: list[str] = [
        f"🍸 您可以調製的雞尾酒 ({len(results)} 款)",
        _SEP,
        "",
    ]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['name']}")
        if r.get("rating_value"):
            lines.append(f"   ⭐ {r['rating_value']:.1f}/5")
        lines.append("")
    lines.append("💡 傳送『調酒詳情 <名稱>』查看完整酒譜")
    return "\n".join(lines)


def fmt_help() -> str:
    return "\n".join(
        [
            "🥃 【Distiller 指令指南】",
            _SEP,
            "",
            "🥃 烈酒查詢 (Distiller)",
            "  • 烈酒排行 [N]        查看專家評分榜單",
            "  • 烈酒搜尋 <關鍵字>   找尋品名、品牌、描述",
            "  • 烈酒詳情 <名稱>     完整資訊與風味圖譜",
            "  • 烈酒列表 [產地] [分數]  篩選特定條件",
            "  • 烈酒統計           資料庫數據摘要",
            "  • 烈酒風味 [名稱]    特定風味維度排行",
            "",
            "🍸 雞尾酒查詢 (Difford's)",
            "  • 雞尾酒酒譜 <酒名>   食材、作法、歷史",
            "    例：雞尾酒酒譜 Margarita",
            "  • 雞尾酒搜尋 <關鍵字> 搜尋雞尾酒名稱",
            "  • 雞尾酒詳情 <名稱>   完整酒譜與評分",
            "  • 雞尾酒統計         資料庫數據摘要",
            "  • 雞尾酒列表 [--ingredient/--tag/--rating]  篩選調酒",
            "  • 雞尾酒推薦         根據收藏推薦可調製項目",
            "",
            "🤖 系統指令",
            "  • 執行狀態           查看爬蟲執行狀態",
            "  • 說明               顯示本指南",
            "",
            "💡 提示：輸入關鍵字的一部分即可搜尋！",
        ]
    )


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------


def parse_command(text: str) -> tuple[str, list[str | int | None]]:
    """將 LINE 訊息解析為 (command, args)。"""
    text = text.strip()
    lower = text.lower()

    if lower in ("說明", "help", "指令", "?", "？"):
        return "help", []

    # 烈酒統計
    if lower in ("烈酒統計", "統計", "stats", "總覽"):
        return "stats", []

    # 烈酒風味
    if lower in ("烈酒風味", "風味", "flavors", "flavor"):
        return "flavors", []

    m = re.match(r"^(烈酒風味|風味|flavor[s]?)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "flavors", [m.group(2).strip()]

    # 烈酒排行
    m = re.match(r"^(烈酒排行|top|排行)\s*(\d+)?$", lower)
    if m:
        n = int(m.group(2)) if m.group(2) else 10
        return "top", [min(n, 20)]

    # 烈酒搜尋
    m = re.match(r"^(烈酒搜尋|搜尋|search|找)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "search", [m.group(2).strip()]

    # 烈酒詳情
    m = re.match(r"^(烈酒詳情|詳情|info|查)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "info", [m.group(2).strip()]

    # 烈酒列表 [產地] [分數]
    m = re.match(r"^(烈酒列表|列表|list)(\s+(.+?))?(\s+(\d{2,3}))?$", text, re.IGNORECASE)
    if m:
        country = m.group(3).strip() if m.group(3) else None
        score = int(m.group(5)) if m.group(5) else None
        # 如果第三組只是數字，視為分數
        if country and country.isdigit():
            score = int(country)
            country = None
        return "list", [country, score]

    # 雞尾酒統計
    if lower in ("雞尾酒統計", "調酒統計", "cocktail stats"):
        return "cocktail_stats", []

    # 雞尾酒搜尋 <kw> / cocktail search <kw>
    m = re.match(r"^(雞尾酒搜尋|調酒搜尋|cocktail\s+search)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "cocktail_search", [m.group(2).strip()]

    # 雞尾酒詳情 <name> / cocktail info <name>
    m = re.match(r"^(雞尾酒詳情|調酒詳情|cocktail\s+info)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "cocktail_info", [m.group(2).strip()]

    # 雞尾酒推薦 / 我能做什麼 / cocktail makeable
    if lower in ("雞尾酒推薦", "我能做什麼", "cocktail makeable", "調酒推薦"):
        return "cocktail_makeable", []

    # 雞尾酒列表 [--ingredient X | --tag Y | --rating N] / cocktail list [...]
    m = re.match(
        r"^(雞尾酒列表|調酒列表|cocktail\s+list)(\s+--(\w+)\s+(.+))?$", text, re.IGNORECASE
    )
    if m:
        filter_type = m.group(3) if m.group(3) else None
        filter_value = m.group(4).strip() if m.group(4) else None
        return "cocktail_list_filter", [filter_type, filter_value]

    # 雞尾酒酒譜（Difford's Guide）
    m = re.match(r"^(雞尾酒酒譜|酒譜|酒方|recipe)\s+(.+)$", text, re.IGNORECASE)
    if m:
        return "recipe", [m.group(2).strip()]

    # 烈酒爬蟲（支援新語法「烈酒爬蟲」及舊語法「執行/run distiller」）
    m = re.match(
        r"^(?:烈酒爬蟲|(?:執行|run)\s+distiller)\s+(test|medium|full)$",
        text,
        re.IGNORECASE,
    )
    if m:
        return "run_scrape", [m.group(1).lower()]

    # 雞尾酒爬蟲（支援新語法「雞尾酒爬蟲」及舊語法「執行/run diffords」）
    m = re.match(
        r"^(?:雞尾酒爬蟲|(?:執行|run)\s+diffords?)\s+(test|incremental|full)$",
        text,
        re.IGNORECASE,
    )
    if m:
        return "run_diffords", [m.group(1).lower()]

    # 向下相容：執行 <mode>（不指定來源，預設為 Distiller）
    m = re.match(r"^(執行|run)\s+(test|medium|full)$", text, re.IGNORECASE)
    if m:
        return "run_scrape", [m.group(2).lower()]

    if lower in ("執行狀態", "run status", "爬蟲狀態"):
        return "run_status", []

    return "unknown", [text]


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------


def create_app(
    db_path: str = DB_DEFAULT,
    diffords_db_path: str = DIFFORDS_DB_DEFAULT,
    channel_secret: str | None = None,
    channel_id: str | None = None,
) -> Flask:
    app = Flask(__name__)
    _channel_secret = channel_secret or os.getenv("LINE_CHANNEL_SECRET", "")
    _channel_id = channel_id or os.getenv("LINE_CHANNEL_ID", "")

    @app.route("/health", methods=["GET"])
    def health():
        diffords_exists = Path(diffords_db_path).exists()
        diffords_count = None
        diffords_last_scrape = None
        diffords_avg_rating = None
        if diffords_exists:
            try:
                from distiller_scraper.diffords_storage import DiffordsStorage

                with DiffordsStorage(diffords_db_path) as ds:
                    stats = ds.get_stats()
                diffords_count = stats.get("總雞尾酒數")
                diffords_last_scrape = stats.get("最後爬取")
                diffords_avg_rating = stats.get("平均評分")
            except Exception:
                pass
        return {
            "status": "ok",
            "db_exists": Path(db_path).exists(),
            "token_cached": _token_cache.get("token") is not None,
            "diffords_db_exists": diffords_exists,
            "diffords_cocktail_count": diffords_count,
            "diffords_last_scrape": diffords_last_scrape,
            "diffords_avg_rating": diffords_avg_rating,
        }, 200

    @app.route("/webhook", methods=["POST"])
    def webhook():
        body = request.get_data()
        data = request.get_json(silent=True)

        if not data or not data.get("events"):
            if body and data is None:
                logger.warning("收到無法解析的 JSON")
            return "OK", 200

        signature = request.headers.get("X-Line-Signature", "")
        if not _verify_signature(body, signature, _channel_secret):
            logger.warning("簽名驗證失敗（來源 IP：%s）", request.remote_addr)
            abort(400)

        for event in data.get("events", []):
            if event.get("type") != "message":
                continue
            if event.get("message", {}).get("type") != "text":
                continue

            reply_token = event.get("replyToken", "")
            user_text = event["message"]["text"]
            logger.info("收到訊息：%s", user_text)

            user_id = event.get("source", {}).get("userId", "")

            response = _handle(
                user_text, db_path, diffords_db_path=diffords_db_path, user_id=user_id
            )

            token = _get_cached_token(_channel_id, _channel_secret)
            if token:
                if not _reply(reply_token, response, token):
                    logger.error("回覆訊息失敗")
            else:
                logger.error("無法取得 Access Token，無法回覆")

        return "OK", 200

    return app


def _handle(
    text: str,
    db_path: str,
    diffords_db_path: str = DIFFORDS_DB_DEFAULT,
    user_id: str | None = None,
) -> str:
    """解析指令並回傳回覆文字。"""
    if not Path(db_path).exists():
        if not _ensure_db_from_gcs(db_path):
            logger.warning("資料庫不存在：%s", db_path)
            return "⚠️ 資料庫不存在，請先執行：python run.py --mode test --output sqlite"

    command, args = parse_command(text)

    try:
        if command == "help":
            return fmt_help()
        elif command == "top":
            raw_n = args[0] if args else 10
            if isinstance(raw_n, int):
                n = raw_n
            elif isinstance(raw_n, str) and raw_n.isdigit():
                n = int(raw_n)
            else:
                n = 10
            return fmt_top(db_path, n)
        elif command == "search":
            keyword = str(args[0])
            return fmt_search(db_path, keyword)
        elif command == "info":
            name = str(args[0])
            return fmt_info(db_path, name)
        elif command == "stats":
            return fmt_stats(db_path)
        elif command == "flavors":
            flavor_name = str(args[0]) if args else None
            return fmt_flavors(db_path, flavor_name)
        elif command == "list":
            raw_country = args[0] if len(args) > 0 else None
            country = str(raw_country) if raw_country is not None else None
            raw_score = args[1] if len(args) > 1 else None
            if isinstance(raw_score, int):
                min_score = raw_score
            elif isinstance(raw_score, str) and raw_score.isdigit():
                min_score = int(raw_score)
            else:
                min_score = None
            return fmt_list(db_path, country, min_score)
        elif command == "recipe":
            query = str(args[0]) if args else ""
            if not query:
                return "請輸入酒譜名稱，例：酒譜 Negroni"
            if not _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB):
                return "⚠️ Difford's 酒譜資料庫尚未建立，請先執行爬蟲。"
            return fmt_recipe(diffords_db_path, query)
        elif command == "run_status":
            return fmt_run_status()
        elif command == "run_scrape":
            authorized_user_id = os.getenv("LINE_USER_ID", "")
            if not authorized_user_id or user_id != authorized_user_id:
                return "⛔ 僅授權使用者可執行爬蟲指令。"
            mode = str(args[0])
            with _scrape_lock:
                if _scrape_state["running"]:
                    return f"⚠️ Distiller 爬蟲正在執行中（{_scrape_state['mode']} 模式），請稍後再試。"
                _scrape_state["running"] = True
                _scrape_state["mode"] = mode
                _scrape_state["started_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                _start_scraper(mode, db_path)
            except Exception as exc:
                with _scrape_lock:
                    _scrape_state["running"] = False
                    _scrape_state["mode"] = None
                logger.error("啟動 Distiller 爬蟲失敗：%s", exc)
                return f"⚠️ Distiller 爬蟲啟動失敗：{exc}"
            return f"🚀 Distiller 爬蟲已啟動（{mode} 模式），完成後將推播通知您。"
        elif command == "run_diffords":
            authorized_user_id = os.getenv("LINE_USER_ID", "")
            if not authorized_user_id or user_id != authorized_user_id:
                return "⛔ 僅授權使用者可執行爬蟲指令。"
            mode = str(args[0])
            with _diffords_lock:
                if _diffords_state["running"]:
                    return f"⚠️ Difford's 爬蟲正在執行中（{_diffords_state['mode']} 模式），請稍後再試。"
                _diffords_state["running"] = True
                _diffords_state["mode"] = mode
                _diffords_state["started_at"] = datetime.now().isoformat(timespec="seconds")
            try:
                _start_diffords(mode, diffords_db_path)
            except Exception as exc:
                with _diffords_lock:
                    _diffords_state["running"] = False
                    _diffords_state["mode"] = None
                logger.error("啟動 Difford's 爬蟲失敗：%s", exc)
                return f"⚠️ Difford's 爬蟲啟動失敗：{exc}"
            return f"🚀 Difford's 爬蟲已啟動（{mode} 模式），完成後將推播通知您。"
        elif command == "cocktail_search":
            keyword = str(args[0]) if args else ""
            _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB)
            return fmt_cocktail_search(diffords_db_path, keyword)
        elif command == "cocktail_info":
            name = str(args[0]) if args else ""
            _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB)
            return fmt_cocktail_info(diffords_db_path, name)
        elif command == "cocktail_stats":
            _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB)
            return fmt_cocktail_stats(diffords_db_path)
        elif command == "cocktail_list_filter":
            filter_type = args[0] if args else None
            filter_value = args[1] if len(args) > 1 else None
            _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB)
            return fmt_cocktail_list(diffords_db_path, filter_type, filter_value)
        elif command == "cocktail_makeable":
            _ensure_db_from_gcs(diffords_db_path, GCS_DIFFORDS_DB_BLOB)
            return fmt_cocktail_makeable(diffords_db_path, db_path)
        else:
            return f"不認識指令「{text}」。\n傳送「說明」查看所有指令。"
    except Exception as exc:
        logger.error("處理指令失敗：%s", exc)
        return "⚠️ 查詢時發生錯誤，請稍後再試。"


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    _diffords_status = "✓" if Path(DIFFORDS_DB_DEFAULT).exists() else "✗ 未找到"
    print(f"""
Distiller LINE Bot 已啟動
────────────────────────
Port        : {port}
DB          : {DB_DEFAULT}
Difford's DB: {DIFFORDS_DB_DEFAULT} [{_diffords_status}]

本機開發步驟：
1. 安裝 ngrok：brew install ngrok
2. 啟動 tunnel：ngrok http {port}
3. 複製 https://xxxx.ngrok.io/webhook
4. 在 LINE Developers Console 貼上 Webhook URL
5. 開啟 Use webhook 開關

注意：macOS AirPlay Receiver 會佔用 port 5000
      可在系統設定 → 通用 → AirDrop 與接力 中關閉，或使用預設 port 8000
""")
    _secret = os.getenv("LINE_CHANNEL_SECRET", "")
    _id = os.getenv("LINE_CHANNEL_ID", "")
    if not _secret or not _id:
        logger.error("缺少必要環境變數：LINE_CHANNEL_SECRET / LINE_CHANNEL_ID")
        sys.exit(1)
    logger.info("環境變數檢查通過")
    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=False)
