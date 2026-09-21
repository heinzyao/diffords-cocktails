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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from flask import Flask, abort, request

from diffords_guide import nlp
from diffords_guide.config import DIFFORDS_DB_DEFAULT, GCS_DIFFORDS_DB_BLOB
from diffords_guide.notify import fetch_access_token

_ = load_dotenv()

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
MSG_LIMIT = 4900
SEARCH_LIMIT_DEFAULT = 5
LIST_LIMIT_DEFAULT = 10
RESULT_LIMIT_MAX = 20

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

_NLP_RATE_LIMIT = 10
_NLP_RATE_WINDOW = 60.0
_nlp_calls: dict[str, tuple[float, int]] = {}
_nlp_rate_lock = threading.Lock()


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


def _is_admin(user_id: str | None) -> bool:
    """只有 LINE_USER_ID 本人算管理員。

    沒設 LINE_USER_ID 就誰都不是 —— 預設關閉。這個 bot 會被分享給其他人，
    而爬蟲指令會開 Cloud Run Job 改寫共用的 GCS DB，不能讓任何人都打得動。
    """
    admin = os.getenv("LINE_USER_ID", "")
    return bool(admin) and user_id == admin


def _nlp_rate_ok(user_id: str | None) -> bool:
    """每人每分鐘最多 _NLP_RATE_LIMIT 次 LLM 呼叫，超過回 False。

    Gemini 配額綁的是 GCP 專案而非 API key，而這把 key 與 cat-lendar 共用同一個
    專案 —— 這個 bot 被朋友打爆時，那邊會一起沒配額。既有指令不走這條路，
    被擋的人改用「說明」裡的指令仍可無限查詢。

    ponytail: 計數是行程內的，max-instances 2 之下實際上限是兩倍；
    要精確就得把計數挪到 Firestore/Redis，目前的量級不值得。
    """
    now = time.time()
    key = user_id or ""
    with _nlp_rate_lock:
        # ponytail: 固定視窗計數，視窗交界最多放行兩倍額度；要嚴格就換 sliding window
        start, count = _nlp_calls.get(key, (0.0, 0))
        if now - start >= _NLP_RATE_WINDOW:
            start, count = now, 0
        if count >= _NLP_RATE_LIMIT:
            return False
        _nlp_calls[key] = (start, count + 1)
        # ponytail: 順手回收過期條目，省掉一個背景 GC 執行緒
        if len(_nlp_calls) > 500:
            for k, (s, _) in list(_nlp_calls.items()):
                if now - s >= _NLP_RATE_WINDOW:
                    del _nlp_calls[k]
        return True


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


def fmt_cocktail_search(
    db_path: str,
    keyword: str,
    *,
    sort: str = "rating",
    desc: bool = True,
    limit: int = SEARCH_LIMIT_DEFAULT,
    **filters: Any,
) -> str:
    """搜尋＝「帶名稱關鍵字的列表」：keyword 之外的條件與 fmt_cocktail_list 同規則疊加。"""
    active = {k: v for k, v in filters.items() if v is not None}

    # 標題要帶上疊加的條件：只講 keyword 會讓「找不到」看起來像關鍵字打錯，
    # 但真正篩掉結果的往往是後面那些條件。
    detail_parts = _condition_labels(active)
    if sort != "rating" or not desc:
        detail_parts.append(_sort_label(sort, desc))
    # 只在真的有東西可講時才加括號：裸搜尋不需要「（依評分降序）」這種預設值贅字
    detail = f"（{'・'.join(detail_parts)}）" if detail_parts else ""

    empty = (
        f"🔍 找不到符合「{keyword}」{detail}的雞尾酒，請放寬條件或換個關鍵字！"
        if detail_parts
        else f"🔍 找不到符合「{keyword}」的雞尾酒，請嘗試其他關鍵字！"
    )
    return _query_and_render(
        db_path,
        header=f"🔍 搜尋「{keyword}」{detail}的結果：",
        empty=empty,
        sort=sort,
        desc=desc,
        limit=limit,
        keyword=keyword,
        **active,
    )


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


