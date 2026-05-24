"""
LineNotifier 單元測試
所有 HTTP 請求均使用 Mock，不需要真實 LINE API 連線
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from distiller_scraper.notify import LINE_PUSH_URL, LINE_TOKEN_URL, LineNotifier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def notifier():
    return LineNotifier(
        channel_id="test-id",
        channel_secret="test-secret",
        user_id="U1234567890",
    )


@pytest.fixture
def unconfigured_notifier():
    return LineNotifier(channel_id="", channel_secret="", user_id="")


def _mock_token_response():
    """建立成功的 token response mock。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": "mock-access-token"}
    return resp


def _mock_push_response(status_code=200):
    """建立 push message response mock。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "OK" if status_code == 200 else "Unauthorized"
    return resp


# ---------------------------------------------------------------------------
# 初始化與設定
# ---------------------------------------------------------------------------


class TestInit:
    def test_explicit_credentials(self, notifier):
        assert notifier.channel_id == "test-id"
        assert notifier.channel_secret == "test-secret"
        assert notifier.user_id == "U1234567890"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("LINE_CHANNEL_ID", "env-id")
        monkeypatch.setenv("LINE_CHANNEL_SECRET", "env-secret")
        monkeypatch.setenv("LINE_USER_ID", "U_env_user")
        n = LineNotifier()
        assert n.channel_id == "env-id"
        assert n.channel_secret == "env-secret"
        assert n.user_id == "U_env_user"

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LINE_CHANNEL_ID", "env-id")
        monkeypatch.setenv("LINE_CHANNEL_SECRET", "env-secret")
        monkeypatch.setenv("LINE_USER_ID", "U_env")
        n = LineNotifier(
            channel_id="explicit-id",
            channel_secret="explicit-secret",
            user_id="U_explicit",
        )
        assert n.channel_id == "explicit-id"
        assert n.channel_secret == "explicit-secret"
        assert n.user_id == "U_explicit"

    def test_missing_env_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("LINE_CHANNEL_ID", raising=False)
        monkeypatch.delenv("LINE_CHANNEL_SECRET", raising=False)
        monkeypatch.delenv("LINE_USER_ID", raising=False)
        n = LineNotifier()
        assert n.channel_id == ""
        assert n.channel_secret == ""
        assert n.user_id == ""


class TestIsConfigured:
    def test_configured(self, notifier):
        assert notifier.is_configured() is True

    def test_missing_channel_id(self):
        n = LineNotifier(channel_id="", channel_secret="s", user_id="U123")
        assert n.is_configured() is False

    def test_missing_channel_secret(self):
        n = LineNotifier(channel_id="id", channel_secret="", user_id="U123")
        assert n.is_configured() is False

    def test_missing_user_id(self):
        n = LineNotifier(channel_id="id", channel_secret="s", user_id="")
        assert n.is_configured() is False

    def test_all_missing(self, unconfigured_notifier):
        assert unconfigured_notifier.is_configured() is False


# ---------------------------------------------------------------------------
# _get_access_token()
# ---------------------------------------------------------------------------


class TestGetAccessToken:
    def test_success(self, notifier):
        with patch(
            "distiller_scraper.notify.requests.post",
            return_value=_mock_token_response(),
        ) as mock_post:
            token = notifier._get_access_token()
        assert token == "mock-access-token"
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == LINE_TOKEN_URL
        assert call_kwargs[1]["data"]["client_id"] == "test-id"
        assert call_kwargs[1]["data"]["client_secret"] == "test-secret"

    def test_api_error_returns_none(self, notifier):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = "Bad Request"
        with patch("distiller_scraper.notify.requests.post", return_value=resp):
            token = notifier._get_access_token()
        assert token is None

    def test_connection_error_returns_none(self, notifier):
        with patch(
            "distiller_scraper.notify.requests.post",
            side_effect=requests.ConnectionError,
        ):
            token = notifier._get_access_token()
        assert token is None


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestSend:
    def test_success(self, notifier):
        with patch.object(notifier, "_get_access_token", return_value="tok"):
            with patch(
                "distiller_scraper.notify.requests.post",
                return_value=_mock_push_response(),
            ) as mock_post:
                result = notifier.send("Hello")
        assert result is True
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == LINE_PUSH_URL
        assert call_kwargs[1]["json"]["to"] == "U1234567890"
        assert call_kwargs[1]["json"]["messages"] == [{"type": "text", "text": "Hello"}]
        assert "Bearer tok" in call_kwargs[1]["headers"]["Authorization"]

    def test_unconfigured_returns_false(self, unconfigured_notifier):
        with patch("distiller_scraper.notify.requests.post") as mock_post:
            result = unconfigured_notifier.send("Hello")
        assert result is False
        mock_post.assert_not_called()

    def test_token_failure_returns_false(self, notifier):
        with patch.object(notifier, "_get_access_token", return_value=None):
            result = notifier.send("Hello")
        assert result is False

    def test_push_http_error_returns_false(self, notifier):
        with patch.object(notifier, "_get_access_token", return_value="tok"):
            with patch(
                "distiller_scraper.notify.requests.post",
                return_value=_mock_push_response(401),
            ):
                result = notifier.send("Hello")
        assert result is False

    def test_push_connection_error_returns_false(self, notifier):
        with patch.object(notifier, "_get_access_token", return_value="tok"):
            with patch(
                "distiller_scraper.notify.requests.post",
                side_effect=requests.ConnectionError,
            ):
                result = notifier.send("Hello")
        assert result is False


# ---------------------------------------------------------------------------
# notify_success()
# ---------------------------------------------------------------------------


class TestNotifySuccess:
    def test_formats_message(self, notifier):
        stats = {
            "總記錄數": 150,
            "失敗 URL 數": 3,
            "類別分布": {"whiskey": 80, "gin": 70},
        }
        with patch.object(notifier, "send", return_value=True) as mock_send:
            result = notifier.notify_success("full", stats)
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "150" in msg
        assert "3" in msg
        assert "whiskey" in msg
        assert "gin" in msg
        assert "FULL" in msg

    def test_handles_english_stat_keys(self, notifier):
        stats = {
            "total_records": 42,
            "failed_urls": 0,
            "category_distribution": {"rum": 42},
        }
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", stats)
        msg = mock_send.call_args[0][0]
        assert "42" in msg
        assert "rum" in msg

    def test_empty_stats(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", {})
        msg = mock_send.call_args[0][0]
        assert "?" in msg

    def test_duration_shown_when_nonzero(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", {}, duration_secs=120)
        msg = mock_send.call_args[0][0]
        assert "2 分 0 秒" in msg
        assert "⏱" in msg

    def test_duration_hidden_when_zero(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", {}, duration_secs=0)
        msg = mock_send.call_args[0][0]
        assert "⏱" not in msg

    def test_page_errors_shown_when_nonzero(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", {}, page_errors=3)
        msg = mock_send.call_args[0][0]
        assert "頁面錯誤" in msg
        assert "3" in msg

    def test_page_errors_hidden_when_zero(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", {})
        msg = mock_send.call_args[0][0]
        assert "頁面錯誤" not in msg

    def test_diffords_stat_keys(self, notifier):
        stats = {
            "爬取新增": 50,
            "跳過（已是最新）": 200,
            "失敗": 3,
        }
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("incremental", stats, source="Difford's Guide")
        msg = mock_send.call_args[0][0]
        assert "50" in msg
        assert "3" in msg
        assert "200" in msg
        assert "跳過記錄" in msg
        assert "Difford's Guide" in msg

    def test_diffords_no_skipped_key(self, notifier):
        stats = {
            "爬取新增": 10,
            "失敗": 0,
        }
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_success("test", stats, source="Difford's Guide")
        msg = mock_send.call_args[0][0]
        assert "10" in msg
        assert "跳過記錄" not in msg


# ---------------------------------------------------------------------------
# notify_failure()
# ---------------------------------------------------------------------------


class TestNotifyFailure:
    def test_formats_message_with_error(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            result = notifier.notify_failure("full", "Driver crashed")
        assert result is True
        msg = mock_send.call_args[0][0]
        assert "Driver crashed" in msg
        assert "FULL" in msg

    def test_default_error_message(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure("test")
        msg = mock_send.call_args[0][0]
        assert "未知錯誤" in msg

    def test_page_errors_in_message(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure("full", "Some error", page_errors=5)
        msg = mock_send.call_args[0][0]
        assert "5" in msg

    def test_error_details_in_message(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure(
                "full", "Some error", error_details="url1 url2 url3"
            )
        msg = mock_send.call_args[0][0]
        assert "url1 url2 url3" in msg

    def test_zero_page_errors_and_empty_details_backward_compat(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            result_new = notifier.notify_failure(
                "full", "Driver crashed", page_errors=0, error_details=""
            )
        msg_new = mock_send.call_args[0][0]
        with patch.object(notifier, "send", return_value=True) as mock_send2:
            notifier.notify_failure("full", "Driver crashed")
        msg_old = mock_send2.call_args[0][0]
        assert result_new is True
        assert msg_new == msg_old

    def test_line_api_failure_returns_false_only(self, notifier):
        with patch.object(notifier, "send", return_value=False) as mock_send:
            result = notifier.notify_failure(
                "full", "Driver crashed", page_errors=3, error_details="url1"
            )
        assert result is False
        mock_send.assert_called_once()

    def test_page_errors_and_error_details_combined(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure(
                "full", "Timeout", page_errors=7, error_details="/whiskey /gin"
            )
        msg = mock_send.call_args[0][0]
        assert "7" in msg
        assert "/whiskey /gin" in msg

    def test_duration_shown_in_failure(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure("full", "Timeout", duration_secs=60)
        msg = mock_send.call_args[0][0]
        assert "1 分 0 秒" in msg
        assert "⏱" in msg

    def test_duration_hidden_when_zero_in_failure(self, notifier):
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.notify_failure("full", "Timeout", duration_secs=0)
        msg = mock_send.call_args[0][0]
        assert "⏱" not in msg
