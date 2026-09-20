"""query_cocktails() 組合查詢的行為測試。"""

import pytest

from diffords_guide.storage import SORT_KEYS, DiffordsStorage


def _cocktail(cid, name, *, rating=None, count=None, abv=None,
              calories=None, date=None, tags=None, desc=None,
              ingredients=None):
    """組出 save_cocktail() 吃的最小 payload。

    兩個非顯而易見的要求：
    - id 是從 url 的 /cocktails/recipe/{id}/{slug} 解析出來的，payload 的 "id" 鍵不被讀取
    - ingredients_html 的每個元素必須有 sort_order，缺了會 KeyError，
      而 save_cocktail 會吞掉例外只回 False，導致測試拿到空 DB
    """
    return {
        "name": name,
        "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/{name.lower().replace(' ', '-')}",
        "description": desc,
        "rating_value": rating,
        "rating_count": count,
        "abv": abv,
        "calories": calories,
        "date_published": date,
        "tags": tags or [],
        "ingredients_html": [
            {"sort_order": i, "item": item, "amount": "30ml"}
            for i, item in enumerate(ingredients or [])
        ],
    }


@pytest.fixture
def storage(tmp_path):
    """五筆固定資料，涵蓋 NULL、同分、多標籤與多食材。"""
    with DiffordsStorage(str(tmp_path / "t.db")) as st:
        # save_cocktail 吞例外只回 False，所以每筆都要斷言，否則失敗會靜默
        assert st.save_cocktail(_cocktail(
            1, "Negroni", rating=4.5, count=100, abv=24.0, calories=200,
            date="2020-01-01", tags=["Classic/vintage"], desc="bitter citrus",
            ingredients=["Gin", "Campari"])) is True
        assert st.save_cocktail(_cocktail(
            2, "Martini", rating=4.5, count=50, abv=30.0, calories=180,
            date="2021-01-01", tags=["Classic/vintage"], desc="dry and cold",
            ingredients=["Gin", "Dry Vermouth"])) is True
        assert st.save_cocktail(_cocktail(
            3, "Daiquiri", rating=4.0, count=80, abv=20.0, calories=150,
            date="2019-01-01", tags=["Sour"], desc="fresh citrus",
            ingredients=["Rum", "Lime Juice"])) is True
        # abv / calories 為 NULL，用來驗證 NULLS LAST
        assert st.save_cocktail(_cocktail(
            4, "Mystery", rating=3.0, count=10, tags=["Sour"],
            ingredients=["Gin"])) is True
        # rating_count < 5，用來驗證 min_count 預設不吃掉資料
        assert st.save_cocktail(_cocktail(
            5, "Obscure Gin Thing", rating=5.0, count=1, abv=40.0,
            calories=90, date="2022-01-01", ingredients=["Gin"])) is True
        yield st


def test_conditions_are_combined_not_exclusive(storage):
    """材料 + 評分 + ABV 三個條件同時生效（舊版是 if/elif 只會套用一個）。

    Negroni 被 abv 28 濾掉、Daiquiri 被評分 4.2 濾掉、Mystery 無 abv 被濾掉。
    剩下兩筆依評分降序。
    """
    rows = storage.query_cocktails(ingredient="gin", min_rating=4.2, min_abv=28)
    assert [r["name"] for r in rows] == ["Obscure Gin Thing", "Martini"]


def test_no_filters_returns_everything(storage):
    rows = storage.query_cocktails(limit=100)
    assert len(rows) == 5


def test_min_count_defaults_to_none_and_keeps_low_vote_rows(storage):
    """min_count 預設 None：只有 1 票的 Obscure Gin Thing 必須留著。"""
    names = [r["name"] for r in storage.query_cocktails(ingredient="gin", limit=100)]
    assert "Obscure Gin Thing" in names
    filtered = storage.query_cocktails(ingredient="gin", min_count=5, limit=100)
    assert "Obscure Gin Thing" not in [r["name"] for r in filtered]


def test_tag_filter_uses_json_each(storage):
    rows = storage.query_cocktails(tag="Classic/vintage", limit=100)
    assert sorted(r["name"] for r in rows) == ["Martini", "Negroni"]


