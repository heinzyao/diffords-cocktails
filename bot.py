#!/usr/bin/env python3
"""LINE Bot for querying Difford's Guide cocktail recipes."""

import base64
import hashlib
import hmac
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

from diffords_guide.config import DIFFORDS_DB_DEFAULT, GCS_DIFFORDS_DB_BLOB
from diffords_guide.notify import fetch_access_token

_ = load_dotenv()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
MSG_LIMIT = 4900

GCS_BUCKET = os.getenv("GCS_BUCKET", "")
GCS_DB_BLOB = os.getenv("GCS_DB_BLOB", GCS_DIFFORDS_DB_BLOB)
DB_DEFAULT = DIFFORDS_DB_DEFAULT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_token_cache: dict[str, str | float] = {}
_scrape_lock = threading.Lock()
_scrape_state: dict[str, Any] = {"running": False, "mode": None, "started_at": None}
_db_gcs_checked: dict[str, float] = {}
_DB_GCS_CHECK_INTERVAL = 300


def _get_cached_token(channel_id: str, channel_secret: str) -> str | None:
    now = time.time()
    token = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at", 0.0)
    if isinstance(token, str) and isinstance(expires_at, (int, float)):
        if now < expires_at - 60:
            return token
    token = fetch_access_token(channel_id, channel_secret)
    if token:
        _token_cache["token"] = token
        _token_cache["expires_at"] = now + 82800
    return token


def _verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == signature


def _reply(reply_token: str, text: str, access_token: str) -> bool:
    chunks = [text[i : i + MSG_LIMIT] for i in range(0, len(text), MSG_LIMIT)][:5]
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": c} for c in chunks]}
    try:
        resp = requests.post(
            LINE_REPLY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.error("LINE reply failed: %s", exc)
        return False
    if resp.status_code != 200:
        logger.warning("LINE reply failed: %s %s", resp.status_code, resp.text[:200])
        return False
    return True


def _ensure_db_from_gcs(db_path: str, blob_name: str = GCS_DB_BLOB) -> bool:
    if not GCS_BUCKET:
        return Path(db_path).exists()

    from diffords_guide import gcs_storage

    path = Path(db_path)
    if not path.exists():
        ok = gcs_storage.download_db(GCS_BUCKET, blob_name, db_path)
        if ok:
            _db_gcs_checked[db_path] = time.time()
        return ok

    now = time.time()
    if now - _db_gcs_checked.get(db_path, 0.0) < _DB_GCS_CHECK_INTERVAL:
        return True

    blob_updated = gcs_storage.get_blob_updated_time(GCS_BUCKET, blob_name)
    _db_gcs_checked[db_path] = now
    if blob_updated is None:
        return True
    if blob_updated.timestamp() > path.stat().st_mtime:
        return gcs_storage.download_db(GCS_BUCKET, blob_name, db_path)
    return True


def _start_diffords(mode: str, db_path: str) -> None:
    if GCS_BUCKET and os.getenv("GOOGLE_CLOUD_PROJECT"):
        project = os.getenv("GOOGLE_CLOUD_PROJECT", os.getenv("GCLOUD_PROJECT", ""))
        region = os.getenv("CLOUD_RUN_REGION", "asia-east1")
        job_name = os.getenv("DIFFORDS_JOB_NAME", "diffords-cocktails-scraper")

        from google.cloud import run_v2  # type: ignore[import]

        client = run_v2.JobsClient()
        name = f"projects/{project}/locations/{region}/jobs/{job_name}"
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    args=["--mode", mode, "--notify-line"]
                )
            ]
        )
        client.run_job(request=run_v2.RunJobRequest(name=name, overrides=overrides))
        logger.info("Cloud Run Job started: %s", name)
        # 遠端 job 在獨立容器執行，此 bot 實例無法追蹤其進度；立即清除本地狀態
        with _scrape_lock:
            _scrape_state["running"] = False
            _scrape_state["mode"] = None
            _scrape_state["started_at"] = None
        return

    cmd = [
        sys.executable,
        str(Path(__file__).parent / "run_diffords.py"),
        "--mode",
        mode,
        "--db-path",
        db_path,
        "--notify-line",
    ]

    def _run() -> None:
        try:
            subprocess.run(cmd, capture_output=False, check=False)
        finally:
            with _scrape_lock:
                _scrape_state["running"] = False
                _scrape_state["mode"] = None
                _scrape_state["started_at"] = None

    threading.Thread(target=_run, daemon=True).start()


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    return text[:limit] + "…" if len(text) > limit else text


