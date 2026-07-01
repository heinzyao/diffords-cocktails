"""
LINE 推播通知模組 (LINE Messaging API Notifier)

負責處理專案執行狀態的自動化推播通知。
透過 LINE Messaging API 的 Push Message 端點，即時回報爬蟲或排程任務的成功與失敗狀態。
本模組採用動態取得短期 Access Token 的機制（使用 Channel ID 與 Channel Secret），無需維護長期 Token。

前置作業（環境變數設定）：
  - LINE_CHANNEL_ID: LINE 官方帳號的 Channel ID
  - LINE_CHANNEL_SECRET: LINE 官方帳號的 Channel Secret
  - LINE_USER_ID: 負責接收通知的目標使用者 ID
"""

import logging
import os
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

LINE_TOKEN_URL = "https://api.line.me/v2/oauth/accessToken"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

_SEP = "━━━━━━━━━━━━━━"


def fetch_access_token(channel_id: str, channel_secret: str) -> str | None:
    """用 Channel ID + Secret 向 LINE 換取短期 Access Token，失敗回傳 None。"""
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
    except requests.RequestException as exc:
        logger.error("LINE Token 請求失敗：%s", exc)
        return None
    if resp.status_code != 200:
        logger.warning("LINE Token 取得失敗：%s %s", resp.status_code, resp.text[:200])
        return None
    token = resp.json().get("access_token")
    return token if isinstance(token, str) else None


def _fmt_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs} 秒"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m} 分 {s} 秒"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h} 小時 {m} 分"


class LineNotifier:
    """
    LINE 推播通知器。

    封裝 LINE Messaging API 的 OAuth 驗證機制與發送邏輯，提供發送文字訊息、
    成功狀態報告與異常警報的高階方法。
    若初始化時未明確指定憑證，將自動嘗試從環境變數載入。
    """

    def __init__(
        self,
        channel_id: str | None = None,
        channel_secret: str | None = None,
        user_id: str | None = None,
    ):
        self.channel_id = (
            channel_id if channel_id is not None else os.getenv("LINE_CHANNEL_ID", "")
        )
        self.channel_secret = (
            channel_secret
            if channel_secret is not None
            else os.getenv("LINE_CHANNEL_SECRET", "")
        )
        self.user_id = user_id if user_id is not None else os.getenv("LINE_USER_ID", "")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def is_configured(self) -> bool:
        """當 channel_id、channel_secret、user_id 皆已設定時回傳 True。"""
        return bool(self.channel_id and self.channel_secret and self.user_id)

    def _get_access_token(self) -> str | None:
        return fetch_access_token(self.channel_id, self.channel_secret)

    def send(self, text: str) -> bool:
        """發送文字推播訊息，成功回傳 True。"""
        if not self.is_configured():
            logger.warning("LINE 憑證未設定，跳過通知。")
            return False

        token = self._get_access_token()
        if not token:
            return False

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        body = {
            "to": self.user_id,
            "messages": [{"type": "text", "text": text}],
        }
        try:
            resp = requests.post(LINE_PUSH_URL, headers=headers, json=body, timeout=10)
            if resp.status_code == 200:
                logger.info("LINE 通知發送成功。")
                return True
            else:
                logger.warning("LINE 通知發送失敗：%s %s", resp.status_code, resp.text)
                return False
        except requests.RequestException as exc:
            logger.error("LINE 通知發送失敗：%s", exc)
            return False

    def notify_success(
        self,
        mode: str,
        stats: dict,
        duration_secs: int = 0,
        source: str = "Difford's Guide",
    ) -> bool:
        """
        發送執行成功的狀態報告通知。

        Args:
            mode: 執行的模式名稱（如 'full', 'incremental'）。
            stats: 包含 '爬取新增' / '失敗' / '跳過（已是最新）' 等數據的統計字典。
            duration_secs: 執行總耗時（秒）。若大於 0 將顯示於通知內。
            source: 資料來源或系統名稱（預設為 "Difford's Guide"）。

        Returns:
            bool: 發送成功回傳 True，發生錯誤或未設定憑證回傳 False。
        """
        total = stats.get("爬取新增", "?")
        failed = stats.get("失敗", "?")
        skipped = stats.get("跳過（已是最新）", None)
        lines = [
            f"✅ 【{source} 資料更新完成】",
            _SEP,
            f"🕒 結束時間：{self._timestamp()}",
            f"⚙️ 執行模式：{mode.upper()}",
        ]
        if duration_secs > 0:
            lines.append(f"⏱ 總計耗時：{_fmt_duration(duration_secs)}")

        lines.extend(
            [
                "",
                "📊 執行結果",
                f"  • 總計記錄：{total} 筆",
                f"  • 失敗連結：{failed} 筆",
            ]
        )
        if skipped is not None:
            lines.append(f"  • 跳過記錄：{skipped} 筆（已是最新）")

        return self.send("\n".join(lines))

    def notify_failure(
        self,
        mode: str,
        error: str = "",
        duration_secs: int = 0,
        source: str = "Difford's Guide",
    ) -> bool:
        """
        發送執行失敗或發生異常的警報通知。

        Args:
            mode: 執行的模式名稱（如 'full', 'incremental'）。
            error: 主要錯誤訊息或例外狀況的簡要說明。
            duration_secs: 任務中斷前的已執行耗時（秒）。
            source: 資料來源或系統名稱（預設為 "Difford's Guide"）。

        Returns:
            bool: 發送成功回傳 True，發生錯誤或未設定憑證回傳 False。
        """
        lines: list[str] = [
            f"❌ 【{source} 執行發生異常】",
            _SEP,
            f"🕒 中斷時間：{self._timestamp()}",
            f"⚙️ 執行模式：{mode.upper()}",
        ]
        if duration_secs > 0:
            lines.append(f"⏱ 中斷前耗時：{_fmt_duration(duration_secs)}")

        lines.extend(
            [
                "",
                "⚠️ 錯誤原因",
                f"  {error or '未知錯誤，請檢查系統日誌。'}",
            ]
        )
        return self.send("\n".join(lines))
