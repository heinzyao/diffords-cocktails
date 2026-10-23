"""把音樂指令轉送給 music-recommender Cloud Run 服務。

服務不開放未驗證呼叫，用 bot 自己的 runtime SA 換 ID token（audience = 服務 URL）。
所有函式都回傳可直接顯示在 LINE 的繁中一句話，失敗也一樣 —— 例外不往外丟，
音樂功能壞掉不能拖垮雞尾酒指令。沒設 MUSIC_SERVICE_URL 就是未啟用。
"""

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 240
PROMPT_MAX = 300
N_MAX = 40

DISABLED = "🎧 音樂功能未啟用"


def _call(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]] | str:
    """呼叫服務；成功回 (status, json)，連不上／逾時回錯誤訊息字串。"""
    base = os.getenv("MUSIC_SERVICE_URL", "").strip().rstrip("/")
    if not base:
        return DISABLED

    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    try:
        token = id_token.fetch_id_token(Request(), base)
        resp = requests.request(
            method,
            base + path,
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.Timeout:
        return f"⏳ 音樂服務超過 {TIMEOUT} 秒沒回應，請稍後再試。"
    except Exception as exc:
        # 只記例外型別：訊息裡可能夾帶 token 或內部 URL
        logger.error("音樂服務呼叫失敗：%s", type(exc).__name__)
        return "⚠️ 連不上音樂服務，請稍後再試。"
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _error(status: int, body: dict[str, Any]) -> str:
    detail = str(body.get("error") or "")[:200]
    if status == 503:
        return f"⏳ {detail or 'Spotify 限流中，請過幾小時再試。'}"
    return f"⚠️ 音樂服務錯誤（HTTP {status}）{('：' + detail) if detail else ''}"


def recommend(prompt: str, n: int | None, create_playlist: bool) -> str:
    payload: dict[str, Any] = {"prompt": prompt, "create_playlist": create_playlist}
    if n is not None:
        payload["n"] = max(1, min(n, N_MAX))
    result = _call("POST", "/recommend", payload)
    if isinstance(result, str):
        return result
    status, body = result
    if status != 200:
        return _error(status, body)
    return str(body.get("text") or "🎧 服務沒有回傳任何歌曲。")


def refresh_taste() -> str:
    result = _call("POST", "/refresh-taste")
    if isinstance(result, str):
        return result
    status, body = result
    if status == 202:
        return "🎧 已開始重建口味檔，完成後「音樂狀態」會顯示新的建立時間。"
    if status == 409:
        return "🎧 口味檔正在重建中，請稍候。"
    return _error(status, body)


def status() -> str:
    result = _call("GET", "/status")
    if isinstance(result, str):
        return result
    code, body = result
    if code != 200:
        return _error(code, body)
    return "\n".join([
        "🎧 音樂服務狀態",
        f"口味檔建立：{body.get('taste_built_at') or '無'}",
        f"口味曲風：{body.get('taste_genres') or '未知'}",
        f"樂評曲目：{body.get('critics_count', 0)} 首（更新：{body.get('critics_updated_at') or '無'}）",
        f"口味重建中：{'是' if body.get('refresh_running') else '否'}",
    ])


def run(action: str, *args: Any) -> str:
    if action == "recommend":
        return recommend(*args)
    if action == "refresh":
        return refresh_taste()
    return status()
