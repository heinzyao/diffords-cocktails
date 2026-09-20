"""自然語言查詢解析的測試。

重點不在 Gemini 回什麼（那是 mock），而在兩件事：
  1. sanitize() 擋得住不合法的輸出 —— 它是 LLM 與 SQL 之間唯一的防線
  2. 任何失敗都必須回傳 None，讓 bot 乾淨退回舊行為
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from diffords_guide import nlp


# --- sanitize：白名單與範圍檢查 ---------------------------------------


def test_sanitize_keeps_only_whitelisted_keys():
    out = nlp.sanitize({
        "ingredient": "gin",
        "min_rating": 4.2,
        "sort": "abv",
        "limit": 5,
        # 以下都不在白名單內，必須被丟棄
        "db_path": "/etc/passwd",
        "sql": "DROP TABLE cocktails",
        "order_by": "1; DELETE FROM cocktails",
    })

    assert out == {"ingredient": "gin", "min_rating": 4.2, "sort": "abv", "limit": 5}


def test_sanitize_rejects_sort_outside_whitelist():
    """sort 是唯一會被拼進 SQL 的值（storage._SORT_COLUMNS），必須嚴格。"""
    assert "sort" not in nlp.sanitize({"sort": "rating; DROP TABLE cocktails"})
    assert "sort" not in nlp.sanitize({"sort": "c.rating_value"})
    assert nlp.sanitize({"sort": "rating"})["sort"] == "rating"


@pytest.mark.parametrize(
    "payload,key",
    [
        ({"min_rating": 9.9}, "min_rating"),      # 評分上限 5
        ({"max_rating": -1}, "max_rating"),
        ({"min_abv": 500}, "min_abv"),            # ABV 上限 100
        ({"limit": 0}, "limit"),                  # 筆數至少 1
        ({"limit": 9999}, "limit"),               # 筆數上限 20
        ({"min_rating": "4.5"}, "min_rating"),    # 字串不算數值
        ({"ingredient": "   "}, "ingredient"),    # 空白字串
        ({"ingredient": 123}, "ingredient"),      # 型別錯誤
    ],
)
def test_sanitize_drops_out_of_range_or_wrong_type(payload, key):
    assert key not in nlp.sanitize(payload)


def test_sanitize_does_not_treat_bool_as_number():
    """bool 是 int 的子類別，若不先擋掉，True 會變成 min_rating=1.0。"""
    assert "min_rating" not in nlp.sanitize({"min_rating": True})
    assert nlp.sanitize({"desc": False})["desc"] is False


def test_sanitize_keeps_valid_fields_when_one_is_bad():
    """一個欄位不合法不該讓整次查詢失敗。"""
    out = nlp.sanitize({"ingredient": "rum", "min_rating": 99, "sort": "abv"})

    assert out == {"ingredient": "rum", "sort": "abv"}


# --- parse_query：失敗一律回 None ---------------------------------------


def _mock_genai(response_text):
    """讓 parse_query 內部的 `from google import genai` 拿到假的 client。"""
    client = MagicMock()
    client.models.generate_content.return_value = MagicMock(text=response_text)
    genai_mod = MagicMock()
    genai_mod.Client.return_value = client
    return genai_mod, client


def test_parse_query_returns_none_without_api_key(monkeypatch):
    """沒設 key 時整條路徑停用，不該拋例外。"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert nlp.parse_query("幫我找琴酒調酒") is None


def test_parse_query_returns_args_on_valid_response(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    genai_mod, client = _mock_genai(json.dumps({"ingredient": "gin", "min_rating": 4.0}))

    with patch("google.genai.Client", genai_mod.Client):
        out = nlp.parse_query("幫我找評價好的琴酒調酒")

    assert out == {"ingredient": "gin", "min_rating": 4.0}
    # 確認真的走到 API 呼叫，而不是被某個提早 return 繞過去
    client.models.generate_content.assert_called_once()
    assert client.models.generate_content.call_args.kwargs["contents"] == (
        "幫我找評價好的琴酒調酒"
    )


def test_parse_query_returns_none_on_timeout(monkeypatch):
    """逾時必須安靜退回，不能讓例外冒到 webhook。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    genai_mod, client = _mock_genai("")
    client.models.generate_content.side_effect = TimeoutError("deadline exceeded")

    with patch("google.genai.Client", genai_mod.Client):
        assert nlp.parse_query("隨便給我一杯") is None


def test_parse_query_returns_none_on_non_json(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    genai_mod, _ = _mock_genai("抱歉，我不太確定你的意思。")

    with patch("google.genai.Client", genai_mod.Client):
        assert nlp.parse_query("今天天氣如何") is None


def test_parse_query_returns_none_when_nothing_usable_extracted(monkeypatch):
    """空物件（LLM 判定不是查詢）與全部不合法，都等同解析失敗。"""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    for payload in ("{}", json.dumps({"min_rating": 99, "sort": "bogus"})):
        genai_mod, _ = _mock_genai(payload)
        with patch("google.genai.Client", genai_mod.Client):
            assert nlp.parse_query("閒聊") is None
