"""音樂指令轉送：權限分流、服務錯誤處理、reply／push 時機。全部 mock，不打網路。"""

import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

import bot
from diffords_guide import music_client

ADMIN = "Uadmin"


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    monkeypatch.setenv("LINE_USER_ID", ADMIN)
    monkeypatch.setenv("MUSIC_SERVICE_URL", "https://music.example")


def _resp(status, body):
    r = MagicMock(status_code=status)
    r.json.return_value = body
    return r


@pytest.fixture
def service():
    """攔下 ID token 與 HTTP 呼叫。"""
    with (
        patch("google.oauth2.id_token.fetch_id_token", return_value="idtok") as tok,
        patch.object(music_client.requests, "request") as req,
    ):
        yield tok, req


def test_parse_music_commands_for_admin():
    p = lambda t: bot.parse_command(t, admin=True)
    assert p("音樂推薦 下雨天的爵士") == ("music", ["recommend", "下雨天的爵士", None, True])
    assert p("音樂預覽 下雨天的爵士 10首") == ("music", ["recommend", "下雨天的爵士", 10, False])
    assert p("音樂口味更新") == ("music", ["refresh"])
    assert p("音樂狀態") == ("music", ["status"])
    assert p("音樂推薦")[0] == "error"
    assert p("音樂推薦 " + "a" * 301)[0] == "error"
    assert p("音樂推薦 " + "a" * 300)[0] == "music"


@pytest.mark.parametrize("text", [
    "音樂推薦 下雨天的爵士", "音樂預覽 下雨天的爵士", "音樂口味更新", "音樂狀態",
    "音樂推薦", "音樂推薦 " + "a" * 301, "音樂說明", "music help",
])
def test_non_admin_music_is_indistinguishable_from_unknown(text, service):
    assert bot.parse_command(text) == ("unknown", [text.strip()])
    with patch.object(bot.nlp, "parse_query", return_value=None):
        for uid in ("Ustranger", None):
            assert bot.handle_message(text, user_id=uid) == bot.handle_message("隨便打的字", user_id=uid)
    service[1].assert_not_called()


def test_help_split_by_identity_and_chat():
    cocktail = bot.handle_message("雞尾酒說明", user_id="Ustranger")
    assert cocktail.startswith("🍹 Difford's Guide 雞尾酒助理指令") and "音樂" not in cocktail
    # 非管理員、或管理員在群組：「說明」維持原樣 = 雞尾酒說明，不提音樂
    for uid, private in (("Ustranger", True), (None, True), ("Ustranger", False), (ADMIN, False)):
        for cmd in ("說明", "help", "指令", "雞尾酒說明", "cocktail help"):
            assert bot.handle_message(cmd, user_id=uid, private=private) == cocktail
    # 管理員 1:1：總覽指向兩份說明，本身不列細項
    overview = bot.handle_message("說明", user_id=ADMIN)
    assert "「雞尾酒說明」" in overview and "「音樂說明」" in overview and "狀態" in overview
    assert "雞尾酒列表" not in overview and "音樂推薦 <" not in overview
    assert bot.handle_message("雞尾酒說明", user_id=ADMIN) == cocktail
    music = bot.handle_message("音樂說明", user_id=ADMIN)
    assert music == bot.handle_message("music help", user_id=ADMIN)
    for cmd in ("音樂推薦", "音樂預覽", "音樂口味更新", "音樂狀態"):
        assert f"例：{cmd}" in music
    assert "推薦約需 1 分鐘，超過時會先回覆再推播結果" in music


def test_admin_music_help_in_group_is_unknown():
    with patch.object(bot.nlp, "parse_query", return_value=None):
        assert bot.handle_message("音樂說明", user_id=ADMIN, private=False) == bot.handle_message(
            "隨便打的字", user_id=ADMIN, private=False
        )


def test_service_not_configured(monkeypatch, service):
    monkeypatch.setenv("MUSIC_SERVICE_URL", "")
    assert bot.handle_message("音樂預覽 爵士", user_id=ADMIN) == "🎧 音樂功能未啟用"
    service[1].assert_not_called()


def test_recommend_success_sends_id_token_and_payload(service):
    tok, req = service
    req.return_value = _resp(200, {"text": "1. So What", "playlist_url": None})
    assert bot.handle_message("音樂預覽 爵士 50首", user_id=ADMIN) == "1. So What"
    assert tok.call_args[0][1] == "https://music.example"
    _, kwargs = req.call_args
    assert req.call_args[0][1] == "https://music.example/recommend"
    assert kwargs["headers"]["Authorization"] == "Bearer idtok"
    assert kwargs["json"] == {"prompt": "爵士", "create_playlist": False, "n": 40}
    assert kwargs["timeout"] == 240