def _open_storage(db_path: str):
    from diffords_guide.storage import DiffordsStorage

    if not _ensure_db_from_gcs(db_path, GCS_DB_BLOB):
        return None
    return DiffordsStorage(db_path)


def fmt_cocktail_search(db_path: str, keyword: str, limit: int = 5) -> str:
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        rows = storage.search_cocktails(keyword, limit=limit)
    finally:
        storage.close()
    if not rows:
        return f"🔍 找不到符合「{keyword}」的雞尾酒，請嘗試其他關鍵字！"

    lines = [
        f"🔍 搜尋「{keyword}」的結果：",
        "──────────────────"
    ]
    for idx, cocktail in enumerate(rows, 1):
        rating = cocktail.get("rating_value")
        rating_text = f"{rating:.1f} ★" if isinstance(rating, (int, float)) else "N/A"
        lines.append(f"{idx}. 🌟 {cocktail['name']} ({rating_text})")
    lines.extend([
        "──────────────────",
        "💡 輸入「雞尾酒酒譜 <酒名>」即可查看完整調製步驟！"
    ])
    return "\n".join(lines)


def fmt_cocktail_info(db_path: str, name: str) -> str:
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        cocktail = storage.get_cocktail_by_name(name)
    finally:
        storage.close()
    if not cocktail:
        return f"🔍 找不到符合「{name}」的雞尾酒，請檢查拼字或嘗試其他關鍵字！"

    lines = []
    lines.append(f"🍸 {cocktail['name']}")
    lines.append("──────────────────")

    # 基礎屬性
    rating = cocktail.get("rating_value")
    if rating not in (None, ""):
        rating_count = cocktail.get("rating_count")
        if rating_count:
            lines.append(f"⭐ 評分：{rating:.1f} ★ ({rating_count} 票)")
        else:
            lines.append(f"⭐ 評分：{rating:.1f} ★")

    abv = cocktail.get("abv")
    if abv not in (None, ""):
        lines.append(f"🔥 酒精濃度 (ABV)：{abv}%")

    glass = cocktail.get("glassware")
    if glass not in (None, ""):
        lines.append(f"🥛 推薦杯型：{glass}")

    garnish = cocktail.get("garnish")
    if garnish not in (None, ""):
        lines.append(f"🍊 推薦裝飾：{garnish}")

    # 準備步驟
    prepare = cocktail.get("prepare")
    if prepare not in (None, ""):
        lines.extend(["", "📝 準備步驟：", f"{prepare}"])

    # 食材
    ingredients = cocktail.get("ingredients") or []
    if ingredients:
        lines.extend(["", "🛒 食材配方："])
        for ingredient in ingredients[:12]:
            amount = ingredient.get("amount") or ""
            item = ingredient.get("item") or ""
            lines.append(f" ▫ {amount} {item}".rstrip())

    # 作法
    instructions = cocktail.get("instructions")
    if instructions:
        trunc_instructions = _truncate(instructions, 1000)
        lines.extend(["", "📋 調製方法：", trunc_instructions])

    # 評語與歷史背景
    review = cocktail.get("review")
    history = cocktail.get("history")
    notes = []
    if review:
        notes.append(f"💬 評語：{_truncate(review, 300)}")
    if history:
        notes.append(f"📜 歷史：{_truncate(history, 300)}")

    if notes:
        lines.extend(["", "💡 酒譜背景與筆記："])
        lines.extend(notes)

    if cocktail.get("url"):
        lines.extend(["", f"🌐 原文網址：\n{cocktail['url']}"])

    return "\n".join(lines).strip()


