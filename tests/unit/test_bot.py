from unittest.mock import MagicMock, patch

import bot
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_parse_cocktail_commands():
    assert bot.parse_command("雞尾酒統計") == ("stats", [])
    assert bot.parse_command("雞尾酒搜尋 negroni") == ("search", ["negroni", 5])
    assert bot.parse_command("雞尾酒搜尋 negroni 12筆") == ("search", ["negroni", 12])
    # 酒名以數字結尾時不該被當成筆數
    assert bot.parse_command("雞尾酒搜尋 Apollo 8") == ("search", ["Apollo 8", 5])
    assert bot.parse_command("雞尾酒酒譜 Negroni") == ("info", ["Negroni"])
    assert bot.parse_command("雞尾酒列表 材料 gin") == ("list", [{"ingredient": "gin"}])
    assert bot.parse_command("雞尾酒列表 評分 4.5") == ("list", [{"min_rating": 4.5}])
    assert bot.parse_command("雞尾酒列表 酒精濃度 15") == ("list", [{"min_abv": 15.0}])
    assert bot.parse_command("雞尾酒列表 abv 15%") == ("list", [{"min_abv": 15.0}])
    assert bot.parse_command("雞尾酒列表 15筆") == ("list", [{"limit": 15}])
    assert bot.parse_command("雞尾酒列表 材料 gin 15筆") == (
        "list",
        [{"ingredient": "gin", "limit": 15}],
    )
    assert bot.parse_command("雞尾酒列表 評分 4.5 3筆") == (
        "list",
        [{"min_rating": 4.5, "limit": 3}],
    )
    assert bot.parse_command("雞尾酒爬蟲 incremental") == ("scrape", ["incremental"])


def test_format_cocktail_info(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    result = bot.fmt_cocktail_info(str(db_path), "Negroni")

    assert "Negroni" in result
    assert "Tanqueray Gin" in result
    assert "STIR all ingredients" in result


def test_format_cocktail_list_by_abv(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    result = bot.fmt_cocktail_list(str(db_path), min_abv=10.0)

    assert "Negroni" in result
    assert "ABV >= 10.0%" in result


def test_handle_message_unknown():
    assert "說明" in bot.handle_message("not a command")


def test_start_scraper_sets_running_state(tmp_path):
    db_path = tmp_path / "diffords.db"
    with patch.object(bot, "_start_diffords") as mock_start:
        result = bot.handle_message("雞尾酒爬蟲 test", db_path=str(db_path))

    assert "成功啟動" in result
    mock_start.assert_called_once_with("test", str(db_path))
    assert bot._scrape_state["running"] is True


def test_webhook_replies_to_text_message(monkeypatch):
    monkeypatch.setenv("LINE_CHANNEL_ID", "id")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", "secret")
    body = '{"events":[{"type":"message","replyToken":"r","message":{"type":"text","text":"說明"}}]}'.encode()
    signature = bot.base64.b64encode(
        bot.hmac.new(b"secret", body, bot.hashlib.sha256).digest()
    ).decode()

    with (
        patch.object(bot, "_get_cached_token", return_value="token"),
        patch.object(bot, "_reply", return_value=True) as mock_reply,
    ):
        client = bot.app.test_client()
        resp = client.post(
            "/webhook",
            data=body,
            content_type="application/json",
            headers={"X-Line-Signature": signature},
        )

    assert resp.status_code == 200
    assert mock_reply.call_args[0][0] == "r"


def test_ensure_db_from_gcs_downloads_missing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "GCS_BUCKET", "bucket")
    db_path = tmp_path / "diffords.db"

    with (
        patch("diffords_guide.gcs_storage.download_db", return_value=True),
        patch("diffords_guide.gcs_storage.get_blob_updated_time", return_value=None),
    ):
        assert bot._ensure_db_from_gcs(str(db_path), "diffords.db") is True