def test_503_relays_rate_limit_message(service):
    service[1].return_value = _resp(503, {"error": "Spotify 限流，約 14 小時後恢復"})
    assert "約 14 小時後恢復" in bot.handle_message("音樂推薦 爵士", user_id=ADMIN)


def test_timeout_and_other_errors(service):
    service[1].side_effect = requests.Timeout()
    assert "240 秒" in bot.handle_message("音樂推薦 爵士", user_id=ADMIN)
    service[1].side_effect = None
    service[1].return_value = _resp(500, {})
    assert "HTTP 500" in bot.handle_message("音樂推薦 爵士", user_id=ADMIN)
    service[0].side_effect = RuntimeError("no metadata server")
    assert "連不上" in bot.handle_message("音樂狀態", user_id=ADMIN)


def test_refresh_and_status(service):
    service[1].return_value = _resp(409, {"status": "running"})
    assert "重建中" in bot.handle_message("音樂口味更新", user_id=ADMIN)
    service[1].return_value = _resp(200, {"critics_count": 12, "refresh_running": False})
    assert "12 首" in bot.handle_message("音樂狀態", user_id=ADMIN)


def test_long_text_is_truncated_under_line_limit(service):
    service[1].return_value = _resp(200, {"text": "x" * 6000})
    assert len(bot.handle_message("音樂推薦 爵士", user_id=ADMIN)) <= 5000
    with patch.object(bot, "_post_line", return_value=True) as post:
        bot._push(ADMIN, "y" * 6000, "tok")
    assert len(post.call_args[0][1]["messages"][0]["text"]) <= 5000


def _event(text, user=ADMIN):
    return {"type": "message", "replyToken": "r", "source": {"userId": user},
            "message": {"type": "text", "text": text}}


def test_fast_result_uses_reply():
    with (
        patch.object(bot.music_client, "run", return_value="歌單"),
        patch.object(bot, "_post_line", return_value=True) as post,
    ):
        bot._handle_event(_event("音樂預覽 爵士"), "tok")
    urls = [c[0][0] for c in post.call_args_list]
    assert urls == [bot.LINE_LOADING_URL, bot.LINE_REPLY_URL]
    assert post.call_args[0][1]["messages"][0]["text"] == "歌單"


def test_slow_result_replies_then_pushes(monkeypatch):
    monkeypatch.setattr(bot, "MUSIC_REPLY_WAIT", 0.05)
    release = threading.Event()

    def slow(*_):
        release.wait(2)
        return "歌單"

    with (
        patch.object(bot.music_client, "run", side_effect=slow),
        patch.object(bot, "_post_line", side_effect=lambda url, *a: url != bot.LINE_REPLY_URL or release.set() or True) as post,
    ):
        bot._handle_music_event("音樂推薦 爵士", ADMIN, "r", "tok")
    calls = [(c[0][0], c[0][1]) for c in post.call_args_list]
    assert [u for u, _ in calls] == [bot.LINE_LOADING_URL, bot.LINE_REPLY_URL, bot.LINE_PUSH_URL]
    assert calls[1][1]["messages"][0]["text"] == "🎧 還在挑歌，完成後推播給你"
    assert calls[2][1] == {"to": ADMIN, "messages": [{"type": "text", "text": "歌單"}]}


def test_non_admin_event_skips_music_flow():
    with (
        patch.object(bot.nlp, "parse_query", return_value=None),
        patch.object(bot, "_post_line", return_value=True) as post,
    ):
        bot._handle_event(_event("音樂預覽 爵士", user="Ustranger"), "tok")
    assert [c[0][0] for c in post.call_args_list] == [bot.LINE_REPLY_URL]
    assert "說明" in post.call_args[0][1]["messages"][0]["text"]


def test_admin_in_group_gets_no_music():
    """群組裡的回覆其他成員都看得到：管理員在群組也要與 unknown 相同、說明不列音樂。"""
    group = {**_event("音樂預覽 爵士"), "source": {"type": "group", "groupId": "G", "userId": ADMIN}}
    help_in_group = {**group, "message": {"type": "text", "text": "說明"}}
    with (
        patch.object(bot.nlp, "parse_query", return_value=None),
        patch.object(bot.music_client, "run") as run,
        patch.object(bot, "_post_line", return_value=True) as post,
    ):
        bot._handle_event(group, "tok")
        bot._handle_event(help_in_group, "tok")
    run.assert_not_called()
    texts = [c[0][1]["messages"][0]["text"] for c in post.call_args_list]
    assert "無法識別" in texts[0] and "音樂" not in texts[1]