def fmt_cocktail_stats(db_path: str) -> str:
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        stats = storage.get_stats()
    finally:
        storage.close()

    total = stats.get("總雞尾酒數", 0)
    avg_rating = stats.get("平均評分", "N/A")
    last_run = stats.get("最後爬取", "從未")

    lines = [
        "📊 Difford's Guide 雞尾酒資料庫統計",
        "──────────────────",
        f"📈 總收錄酒譜：{total} 款",
        f"⭐ 平均社群評分：{avg_rating} ★",
        f"🕒 最後更新時間：{last_run}",
        "──────────────────"
    ]
    return "\n".join(lines)


def fmt_cocktail_list(
    db_path: str,
    *,
    ingredient: str | None = None,
    tag: str | None = None,
    min_rating: float | None = None,
    min_abv: float | None = None,
    limit: int = 10,
) -> str:
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        if ingredient:
            rows = storage.filter_by_ingredient(ingredient, limit=limit)
            title = f"含有「{ingredient}」"
        elif tag:
            rows = storage.filter_by_tag(tag, limit=limit)
            title = f"標籤「{tag}」"
        elif min_rating is not None:
            rows = storage.filter_by_rating(min_rating=min_rating, limit=limit)
            title = f"評分 >= {min_rating} ★"
        elif min_abv is not None:
            rows = storage.filter_by_abv(min_abv=min_abv, limit=limit)
            title = f"ABV >= {min_abv}%"
        else:
            rows = storage.get_top_rated(limit=limit)
            title = "社群高分精選"
    finally:
        storage.close()
    if not rows:
        return f"🔍 找不到符合「{title}」篩選條件的雞尾酒。"

    lines = [
        f"📋 雞尾酒列表（{title}）",
        "──────────────────"
    ]
    for idx, cocktail in enumerate(rows, 1):
        rating = cocktail.get("rating_value")
        rating_text = f"{rating:.1f} ★" if isinstance(rating, (int, float)) else "N/A"
        lines.append(f"{idx}. 🌟 {cocktail['name']} ({rating_text})")
    lines.extend([
        "──────────────────",
        "💡 輸入「雞尾酒酒譜 <酒名>」即可查看完整調製步驟！"
    ])
    return "\n".join(lines)


def fmt_status() -> str:
    with _scrape_lock:
        if not _scrape_state["running"]:
            return "🤖 Difford's Guide 爬蟲任務：目前閒置中"
        started = _scrape_state.get("started_at")
        elapsed = int(time.time() - started) if isinstance(started, (int, float)) else 0
        return f"⚡ Difford's Guide 爬蟲任務：正在執行中...\n模式：{_scrape_state['mode']}\n已耗時：{elapsed} 秒"


def fmt_help() -> str:
    return "\n".join(
        [
            "🍹 Difford's Guide 雞尾酒助理指令",
            "",
            "🔍 【探索與搜尋】",
            "▪ 說明 / 指令 / Help",
            "  顯示此指令清單",
            "▪ 雞尾酒搜尋 <關鍵字>",
            "  搜尋名稱符合關鍵字的雞尾酒",
            "▪ 雞尾酒酒譜 <酒名>",
            "  查詢指定雞尾酒的食材與作法",
            "",
            "📋 【精選與篩選】",
            "▪ 雞尾酒列表",
            "  列出社群高分經典雞尾酒",
            "▪ 雞尾酒列表 材料 <材料>",
            "  依特定材料/基酒篩選",
            "▪ 雞尾酒列表 標籤 <標籤>",
            "  依特定風味標籤或風格篩選",
            "▪ 雞尾酒列表 評分 <最低評分>",
            "  篩選社群高評分的酒譜",
            "▪ 雞尾酒列表 酒精濃度 <最低%>",
            "  篩選酒精濃度高於指定濃度的酒譜",
            "",
            "📊 【系統與爬蟲】",
            "▪ 雞尾酒統計",
            "  顯示資料庫統計資訊",
            "▪ 狀態",
            "  檢查資料庫爬蟲的最新運作狀態",
            "▪ 雞尾酒爬蟲 <test|incremental|full>",
            "  啟動資料庫更新任務 (管理員專用)",
        ]
    )


