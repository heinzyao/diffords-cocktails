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
_SEP_LIGHT = "──────────────"


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
        """用 Channel ID + Secret 產生短期 Access Token。"""
        try:
            resp = requests.post(
                LINE_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.channel_id,
                    "client_secret": self.channel_secret,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning(
                    "LINE Token 取得失敗：%s %s", resp.status_code, resp.text
                )
                return None
            return resp.json()["access_token"]
        except requests.RequestException as exc:
            logger.error("LINE Token 請求失敗：%s", exc)
            return None

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
        page_errors: int = 0,
        source: str = "Distiller",
    ) -> bool:
        """
        發送執行成功的狀態報告通知。

        Args:
            mode: 執行的模式名稱（如 'full', 'incremental'）。
            stats: 包含 '總記錄數' / '失敗 URL 數' / '類別分布' 等數據的統計字典。
            duration_secs: 執行總耗時（秒）。若大於 0 將顯示於通知內。
            page_errors: 執行過程中的頁面層級錯誤總數。
            source: 資料來源或系統名稱（預設為 'Distiller'）。

        Returns:
            bool: 發送成功回傳 True，發生錯誤或未設定憑證回傳 False。
        """
        total = stats.get(
            "總記錄數",
            stats.get("total_records", stats.get("爬取新增", "?")),
        )
        failed = stats.get(
            "失敗 URL 數",
            stats.get("failed_urls", stats.get("失敗", "?")),
        )
        skipped = stats.get("跳過（已是最新）", None)
        categories = stats.get("類別分布", stats.get("category_distribution", {}))
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
        if page_errors > 0:
            lines.append(f"  • 頁面錯誤：{page_errors} 筆")

        if categories:
            cat_total = (
                sum(categories.values())
                if all(isinstance(v, (int, float)) for v in categories.values())
                else 0
            )
            lines.append("")
            lines.append("📂 資料類別分布")
            lines.append(_SEP_LIGHT)
            for k, v in categories.items():
                if cat_total > 0 and isinstance(v, (int, float)):
                    bar_len = round(v / cat_total * 10)
                    bar = "█" * bar_len + "░" * (10 - bar_len)
                    lines.append(f"  {k:<10} {bar} {v} 筆")
                else:
                    lines.append(f"  {k:<10} {v} 筆")

        return self.send("\n".join(lines))

    def notify_failure(
        self,
        mode: str,
        error: str = "",
        page_errors: int = 0,
        error_details: str = "",
        duration_secs: int = 0,
        source: str = "Distiller",
    ) -> bool:
        """
        發送執行失敗或發生異常的警報通知。

        Args:
            mode: 執行的模式名稱（如 'full', 'incremental'）。
            error: 主要錯誤訊息或例外狀況的簡要說明。
            page_errors: 執行過程中的頁面層級錯誤總數。
            error_details: 詳細的錯誤堆疊 (Traceback) 或附加的診斷資訊。
            duration_secs: 任務中斷前的已執行耗時（秒）。
            source: 資料來源或系統名稱（預設為 'Distiller'）。

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
        if page_errors > 0:
            lines.append(f"  • 頁面錯誤數：{page_errors} 筆")

        if error_details:
            lines.append("")
            lines.append("📋 錯誤詳情")
            lines.append(_SEP_LIGHT)
            lines.append(error_details)
        return self.send("\n".join(lines))
