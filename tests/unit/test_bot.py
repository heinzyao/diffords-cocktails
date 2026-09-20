from unittest.mock import MagicMock, patch

import bot
from diffords_guide.storage import DiffordsStorage
from tests.unit.test_diffords import _sample_cocktail


def test_parse_cocktail_commands():
    assert bot.parse_command("雞尾酒統計") == ("stats", [])
    assert bot.parse_command("雞尾酒搜尋 negroni") == (
        "search", [{"keyword": "negroni", "limit": 5}])
    assert bot.parse_command("雞尾酒搜尋 negroni 12筆") == (
        "search", [{"keyword": "negroni", "limit": 12}])
    # 酒名以數字結尾時不該被當成筆數
    assert bot.parse_command("雞尾酒搜尋 Apollo 8") == (
        "search", [{"keyword": "Apollo 8", "limit": 5}])
    assert bot.parse_command("雞尾酒酒譜 Negroni") == ("info", ["Negroni"])
    assert bot.parse_command("雞尾酒列表 材料 gin") == ("list", [{"ingredient": "gin"}])
    assert bot.parse_command("雞尾酒列表 評分 4.5") == ("list", [{"min_rating": 4.5}])
    assert bot.parse_command("雞尾酒列表 酒精濃度 15") == ("list", [{"min_abv": 15.0}])
    # 英文鍵 "abv" 已從 _NUMERIC_KEYS 移除（會與真實食材名衝突，見 bot.py 註解），
    # 改用中文「酒精濃度」驗證同樣行為；"abv 15%" 現在會被當成未知條件。
    assert bot.parse_command("雞尾酒列表 酒精濃度 15%") == ("list", [{"min_abv": 15.0}])
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


def test_parse_ingredient_value_containing_the_word_abv():
    """DB 中真的有食材叫 "Rye whiskey 50% abv"（31 列）；"abv" 移除後
    不再被誤判成篩選關鍵詞，材料的貪婪值解析會把它整段吃下去。"""
    assert bot.parse_command("雞尾酒列表 材料 Rye whiskey 50% abv") == (
        "list",
        [{"ingredient": "Rye whiskey 50% abv"}],
    )


def test_parse_combined_conditions():
    assert bot.parse_command("雞尾酒列表 材料 gin 評分 4.2 酒精濃度 20 排序 abv 降序 15筆") == (
        "list",
        [{"ingredient": "gin", "min_rating": 4.2, "min_abv": 20.0,
          "sort": "abv", "desc": True, "limit": 15}],
    )


def test_parse_multiword_value():
    """材料值吃到下一個關鍵詞為止，支援多詞。"""
    assert bot.parse_command("雞尾酒列表 材料 dry vermouth 描述 citrus") == (
        "list",
        [{"ingredient": "dry vermouth", "description": "citrus"}],
    )


def test_parse_sort_value_shadowing_a_filter_keyword():
    """排序後恰好取一個 token：第二個「酒精濃度」是 sort key 不是篩選條件。"""
    assert bot.parse_command("雞尾酒列表 酒精濃度 20 排序 酒精濃度") == (
        "list",
        [{"min_abv": 20.0, "sort": "abv"}],
    )


def test_parse_ascending_flag():
    assert bot.parse_command("雞尾酒列表 標籤 Sour 排序 卡路里 升序") == (
        "list",
        [{"tag": "Sour", "sort": "calories", "desc": False}],
    )


def test_parse_search_with_sort():
    assert bot.parse_command("雞尾酒搜尋 negroni 排序 日期") == (
        "search",
        [{"keyword": "negroni", "sort": "date", "limit": 5}],
    )


def test_parse_search_without_keyword_is_error():
    command, args = bot.parse_command("雞尾酒搜尋 排序 abv")
    assert command == "error"
    assert "關鍵字" in args[0]


def test_parse_unknown_token_reports_error():
    command, args = bot.parse_command("雞尾酒列表 顏色 紅色")
    assert command == "error"
    assert "顏色" in args[0]


def test_parse_non_numeric_value_reports_error():
    command, args = bot.parse_command("雞尾酒列表 評分 高")
    assert command == "error"
    assert "評分" in args[0]


def test_parse_unknown_sort_key_reports_error():
    command, args = bot.parse_command("雞尾酒列表 排序 顏色")
    assert command == "error"
    assert "排序" in args[0]


def test_handle_message_surfaces_parse_error(tmp_path):
    result = bot.handle_message("雞尾酒列表 顏色 紅色", str(tmp_path / "t.db"))
    assert "顏色" in result