def parse_command(text: str) -> tuple[str, list[Any]]:
    text = text.strip()
    lower = text.lower()
    if lower in ("help", "說明", "指令"):
        return "help", []
    if lower in ("status", "狀態"):
        return "status", []
    if lower in ("雞尾酒統計", "cocktail stats", "stats"):
        return "stats", []

    match = re.match(r"^(?:雞尾酒爬蟲|run diffords)\s+(test|incremental|full)$", text, re.I)
    if match:
        return "scrape", [match.group(1).lower()]

    match = re.match(r"^(?:雞尾酒搜尋|cocktail search|search)\s+(.+)$", text, re.I)
    if match:
        return "search", [match.group(1).strip()]

    match = re.match(r"^(?:雞尾酒酒譜|雞尾酒詳情|recipe|info)\s+(.+)$", text, re.I)
    if match:
        return "info", [match.group(1).strip()]

    match = re.match(r"^雞尾酒列表\s+材料\s+(.+)$", text, re.I)
    if match:
        return "list", [{"ingredient": match.group(1).strip()}]
    match = re.match(r"^雞尾酒列表\s+標籤\s+(.+)$", text, re.I)
    if match:
        return "list", [{"tag": match.group(1).strip()}]
    match = re.match(r"^雞尾酒列表\s+評分\s+([\d.]+)$", text, re.I)
    if match:
        return "list", [{"min_rating": float(match.group(1))}]
    match = re.match(r"^雞尾酒列表\s+(?:酒精濃度|abv)\s+([\d.]+)\s*%?$", text, re.I)
    if match:
        return "list", [{"min_abv": float(match.group(1))}]
    if lower in ("雞尾酒列表", "cocktail list", "list"):
        return "list", [{}]

    return "unknown", [text]


def handle_message(text: str, db_path: str = DB_DEFAULT) -> str:
    command, args = parse_command(text)
    if command == "help":
        return fmt_help()
    if command == "status":
        return fmt_status()
    if command == "stats":
        return fmt_cocktail_stats(db_path)
    if command == "search":
        return fmt_cocktail_search(db_path, args[0])
    if command == "info":
        return fmt_cocktail_info(db_path, args[0])
    if command == "list":
        return fmt_cocktail_list(db_path, **args[0])
    if command == "scrape":
        mode = args[0]
        with _scrape_lock:
            if _scrape_state["running"]:
                return f"⚠️ Difford's Guide 爬蟲任務目前正在執行中（模式：{_scrape_state['mode']}）。請勿重複啟動。"
            _scrape_state["running"] = True
            _scrape_state["mode"] = mode
            _scrape_state["started_at"] = time.time()
        try:
            _start_diffords(mode, db_path)
        except Exception as exc:
            with _scrape_lock:
                _scrape_state["running"] = False
                _scrape_state["mode"] = None
                _scrape_state["started_at"] = None
            logger.exception("Failed to start scraper")
            return f"❌ Difford's Guide 爬蟲任務啟動失敗：{exc}"
        return f"🚀 Difford's Guide 爬蟲任務已成功啟動（模式：{mode}）！"
    return "💡 無法識別此指令。請輸入「說明」查看所有可用指令！"


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "diffords-cocktails"}, 200


@app.route("/webhook", methods=["POST"])
def webhook():
    channel_id = os.getenv("LINE_CHANNEL_ID", "")
    channel_secret = os.getenv("LINE_CHANNEL_SECRET", "")
    if not channel_id or not channel_secret:
        abort(500)

    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")
    if not _verify_signature(body, signature, channel_secret):
        abort(400)

    payload = request.get_json(silent=True) or {}
    token = _get_cached_token(channel_id, channel_secret)
    if not token:
        abort(500)

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message") or {}
        if message.get("type") != "text":
            continue
        reply_token = event.get("replyToken")
        if not reply_token:
            continue
        text = message.get("text") or ""
        _reply(reply_token, handle_message(text), token)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Difford's Guide LINE Bot started at {datetime.now().isoformat()} on port {port}")
    app.run(host="0.0.0.0", port=port)