def test_tag_filter_survives_null_tags(storage):
    """id=5 沒有 tags；json_each 不該炸，也不該誤中。"""
    rows = storage.query_cocktails(tag="Sour", limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri", "Mystery"]


def test_description_filter(storage):
    rows = storage.query_cocktails(description="citrus", limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri", "Negroni"]


def test_keyword_matches_name_only(storage):
    """keyword 只比對 name — description 含 'dry' 的 Martini 不該靠 keyword 中。"""
    rows = storage.query_cocktails(keyword="mart", limit=100)
    assert [r["name"] for r in rows] == ["Martini"]


def test_max_bounds(storage):
    rows = storage.query_cocktails(max_abv=22, limit=100)
    assert sorted(r["name"] for r in rows) == ["Daiquiri"]
    rows = storage.query_cocktails(max_rating=4.0, min_rating=4.0, limit=100)
    assert [r["name"] for r in rows] == ["Daiquiri"]


@pytest.mark.parametrize("sort,expected_first", [
    ("rating", "Obscure Gin Thing"),
    ("abv", "Obscure Gin Thing"),
    ("calories", "Negroni"),
    ("date", "Obscure Gin Thing"),
    ("name", "Obscure Gin Thing"),
    ("count", "Negroni"),
])
def test_every_sort_key_descending(storage, sort, expected_first):
    rows = storage.query_cocktails(sort=sort, desc=True, limit=100)
    assert rows[0]["name"] == expected_first


def test_ascending_reverses_order(storage):
    asc = [r["name"] for r in storage.query_cocktails(sort="calories", desc=False, limit=100)]
    assert asc[0] == "Obscure Gin Thing"  # 90 卡最低


def test_nulls_sort_last_in_both_directions(storage):
    """Mystery 的 abv 是 NULL，升序降序都該墊底。"""
    for desc in (True, False):
        names = [r["name"] for r in storage.query_cocktails(sort="abv", desc=desc, limit=100)]
        assert names[-1] == "Mystery"


def test_ties_broken_by_id_for_stable_order(storage):
    """Negroni 與 Martini 同為 4.5 分，順序必須固定。"""
    first = [r["name"] for r in storage.query_cocktails(sort="rating", limit=100)]
    second = [r["name"] for r in storage.query_cocktails(sort="rating", limit=100)]
    assert first == second
    assert first.index("Negroni") < first.index("Martini")  # id 1 在 id 2 前


def test_limit_is_applied(storage):
    assert len(storage.query_cocktails(limit=2)) == 2


def test_invalid_sort_key_raises(storage):
    """惡意 sort 值必須在拼進 SQL 前就被白名單擋下。"""
    with pytest.raises(ValueError) as exc:
        storage.query_cocktails(sort="rating; DELETE FROM cocktails")
    assert "不支援的排序鍵" in str(exc.value)


def test_sort_keys_constant_is_exported():
    assert SORT_KEYS == (
        "rating", "abv", "sweet_sour", "calories", "date", "name", "count",
    )


def test_results_include_attached_ingredients(storage):
    rows = storage.query_cocktails(keyword="negroni")
    assert sorted(i["item"] for i in rows[0]["ingredients"]) == ["Campari", "Gin"]


def test_tied_ratings_rank_by_vote_count_not_id(tmp_path):
    """同分時票數多的要排前面 —— 這是舊 get_top_rated 的 rating_count DESC 語意。

    真實 DB 有 288 筆並列 5.0、2423 筆並列 4.5，若次要排序只有 c.id（≈字母序），
    整份「社群高分精選」會由字母決定，11 票的酒會壓過 1530 票的 Negroni。
    上面那組 5 筆 fixture 同分太少、票數又剛好與 id 同序，測不出這個回歸。
    """
    with DiffordsStorage(str(tmp_path / "tied.db")) as st:
        # 全部同為 5.0 分，票數刻意與 id 反序：只靠 c.id 排序就會拿到相反的結果
        for cid, name, count in [
            (1, "Alpha Rare", 3),
            (2, "Beta Rare", 12),
            (3, "Gamma Popular", 900),
            (4, "Delta Popular", 1500),
        ]:
            assert st.save_cocktail(_cocktail(cid, name, rating=5.0, count=count)) is True

        names = [r["name"] for r in st.query_cocktails(sort="rating", limit=100)]

    assert names == ["Delta Popular", "Gamma Popular", "Beta Rare", "Alpha Rare"]


def test_id_still_breaks_ties_when_vote_counts_match(tmp_path):
    """票數也並列時才輪到 c.id，確保排序完全確定、不會在多次查詢間跳動。"""
    with DiffordsStorage(str(tmp_path / "same.db")) as st:
        for cid, name in [(1, "First"), (2, "Second"), (3, "Third")]:
            assert st.save_cocktail(_cocktail(cid, name, rating=4.0, count=50)) is True

        first = [r["name"] for r in st.query_cocktails(sort="rating", limit=100)]
        second = [r["name"] for r in st.query_cocktails(sort="rating", limit=100)]

    assert first == ["First", "Second", "Third"]
    assert first == second
