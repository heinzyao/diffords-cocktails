from unittest.mock import MagicMock, patch

import requests

from diffords_guide.notify import LINE_PUSH_URL, LINE_TOKEN_URL, LineNotifier


def test_notifier_reads_explicit_credentials():
    notifier = LineNotifier(channel_id="id", channel_secret="secret", user_id="user")

    assert notifier.is_configured() is True
    assert notifier.channel_id == "id"
    assert notifier.channel_secret == "secret"
    assert notifier.user_id == "user"


def test_send_posts_line_message():
    notifier = LineNotifier(channel_id="id", channel_secret="secret", user_id="user")
    response = MagicMock(status_code=200, text="OK")

    with (
        patch.object(notifier, "_get_access_token", return_value="token"),
        patch("diffords_guide.notify.requests.post", return_value=response) as mock_post,
    ):
        assert notifier.send("Hello") is True

    assert mock_post.call_args[0][0] == LINE_PUSH_URL
    assert mock_post.call_args[1]["json"]["to"] == "user"


def test_get_access_token_handles_request_error():
    notifier = LineNotifier(channel_id="id", channel_secret="secret", user_id="user")

    with patch("diffords_guide.notify.requests.post", side_effect=requests.ConnectionError):
        assert notifier._get_access_token() is None


def test_notify_success_defaults_to_diffords_source():
    notifier = LineNotifier(channel_id="id", channel_secret="secret", user_id="user")

    with patch.object(notifier, "send", return_value=True) as mock_send:
        assert notifier.notify_success("test", {"爬取新增": 3}) is True

    message = mock_send.call_args[0][0]
    assert "Difford's Guide" in message
    assert "3" in message