def test_fmt_cocktail_list_combines_and_sorts(tmp_path):
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, abv in [(1, "Weak Gin", 10.0), (2, "Strong Gin", 45.0)]:
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": 4.5, "rating_count": 20, "abv": abv,
                "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
            }) is True

    result = bot.fmt_cocktail_list(str(db), ingredient="gin", sort="abv", desc=True)
    assert result.index("Strong Gin") < result.index("Weak Gin")
    assert "gin" in result


def test_format_cocktail_info(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    result = bot.fmt_cocktail_info(str(db_path), "Negroni")

    assert "Negroni" in result
    assert "Tanqueray Gin" in result
    assert "STIR all ingredients" in result


def test_search_combines_keyword_and_ingredient_filter(tmp_path):
    """雞尾酒搜尋 negroni 材料 gin：keyword 與 ingredient 需同時套用。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Negroni Sbagliato",
            "url": "https://www.diffordsguide.com/cocktails/recipe/101/negroni-sbagliato",
            "rating_value": 4.0, "rating_count": 20,
            "ingredients_html": [{"sort_order": 0, "item": "Prosecco", "amount": "60ml"}],
        }) is True
        assert st.save_cocktail({
            "name": "Classic Negroni",
            "url": "https://www.diffordsguide.com/cocktails/recipe/102/classic-negroni",
            "rating_value": 4.5, "rating_count": 30,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True

    result = bot.handle_message("雞尾酒搜尋 negroni 材料 gin", str(db))

    assert "Classic Negroni" in result
    assert "Negroni Sbagliato" not in result


def test_search_combines_keyword_and_description_filter(tmp_path):
    """雞尾酒搜尋 negroni 描述 citrus：keyword 與 description 需同時套用。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Negroni Twist",
            "url": "https://www.diffordsguide.com/cocktails/recipe/401/negroni-twist",
            "description": "A citrus-forward variation.",
            "rating_value": 4.0, "rating_count": 10,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True
        assert st.save_cocktail({
            "name": "Negroni Bitter",
            "url": "https://www.diffordsguide.com/cocktails/recipe/402/negroni-bitter",
            "description": "A bold, bitter classic.",
            "rating_value": 4.0, "rating_count": 10,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True

    result = bot.handle_message("雞尾酒搜尋 negroni 描述 citrus", str(db))

    assert "Negroni Twist" in result
    assert "Negroni Bitter" not in result


def test_search_combines_keyword_and_min_rating_filter(tmp_path):
    """雞尾酒搜尋 negroni 評分 4.0：keyword 與 min_rating 需同時套用。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Negroni Low",
            "url": "https://www.diffordsguide.com/cocktails/recipe/501/negroni-low",
            "rating_value": 3.0, "rating_count": 10,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True
        assert st.save_cocktail({
            "name": "Negroni High",
            "url": "https://www.diffordsguide.com/cocktails/recipe/502/negroni-high",
            "rating_value": 4.8, "rating_count": 10,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True

    result = bot.handle_message("雞尾酒搜尋 negroni 評分 4.0", str(db))

    assert "Negroni High" in result
    assert "Negroni Low" not in result


def test_search_filter_with_sort_orders_correctly(tmp_path):
    """雞尾酒搜尋 negroni 材料 gin 排序 酒精濃度 降序：篩選與排序需同時生效。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        for cid, name, abv in [(201, "Negroni Weak", 10.0), (202, "Negroni Strong", 45.0)]:
            assert st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/x",
                "rating_value": 4.0, "rating_count": 10, "abv": abv,
                "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
            }) is True

    result = bot.handle_message("雞尾酒搜尋 negroni 材料 gin 排序 酒精濃度 降序", str(db))

    assert result.index("Negroni Strong") < result.index("Negroni Weak")


def test_search_basic_and_sort_by_date_unaffected(tmp_path):
    """既有搜尋行為（無條件／僅排序）不因 **filters 改動而變化。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Negroni",
            "url": "https://www.diffordsguide.com/cocktails/recipe/301/negroni",
            "rating_value": 4.5, "rating_count": 100, "date_published": "2020-01-01",
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True

    result = bot.handle_message("雞尾酒搜尋 negroni", str(db))
    assert "Negroni" in result
    assert "🔍 搜尋「negroni」的結果：" in result

    result_sorted = bot.handle_message("雞尾酒搜尋 negroni 排序 日期", str(db))
    assert "Negroni" in result_sorted


def test_format_cocktail_list_by_abv(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

    result = bot.fmt_cocktail_list(str(db_path), min_abv=10.0)

    assert "Negroni" in result
    assert "ABV ≥ 10.0%" in result


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


def test_search_title_and_empty_message_mention_filters(tmp_path):
    """搜尋標題要帶上疊加條件：只講 keyword 會讓「找不到」看起來像關鍵字打錯，
    但真正篩掉結果的往往是後面那些條件。"""
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Negroni",
            "url": "https://www.diffordsguide.com/cocktails/recipe/1/x",
            "rating_value": 4.5, "rating_count": 20, "abv": 24.0,
            "ingredients_html": [{"sort_order": 0, "item": "Gin", "amount": "30ml"}],
        }) is True

    # 有結果時，標題帶出條件與排序
    hit = bot.fmt_cocktail_search(str(db), "negroni", ingredient="gin", sort="abv")
    assert "含有「gin」" in hit
    assert "依ABV降序" in hit
    assert "Negroni" in hit

    # 查無結果時也要說明是哪個條件擋掉的，而不是只怪關鍵字
    miss = bot.fmt_cocktail_search(str(db), "negroni", ingredient="campari")
    assert "含有「campari」" in miss
    assert "放寬條件" in miss


# --- 自然語言查詢（階段 1）-------------------------------------------


def test_known_commands_never_call_the_llm(tmp_path):
    """舊指令必須完全不經過 Gemini —— 零延遲、零成本是這條路徑的前提。"""
    db = str(tmp_path / "t.db")

    with patch("diffords_guide.nlp.parse_query") as spy:
        for msg in ("說明", "狀態", "雞尾酒統計", "雞尾酒列表 材料 gin",
                    "雞尾酒搜尋 negroni", "雞尾酒列表 顏色 紅色"):
            bot.handle_message(msg, db)

    spy.assert_not_called()


def test_unknown_message_falls_through_to_natural_language(tmp_path):
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as st:
        assert st.save_cocktail({
            "name": "Gin Fizz",
            "url": "https://www.diffordsguide.com/cocktails/recipe/7/gin-fizz",
            "rating_value": 4.5, "rating_count": 100,
            "ingredients_generic": [{"sort_order": 1, "amount": "45 ml", "item": "Gin"}],
        })

    with patch("diffords_guide.nlp.parse_query", return_value={"ingredient": "gin"}) as spy:
        result = bot.handle_message("幫我找琴酒調酒", str(db))

    spy.assert_called_once_with("幫我找琴酒調酒")
    assert "Gin Fizz" in result


def test_unknown_message_keeps_old_error_when_llm_declines(tmp_path):
    """LLM 解析不出來（回 None）時，行為必須跟加這條路徑之前一模一樣。"""
    with patch("diffords_guide.nlp.parse_query", return_value=None):
        result = bot.handle_message("今天天氣如何", str(tmp_path / "t.db"))

    assert "無法識別此指令" in result


def test_semantic_query_routes_to_vector_search(tmp_path):
    """風味描述要走語意檢索，並把其他條件當成候選篩選。"""
    db = str(tmp_path / "t.db")
    parsed = {"ingredient": "gin", "semantic_query": "苦苦的", "limit": 5}
    hits = [{"id": 1, "name": "Negroni", "review": "Bittersweet.", "rating_value": 4.8}]

    with patch("diffords_guide.nlp.parse_query", return_value=parsed), \
         patch("diffords_guide.embeddings.search", return_value=hits) as search, \
         patch("bot._open_storage") as open_storage:
        open_storage.return_value.query_cocktails.return_value = [{"id": 1}]
        result = bot.handle_message("琴酒做的，苦苦的", db)

    # semantic_query 與 limit 不該混進 query_cocktails 的篩選條件
    assert open_storage.return_value.query_cocktails.call_args.kwargs["ingredient"] == "gin"
    assert "semantic_query" not in open_storage.return_value.query_cocktails.call_args.kwargs
    assert search.call_args.kwargs["candidate_ids"] == [1]
    assert search.call_args.kwargs["limit"] == 5
    assert "Negroni" in result


def test_semantic_query_falls_back_when_index_missing(tmp_path):
    """索引還沒建好時（search 回 None），行為要退回原本的錯誤訊息。

    DB 本身必須存在 —— 不然走的是「資料庫尚未建立」那條分支，測不到這件事。
    """
    from diffords_guide.storage import DiffordsStorage

    db = tmp_path / "t.db"
    DiffordsStorage(str(db)).close()

    with patch("diffords_guide.nlp.parse_query", return_value={"semantic_query": "煙燻味"}), \
         patch("diffords_guide.embeddings.search", return_value=None):
        result = bot.handle_message("有煙燻味的酒", str(db))

    assert "無法識別此指令" in result


def test_every_condition_label_renders_its_value():
    """_LIST_LABELS 走 .format(v)，漏掉 {} 的項目會顯示成「甜酸 ≥」這種沒有數值的標籤。"""
    for key, template in bot._LIST_LABELS.items():
        assert "{}" in template, f"{key} 的標籤少了 {{}} 佔位符"

    rendered = bot._condition_labels({"min_sweet_sour": 6, "ingredient": "gin"})
    assert "甜酸 ≥ 6" in rendered
    assert "含有「gin」" in rendered