_LIST_LABELS = {
    "min_sweet_sour": "甜酸 ≥ {}",
    "max_sweet_sour": "甜酸 ≤ {}",
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


def _condition_labels(active: dict[str, Any]) -> list[str]:
    return [_LIST_LABELS[k].format(v) for k, v in active.items() if k in _LIST_LABELS]


def _sort_label(sort: str, desc: bool) -> str:
    return f"依{_SORT_LABELS[sort]}{'降序' if desc else '升序'}"


def _query_and_render(
    db_path: str,
    *,
    header: str,
    empty: str,
    sort: str,
    desc: bool,
    limit: int,
    **active: Any,
) -> str:
    """查詢並渲染結果列表 —— 列表與搜尋唯一的查詢入口。

    兩者只有標題與查無結果的措辭不同（由呼叫端組好傳入），其餘流程完全相同。
    先前兩份各自實作時，搜尋那份漏了 **filters 而讓「雞尾酒搜尋 X 材料 Y」
    整個炸穿 Flask handler；收斂成單一入口就不會再有那種不同步。
    """
    limit = max(1, min(limit, RESULT_LIMIT_MAX))
    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"
    try:
        rows = storage.query_cocktails(sort=sort, desc=desc, limit=limit, **active)
    except ValueError as exc:
        return f"⚠️ {exc}"
    finally:
        storage.close()
    if not rows:
        return empty

    lines = [header, "──────────────────"]
    for idx, cocktail in enumerate(rows, 1):
        rating = cocktail.get("rating_value")
        rating_text = f"{rating:.1f} ★" if isinstance(rating, (int, float)) else "N/A"
        lines.append(f"{idx}. 🌟 {cocktail['name']} ({rating_text})")
    lines.extend([
        "──────────────────",
        "💡 輸入「雞尾酒酒譜 <酒名>」即可查看完整調製步驟！"
    ])
    return "\n".join(lines)


def fmt_cocktail_list(
    db_path: str,
    *,
    sort: str = "rating",
    desc: bool = True,
    limit: int = LIST_LIMIT_DEFAULT,
    **filters: Any,
) -> str:
    active = {k: v for k, v in filters.items() if v is not None}
    title_parts = _condition_labels(active)

    # 沒下任何條件時沿用舊的「社群高分精選」語意：5 票門檻
    if not active:
        active["min_count"] = 5
        title_parts = ["社群高分精選"]

    title_parts.append(_sort_label(sort, desc))
    title = "・".join(title_parts)

    return _query_and_render(
        db_path,
        header=f"📋 雞尾酒列表（{title}）",
        empty=f"🔍 找不到符合「{title}」篩選條件的雞尾酒。",
        sort=sort,
        desc=desc,
        limit=limit,
        **active,
    )


def fmt_semantic_search(
    db_path: str, query: str, filters: dict[str, Any], limit: int
) -> str:
    """風味語意檢索的結果。

    有結構化條件時先用 query_cocktails 篩出候選，再在子集裡做風味排序 ——
    「琴酒做的、苦苦的」要的是苦味的琴酒，不是任何一杯苦的酒。
    """
    from diffords_guide import embeddings

    storage = _open_storage(db_path)
    if storage is None:
        return "⚠️ 資料庫尚未建立，請先啟動爬蟲任務。"

    candidate_ids = None
    try:
        if filters:
            # 候選放寬到 200，讓語意排序有足夠的挑選空間
            rows = storage.query_cocktails(**{**filters, "limit": 200})
            candidate_ids = [r["id"] for r in rows]
            if not candidate_ids:
                return f"🔍 沒有符合條件的雞尾酒，請放寬條件再試一次！"
    finally:
        storage.close()

    results = embeddings.search(
        db_path, query, limit=limit, candidate_ids=candidate_ids
    )
    if results is None:
        return ""  # 索引未建立或 API 不可用 —— 由呼叫端退回舊行為
    if not results:
        return f"🔍 找不到喝起來像「{query}」的雞尾酒，換個說法試試？"

    detail = "・".join(_condition_labels(filters)) if filters else ""
    header = f"🍸 喝起來像「{query}」" + (f"（{detail}）" if detail else "") + "："
    lines = [header, "──────────────────"]
    for i, row in enumerate(results, 1):
        rating = row.get("rating_value")
        star = f" ({rating:.1f} ★)" if isinstance(rating, (int, float)) else ""
        lines.append(f"{i}. {row['name']}{star}")
        review = _truncate(row.get("review"), 60)
        if review:
            lines.append(f"   {review}")
    lines.append("──────────────────")
    lines.append("💡 輸入「雞尾酒酒譜 <酒名>」即可查看完整調製步驟！")
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
            "▪ 雞尾酒搜尋 <關鍵字> [N筆]",
            f"  搜尋名稱符合關鍵字的雞尾酒（預設 {SEARCH_LIMIT_DEFAULT} 筆，上限 {RESULT_LIMIT_MAX} 筆）",
            "▪ 雞尾酒酒譜 <酒名>",
            "  查詢指定雞尾酒的食材與作法",
            "",
            "📋 【精選與篩選】",
            "▪ 雞尾酒列表 [N筆]",
            f"  列出社群高分經典雞尾酒（預設 {LIST_LIMIT_DEFAULT} 筆，上限 {RESULT_LIMIT_MAX} 筆）",
            "▪ 條件可自由疊加：",
            "  材料 <材料>／標籤 <標籤>／描述 <關鍵字>",
            "  評分 <最低>／最高評分 <最高>",
            "  酒精濃度 <最低%>／最高酒精濃度 <最高%>",
            "  甜酸 <最低>／最高甜酸 <最高>（0-10，越高越酸、越低越甜）",
            "▪ 排序 <評分|酒精濃度|甜酸|卡路里|日期|名稱|評分數> [升序|降序]",
            "  預設依評分降序",
            "",
            "  例：雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆",
            "  例：雞尾酒列表 標籤 Classic/vintage 排序 卡路里 升序",
            "  例：雞尾酒搜尋 negroni 排序 日期",
            "",
            "💡 任一查詢皆可在句尾加「N筆」指定顯示筆數，例如「雞尾酒列表 材料 gin 15筆」",
            "",
            "💬 【直接用講的】",
            "▪ 記不住指令也沒關係，直接描述你想喝什麼",
            "  例：睡前喝的，3筆",
            "  例：有沒有不太烈的經典調酒",
            "  例：龍舌蘭做的，酸一點的",
            "  例：不要太甜的調酒",
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


_LIMIT_RE = re.compile(r"\s+(\d+)\s*筆$")

# 中文關鍵詞對英文資料（酒名／食材／標籤皆為英文）天然不衝突，
# 所以貪婪取值是安全的。
_GREEDY_KEYS = {"材料": "ingredient", "標籤": "tag", "描述": "description"}
_NUMERIC_KEYS = {
    "甜酸": "min_sweet_sour",
    "最高甜酸": "max_sweet_sour",
    "評分": "min_rating",
    "最高評分": "max_rating",
    "酒精濃度": "min_abv",
    # 英文鍵 "abv" 已移除：DB 裡有 31 列食材本身就叫 "Rye whiskey 50% abv"，
    # 貪婪值解析會把 "abv" 誤判成新條件關鍵詞。篩選用途已由「酒精濃度」涵蓋，
    # 排序仍可用 "abv"（見 _SORT_ALIASES，那邊沒有此衝突，故保留）。
    "最高酒精濃度": "max_abv",
}
_SORT_ALIASES = {
    "評分": "rating", "rating": "rating",
    "酒精濃度": "abv", "abv": "abv",
    "甜酸": "sweet_sour", "sweet_sour": "sweet_sour",
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


def _split_limit(text: str) -> tuple[str, int | None]:
    """切出指令尾端的「N筆」；未指定回傳 None。

    用「筆」當單位而非裸數字，是因為 158 款酒名本身以數字結尾（No. 2、Apollo 8）。
    """
    match = _LIMIT_RE.search(text)
    if not match:
        return text, None
    return text[: match.start()].strip(), int(match.group(1))


def parse_command(text: str) -> tuple[str, list[Any]]:
    text, limit = _split_limit(text.strip())
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

    match = re.match(r"^(?:雞尾酒酒譜|雞尾酒詳情|recipe|info)\s+(.+)$", text, re.I)
    if match:
        return "info", [match.group(1).strip()]

    match = re.match(r"^(?:雞尾酒列表|cocktail list|list)(?:\s+(.*))?$", text, re.I)
    if match:
        try:
            args = _scan_conditions((match.group(1) or "").split())
        except ValueError as exc:
            return "error", [f"⚠️ {exc}"]
        if limit:
            args["limit"] = limit
        return "list", [args]

    return "unknown", [text]


def handle_message(
    text: str, db_path: str = DB_DEFAULT, *, user_id: str | None = None
) -> str:
    command, args = parse_command(text)
    if command == "help":
        return fmt_help()
    if command == "status":
        return fmt_status()
    if command == "stats":
        return fmt_cocktail_stats(db_path)
    if command == "error":
        return args[0]
    if command == "search":
        return fmt_cocktail_search(db_path, **args[0])
    if command == "info":
        return fmt_cocktail_info(db_path, args[0])
    if command == "list":
        return fmt_cocktail_list(db_path, **args[0])
    if command == "scrape":
        if not _is_admin(user_id):
            return "⚠️ 「雞尾酒爬蟲」是管理員專用指令。輸入「說明」查看可用指令！"
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
    # 舊指令都沒命中 —— 交給 Gemini 試著解析成查詢條件。
    # 這是純加分路徑：解析不出來（或沒設 GEMINI_API_KEY）就照舊回提示。
    if command == "unknown":
        if not _nlp_rate_ok(user_id):
            return (
                "⏳ 你問得太快了，等一分鐘再試 —— "
                "或直接用「說明」裡的指令查詢，那些不限次數。"
            )
        nl_args = nlp.parse_query(args[0])
        if nl_args:
            logger.info("自然語言查詢：%r → %s", args[0], nl_args)
            semantic = nl_args.pop("semantic_query", None)
            if semantic:
                limit = nl_args.pop("limit", LIST_LIMIT_DEFAULT)
                # 風味描述交給語意檢索；空字串代表索引沒建好，繼續往下退回舊訊息
                rendered = fmt_semantic_search(db_path, semantic, nl_args, limit)
                if rendered:
                    return rendered
            elif nl_args:
                # 不另加開場白 —— fmt_cocktail_list 本來就會列出實際套用的篩選條件，
                # 使用者從那行就能看出有沒有被理解錯，多一句「幫你找到這些」
                # 在零結果時反而跟它的「找不到符合…」打架。
                return fmt_cocktail_list(db_path, **nl_args)

    return "💡 無法識別此指令。請輸入「說明」查看所有可用指令！"


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "diffords-cocktails"}, 200


def _handle_event(event: dict[str, Any], access_token: str) -> None:
    """處理單一 message event 並回覆。

    例外絕對不可逸出：這會在 executor 裡跑，漏出去的話那一則只會靜默沒回覆，
    而且看不到 traceback。
    """
    if event.get("type") != "message":
        return
    message = event.get("message") or {}
    if message.get("type") != "text":
        return
    reply_token = event.get("replyToken")
    if not reply_token:
        return
    text = message.get("text") or ""
    # 指令文法是自由輸入，解析路徑比舊的固定 regex 寬得多。少了這層保險，
    # 任何未預期的例外都會讓使用者只收到沉默。
    try:
        reply = handle_message(text, user_id=(event.get("source") or {}).get("userId"))
    except Exception:
        logger.exception("handle_message 失敗：%r", text)
        reply = "⚠️ 處理指令時發生未預期的錯誤，請稍後再試或輸入「說明」查看可用指令。"
    try:
        _reply(reply_token, reply, access_token)
    except Exception:
        logger.exception("回覆失敗：%r", text)


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

    events = payload.get("events", [])
    if len(events) == 1:
        _handle_event(events[0], token)
    elif events:
        # LINE 會把連打的訊息打包成一個 request。序列處理 N 則就是 N×2 秒，
        # 而 replyToken 約 60 秒過期 —— 後面幾則會來不及回而靜默失敗。
        # 併發讓同一批共用一個等待視窗，而不是排隊累加。
        # ponytail: 4 條夠用（gunicorn 本身已有 --threads 8）；真要削掉這 2 秒的
        # 回應時間得改成先回 200 再背景處理，那需要 Cloud Run 關掉 CPU 節流。
        with ThreadPoolExecutor(max_workers=min(len(events), 4)) as pool:
            for event in events:
                pool.submit(_handle_event, event, token)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Difford's Guide LINE Bot started at {datetime.now().isoformat()} on port {port}")
    app.run(host="0.0.0.0", port=port)
