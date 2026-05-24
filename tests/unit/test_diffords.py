"""
Difford's Guide 爬蟲模組單元測試

測試策略：
- 使用真實的 HTML 片段（從實際頁面提取）測試解析邏輯
- DiffordsStorage 使用記憶體 DB（`:memory:`）
- DiffordsGuideScraper 使用 mock requests.Session 避免真實 HTTP 請求
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from distiller_scraper.diffords_config import (
    DEFAULT_DELAY_MAX,
    DEFAULT_DELAY_MIN,
    DIFFORDS_DB_DEFAULT,
    DIFFORDS_DB_PATH,
    DIFFORDS_NOTIFY_SOURCE,
    GCS_DIFFORDS_DB_BLOB,
    SITEMAP_URL,
    _HEADERS,
)
from distiller_scraper.diffords_selectors import DiffordsExtractor
from distiller_scraper.diffords_storage import DiffordsStorage
from distiller_scraper.diffords_scraper import (
    DiffordsGuideScraper,
    SitemapEntry,
)

# ── 測試固定資料 ──────────────────────────────────────────────────────

SAMPLE_JSON_LD = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Negroni",
    "description": "A classic Italian cocktail.",
    "recipeIngredient": [
        "30 ml Gin",
        "30 ml Red bitter liqueur",
        "30 ml Rosso/sweet vermouth",
    ],
    "recipeInstructions": [
        {
            "@type": "HowToStep",
            "name": "STIR",
            "text": "STIR all ingredients with ice.",
        },
        {"@type": "HowToStep", "name": "STRAIN", "text": "STRAIN into glass."},
    ],
    "keywords": ["Classic/vintage", "Bittersweet (e.g. Negroni)"],
    "aggregateRating": {
        "@type": "aggregateRating",
        "ratingValue": "4.5",
        "ratingCount": "500",
    },
    "nutrition": {"@type": "NutritionInformation", "calories": "200 calories"},
    "totalTime": "PT03M0S",
    "datePublished": "2020-01-01 00:00:00",
    "url": "https://www.diffordsguide.com/cocktails/recipe/1254/negroni",
}

SAMPLE_HTML = f"""
<html><body>
<script type="application/ld+json">{json.dumps(SAMPLE_JSON_LD)}</script>
<h3 class="m-0">Glass:</h3>
<p>Photographed in a Old Fashioned Glass</p>
<h3 class="m-0">Garnish:</h3>
<p>Orange peel twist</p>
<h3 class="m-0">Prepare:</h3>
<p>Chill an Old Fashioned Glass.</p>
<h3 class="m-0">How to make:</h3>
<p>STIR all ingredients with ice. STRAIN into glass.</p>
<h3 class="m-0">Review:</h3>
<p>The iconic Italian aperitivo.</p>
<h3 class="m-0">History:</h3>
<p>Created by Count Negroni in Florence, 1919.</p>
<table class="legacy-ingredients-table">
  <tbody>
    <tr><td>30 ml</td><td>Tanqueray Gin</td></tr>
    <tr><td>30 ml</td><td>Campari</td></tr>
    <tr><td>30 ml</td><td>Martini Rosso</td></tr>
  </tbody>
</table>
<ul><li>16.14% alc./vol. (32.28° proof)</li></ul>
</body></html>
"""

SAMPLE_SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.diffordsguide.com/cocktails/recipe/1/abacaxi-ricaco</loc>
    <lastmod>2024-11-01</lastmod>
  </url>
  <url>
    <loc>https://www.diffordsguide.com/cocktails/recipe/3/abbey</loc>
    <lastmod>2025-01-15</lastmod>
  </url>
  <url>
    <loc>https://www.diffordsguide.com/cocktails/recipe/1254/negroni</loc>
    <lastmod>2026-04-01</lastmod>
  </url>
</urlset>
"""


# HTML-only fixture：模擬缺少 JSON-LD 但有完整 HTML 結構的頁面
SAMPLE_HTML_NO_JSON_LD = """
<html><body>
<h1>Luchadora</h1>
<h3 class="m-0">Glass:</h3>
<p>Old Fashioned Glass</p>
<h3 class="m-0">Prepare:</h3>
<p>Chill glass and prepare garnish.</p>
<h3 class="m-0">How to make:</h3>
<p>SHAKE all ingredients with ice. STRAIN into glass.</p>
<h3 class="m-0">Garnish:</h3>
<p>Lime wheel</p>
<table class="legacy-ingredients-table">
  <tbody>
    <tr><td>45 ml</td><td>Tequila Reposado</td></tr>
    <tr><td>15 ml</td><td>Mezcal</td></tr>
    <tr><td>30 ml</td><td>Lime juice</td></tr>
    <tr><td>15 ml</td><td>Agave syrup</td></tr>
  </tbody>
</table>
<ul><li>23.5% alc./vol. (47° proof)</li></ul>
</body></html>
"""

# 最小 HTML：只有 h1，無食材表格
SAMPLE_HTML_NO_JSON_LD_MINIMAL = """
<html><body>
<h1>Mystery Cocktail</h1>
</body></html>
"""

# 完全空的 HTML：連 h1 都沒有
SAMPLE_HTML_NO_JSON_LD_EMPTY = """
<html><body>
<p>This is not a cocktail page.</p>
</body></html>
"""


# ── DiffordsExtractor 測試 ────────────────────────────────────────────


class TestDiffordsExtractor:
    def test_extract_json_ld(self):
        ld = DiffordsExtractor.extract_json_ld(SAMPLE_HTML)
        assert ld is not None
        assert ld["@type"] == "Recipe"
        assert ld["name"] == "Negroni"

    def test_extract_json_ld_missing(self):
        assert DiffordsExtractor.extract_json_ld("<html><body></body></html>") is None

    def test_extract_json_ld_non_recipe(self):
        html = '<script type="application/ld+json">{"@type": "Person"}</script>'
        assert DiffordsExtractor.extract_json_ld(html) is None

    def test_extract_all_basic_fields(self):
        data = DiffordsExtractor.extract_all(SAMPLE_HTML)
        assert data is not None
        assert data["name"] == "Negroni"
        assert data["rating_value"] == 4.5
        assert data["rating_count"] == 500
        assert data["calories"] == 200
        assert data["prep_time_minutes"] == 3

    def test_extract_all_html_fields(self):
        data = DiffordsExtractor.extract_all(SAMPLE_HTML)
        assert (
            data["glassware"] == "Old Fashioned Glass"
        )  # 移除 "Photographed in a " 前綴
        assert data["garnish"] == "Orange peel twist"
        assert data["review"] == "The iconic Italian aperitivo."
        assert data["history"] == "Created by Count Negroni in Florence, 1919."

    def test_extract_all_tags(self):
        data = DiffordsExtractor.extract_all(SAMPLE_HTML)
        assert "Classic/vintage" in data["tags"]
        assert "Bittersweet (e.g. Negroni)" in data["tags"]

    def test_extract_ingredients_json_ld(self):
        ings = DiffordsExtractor.parse_ingredients_json_ld(
            [
                "30 ml Gin",
                "30 ml Red bitter liqueur",
                "30 ml Rosso/sweet vermouth",
            ]
        )
        assert len(ings) == 3
        assert ings[0]["amount"] == "30 ml"
        assert ings[0]["item"] == "Gin"
        assert ings[1]["sort_order"] == 2

    def test_extract_ingredients_json_ld_no_unit(self):
        ings = DiffordsExtractor.parse_ingredients_json_ld(["Orange peel"])
        assert len(ings) == 1
        assert ings[0]["item"] == "Orange peel"
        assert ings[0]["amount"] == ""

    def test_extract_ingredients_html(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        ings = DiffordsExtractor.extract_ingredients_html(soup)
        assert len(ings) == 3
        assert ings[0]["amount"] == "30 ml"
        assert ings[0]["item"] == "Tanqueray Gin"
        assert ings[2]["item"] == "Martini Rosso"

    def test_extract_abv(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        abv = DiffordsExtractor.extract_abv(soup)
        assert abv == 16.14

    def test_extract_prep_time_minutes(self):
        assert (
            DiffordsExtractor.extract_prep_time_minutes({"totalTime": "PT03M0S"}) == 3
        )
        assert (
            DiffordsExtractor.extract_prep_time_minutes({"totalTime": "PT1H30M"}) == 90
        )
        assert DiffordsExtractor.extract_prep_time_minutes({}) is None

    def test_extract_calories(self):
        assert (
            DiffordsExtractor.extract_calories(
                {"nutrition": {"calories": "200 calories"}}
            )
            == 200
        )
        assert DiffordsExtractor.extract_calories({}) is None


class TestDiffordsExtractorHtmlFallback:
    """測試 HTML-only fallback 路徑（無 JSON-LD 的頁面）。"""

    def test_extract_all_html_only_returns_data(self):
        """無 JSON-LD 但有完整 HTML 時，應透過 fallback 回傳有效資料。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD)
        assert data is not None
        assert data["name"] == "Luchadora"

    def test_extract_all_html_only_ingredients(self):
        """HTML fallback 應從 legacy-ingredients-table 提取食材。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD)
        assert len(data["ingredients_html"]) == 4
        assert data["ingredients_html"][0]["amount"] == "45 ml"
        assert data["ingredients_html"][0]["item"] == "Tequila Reposado"
        assert data["ingredients_html"][3]["item"] == "Agave syrup"
        # 無 JSON-LD 時 ingredients_generic 應為空
        assert data["ingredients_generic"] == []

    def test_extract_all_html_only_html_fields(self):
        """HTML fallback 應正確提取 glassware、garnish、instructions 等。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD)
        assert data["glassware"] == "Old Fashioned Glass"
        assert data["garnish"] == "Lime wheel"
        assert data["prepare"] == "Chill glass and prepare garnish."
        assert "SHAKE" in data["instructions"]

    def test_extract_all_html_only_abv(self):
        """HTML fallback 應正確提取 ABV。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD)
        assert data["abv"] == 23.5

    def test_extract_all_html_only_null_json_ld_fields(self):
        """HTML fallback 的 JSON-LD 專有欄位應為 None。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD)
        assert data["description"] is None
        assert data["rating_value"] is None
        assert data["rating_count"] is None
        assert data["calories"] is None
        assert data["prep_time_minutes"] is None
        assert data["date_published"] is None
        assert data["tags"] == []

    def test_extract_all_html_only_minimal(self):
        """只有 h1 的最小頁面，應回傳資料（名稱有效即可）。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD_MINIMAL)
        assert data is not None
        assert data["name"] == "Mystery Cocktail"
        assert data["ingredients_html"] == []

    def test_extract_all_html_only_no_h1_returns_none(self):
        """連 h1 都沒有的頁面，應回傳 None。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML_NO_JSON_LD_EMPTY)
        assert data is None

    def test_json_ld_path_still_works(self):
        """確認原有的 JSON-LD 路徑在修改後仍正常運作。"""
        data = DiffordsExtractor.extract_all(SAMPLE_HTML)
        assert data is not None
        assert data["name"] == "Negroni"
        assert data["rating_value"] == 4.5
        assert len(data["ingredients_generic"]) == 3
        assert len(data["ingredients_html"]) == 3


# ── DiffordsStorage 測試 ─────────────────────────────────────────────


@pytest.fixture
def storage(tmp_path):
    db = DiffordsStorage(str(tmp_path / "test.db"))
    yield db
    db.close()


SAMPLE_COCKTAIL = {
    "url": "https://www.diffordsguide.com/cocktails/recipe/1254/negroni",
    "lastmod": "2026-04-01",
    "name": "Negroni",
    "description": "A classic Italian cocktail.",
    "glassware": "Old Fashioned Glass",
    "garnish": "Orange peel twist",
    "prepare": "Chill glass.",
    "instructions": "STIR all ingredients with ice.",
    "review": "The iconic Italian aperitivo.",
    "history": "Created by Count Negroni in Florence, 1919.",
    "tags": ["Classic/vintage", "Bittersweet"],
    "rating_value": 4.5,
    "rating_count": 500,
    "calories": 200,
    "prep_time_minutes": 3,
    "abv": 16.14,
    "date_published": "2020-01-01",
    "ingredients_html": [
        {"sort_order": 1, "amount": "30 ml", "item": "Tanqueray Gin"},
        {"sort_order": 2, "amount": "30 ml", "item": "Campari"},
        {"sort_order": 3, "amount": "30 ml", "item": "Martini Rosso"},
    ],
    "ingredients_generic": [
        {"sort_order": 1, "amount": "30 ml", "item": "Gin"},
        {"sort_order": 2, "amount": "30 ml", "item": "Red bitter liqueur"},
        {"sort_order": 3, "amount": "30 ml", "item": "Rosso/sweet vermouth"},
    ],
}


class TestDiffordsStorage:
    def test_save_and_retrieve(self, storage):
        assert storage.save_cocktail(SAMPLE_COCKTAIL)
        result = storage.get_cocktail_by_name("Negroni")
        assert result is not None
        assert result["name"] == "Negroni"
        assert result["rating_value"] == 4.5
        assert result["glassware"] == "Old Fashioned Glass"

    def test_ingredients_saved(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        result = storage.get_cocktail_by_name("Negroni")
        assert len(result["ingredients"]) == 3
        assert result["ingredients"][0]["item"] == "Tanqueray Gin"
        assert result["ingredients"][0]["item_generic"] == "Gin"

    def test_upsert_updates_existing(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        updated = {**SAMPLE_COCKTAIL, "rating_value": 4.8, "rating_count": 600}
        storage.save_cocktail(updated)
        result = storage.get_cocktail_by_name("Negroni")
        assert result["rating_value"] == 4.8
        assert result["rating_count"] == 600

    def test_upsert_preserves_id(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        r1 = storage.get_cocktail_by_name("Negroni")
        storage.save_cocktail(SAMPLE_COCKTAIL)
        r2 = storage.get_cocktail_by_name("Negroni")
        assert r1["id"] == r2["id"]

    def test_tags_deserialized(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        result = storage.get_cocktail_by_name("Negroni")
        tags = result["tags"]
        assert isinstance(tags, list)
        assert "Classic/vintage" in tags

    def test_get_existing_urls(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        urls = storage.get_existing_urls()
        assert SAMPLE_COCKTAIL["url"] in urls

    def test_get_url_lastmod_map(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        lm = storage.get_url_lastmod_map()
        assert lm[SAMPLE_COCKTAIL["url"]] == "2026-04-01"

    def test_search_cocktails(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.search_cocktails("Negr")
        assert len(results) == 1
        assert results[0]["name"] == "Negroni"

    def test_search_cocktails_no_match(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.search_cocktails("XYZ_NOT_EXIST")
        assert results == []

    def test_filter_by_ingredient_found(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.filter_by_ingredient("bitter")
        assert len(results) == 1
        assert results[0]["name"] == "Negroni"
        assert results[0]["ingredients"][0]["item"] == "Tanqueray Gin"

    def test_filter_by_ingredient_empty(self, storage):
        results = storage.filter_by_ingredient("Gin")
        assert results == []

    def test_filter_by_tag_found(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.filter_by_tag("Classic/vintage")
        assert len(results) == 1
        assert results[0]["name"] == "Negroni"
        assert "Classic/vintage" in results[0]["tags"]

    def test_filter_by_tag_empty(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.filter_by_tag("Smoky")
        assert results == []

    def test_filter_by_rating_range(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        higher = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2000/negroni-variant",
            "name": "Negroni Variant",
            "rating_value": 4.8,
            "rating_count": 120,
        }
        low_count = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2001/negroni-lowcount",
            "name": "Negroni Low Count",
            "rating_value": 4.7,
            "rating_count": 2,
        }
        storage.save_cocktail(higher)
        storage.save_cocktail(low_count)

        results = storage.filter_by_rating(min_rating=4.6, max_rating=5.0, min_count=5)
        names = {item["name"] for item in results}
        assert names == {"Negroni Variant"}

    def test_filter_by_abv_range(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        strong = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2002/negroni-strong",
            "name": "Negroni Strong",
            "abv": 40.0,
        }
        storage.save_cocktail(strong)

        results = storage.filter_by_abv(min_abv=10.0, max_abv=20.0)
        names = {item["name"] for item in results}
        assert names == {"Negroni"}

    def test_get_top_rated(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        results = storage.get_top_rated(limit=5)
        assert len(results) == 1

    def test_scrape_run_lifecycle(self, storage):
        run_id = storage.record_scrape_run("incremental")
        assert run_id > 0
        storage.finish_scrape_run(run_id, 10, 50, 2, "completed")
        last = storage.get_last_successful_run()
        assert last is not None

    def test_get_stats(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        stats = storage.get_stats()
        assert stats["總雞尾酒數"] == 1
        assert stats["平均評分"] == 4.5

    def test_ingredients_updated_on_upsert(self, storage):
        storage.save_cocktail(SAMPLE_COCKTAIL)
        # 更新食材
        updated = {
            **SAMPLE_COCKTAIL,
            "ingredients_html": [
                {"sort_order": 1, "amount": "45 ml", "item": "Beefeater Gin"},
            ],
            "ingredients_generic": [
                {"sort_order": 1, "amount": "45 ml", "item": "Gin"},
            ],
        }
        storage.save_cocktail(updated)
        result = storage.get_cocktail_by_name("Negroni")
        assert len(result["ingredients"]) == 1
        assert result["ingredients"][0]["item"] == "Beefeater Gin"
        assert result["ingredients"][0]["amount"] == "45 ml"

    def test_get_makeable_cocktails_full_match(self, storage):
        cocktail = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2003/white-negroni",
            "name": "White Negroni",
            "rating_value": 4.7,
            "rating_count": 80,
            "ingredients_html": [
                {"sort_order": 1, "amount": "30 ml", "item": "Gin"},
                {"sort_order": 2, "amount": "30 ml", "item": "Sweet Vermouth"},
                {"sort_order": 3, "amount": "30 ml", "item": "Lemon juice"},
            ],
            "ingredients_generic": [
                {"sort_order": 1, "amount": "30 ml", "item": "Gin"},
                {"sort_order": 2, "amount": "30 ml", "item": "Sweet Vermouth"},
                {"sort_order": 3, "amount": "30 ml", "item": "Lemon juice"},
            ],
        }
        storage.save_cocktail(cocktail)

        mapping = {
            "gin": ["London Dry Gin"],
            "sweet vermouth": ["Vermouth"],
            "lemon juice": "_non_spirit",
        }
        results = storage.get_makeable_cocktails(
            ["London Dry Gin", "Vermouth"], mapping, limit=10
        )
        assert len(results) == 1
        result = results[0]
        assert result["name"] == "White Negroni"
        assert result["match_score"] == 100
        assert result["missing_ingredients"] == []
        assert set(result["matched_ingredients"]) == {"gin", "sweet vermouth"}

    def test_get_makeable_cocktails_missing_spirits(self, storage):
        cocktail = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2004/dry-negroni",
            "name": "Dry Negroni",
            "rating_value": 4.6,
            "rating_count": 60,
            "ingredients_html": [
                {"sort_order": 1, "amount": "30 ml", "item": "Gin"},
                {"sort_order": 2, "amount": "30 ml", "item": "Sweet Vermouth"},
            ],
            "ingredients_generic": [
                {"sort_order": 1, "amount": "30 ml", "item": "Gin"},
                {"sort_order": 2, "amount": "30 ml", "item": "Sweet Vermouth"},
            ],
        }
        storage.save_cocktail(cocktail)

        mapping = {
            "gin": ["London Dry Gin"],
            "sweet vermouth": ["Vermouth"],
        }
        results = storage.get_makeable_cocktails(["Vermouth"], mapping, limit=10)
        assert results == []

    def test_get_makeable_cocktails_empty_spirit_types(self, storage):
        cocktail = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2005/gin-only",
            "name": "Gin Only",
            "rating_value": 4.4,
            "rating_count": 20,
            "ingredients_html": [
                {"sort_order": 1, "amount": "45 ml", "item": "Gin"},
            ],
            "ingredients_generic": [
                {"sort_order": 1, "amount": "45 ml", "item": "Gin"},
            ],
        }
        storage.save_cocktail(cocktail)

        mapping = {"gin": ["London Dry Gin"]}
        results = storage.get_makeable_cocktails([], mapping, limit=10)
        assert results == []

    def test_get_makeable_cocktails_non_spirit_auto_included(self, storage):
        cocktail = {
            **SAMPLE_COCKTAIL,
            "url": "https://www.diffordsguide.com/cocktails/recipe/2006/limonada",
            "name": "Limonada",
            "rating_value": 4.2,
            "rating_count": 10,
            "ingredients_html": [
                {"sort_order": 1, "amount": "90 ml", "item": "Lemon juice"},
                {"sort_order": 2, "amount": "90 ml", "item": "Soda water"},
            ],
            "ingredients_generic": [
                {"sort_order": 1, "amount": "90 ml", "item": "Lemon juice"},
                {"sort_order": 2, "amount": "90 ml", "item": "Soda water"},
            ],
        }
        storage.save_cocktail(cocktail)

        mapping = {"lemon juice": "_non_spirit", "soda water": "_non_spirit"}
        results = storage.get_makeable_cocktails([], mapping, limit=10)
        assert len(results) == 1
        assert results[0]["name"] == "Limonada"


# ── DiffordsGuideScraper 測試 ─────────────────────────────────────────


class TestDiffordsGuideScraper:
    def test_parse_sitemap(self, tmp_path):
        with patch("distiller_scraper.diffords_scraper._build_session") as mock_sess_fn:
            mock_session = MagicMock()
            mock_resp = MagicMock()
            mock_resp.content = SAMPLE_SITEMAP_XML
            mock_resp.raise_for_status = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_sess_fn.return_value = mock_session

            scraper = DiffordsGuideScraper()
            entries = scraper.parse_sitemap()

        assert len(entries) == 3
        assert entries[0].cocktail_id == 1
        assert entries[0].slug == "abacaxi-ricaco"
        assert (
            entries[2].url
            == "https://www.diffordsguide.com/cocktails/recipe/1254/negroni"
        )
        assert entries[2].lastmod == "2026-04-01"

    def test_should_skip_new_url(self, tmp_path):
        scraper = DiffordsGuideScraper()
        entry = SitemapEntry(1, "test", "https://example.com/1", "2026-01-01")
        assert not scraper._should_skip(entry, incremental=True)

    def test_should_skip_existing_same_lastmod(self):
        scraper = DiffordsGuideScraper()
        url = "https://example.com/1"
        scraper.seen_urls.add(url)
        scraper.lastmod_map[url] = "2026-01-01"
        entry = SitemapEntry(1, "test", url, "2026-01-01")
        assert scraper._should_skip(entry, incremental=True)

    def test_should_not_skip_newer_lastmod(self):
        scraper = DiffordsGuideScraper()
        url = "https://example.com/1"
        scraper.seen_urls.add(url)
        scraper.lastmod_map[url] = "2025-01-01"
        entry = SitemapEntry(1, "test", url, "2026-04-01")
        assert not scraper._should_skip(entry, incremental=True)

    def test_should_not_skip_when_not_incremental(self):
        scraper = DiffordsGuideScraper()
        url = "https://example.com/1"
        scraper.seen_urls.add(url)
        scraper.lastmod_map[url] = "2026-04-01"
        entry = SitemapEntry(1, "test", url, "2026-04-01")
        assert not scraper._should_skip(entry, incremental=False)

    def test_scrape_test_mode(self, tmp_path):
        storage = DiffordsStorage(str(tmp_path / "test.db"))

        with patch("distiller_scraper.diffords_scraper._build_session") as mock_sess_fn:
            mock_session = MagicMock()
            mock_sitemap_resp = MagicMock()
            mock_sitemap_resp.content = SAMPLE_SITEMAP_XML
            mock_sitemap_resp.raise_for_status = MagicMock()

            mock_page_resp = MagicMock()
            mock_page_resp.text = SAMPLE_HTML
            mock_page_resp.raise_for_status = MagicMock()

            mock_session.get.side_effect = [mock_sitemap_resp] + [mock_page_resp] * 10
            mock_sess_fn.return_value = mock_session

            with patch("time.sleep"):  # 跳過延遲
                scraper = DiffordsGuideScraper(storage=storage)
                ok = scraper.scrape(max_recipes=2, incremental=False)

        assert ok
        assert scraper.stats.scraped == 2
        storage.close()

    def test_fetch_recipe_failure(self):
        import requests as req

        scraper = DiffordsGuideScraper()
        with patch.object(
            scraper.session, "get", side_effect=req.RequestException("Network error")
        ):
            result = scraper._fetch_recipe("https://example.com/fail")
        assert result is None

    def test_get_statistics(self):
        scraper = DiffordsGuideScraper()
        scraper.stats.scraped = 10
        scraper.stats.skipped = 50
        scraper.stats.failed = 2
        stats = scraper.get_statistics()
        assert stats["爬取新增"] == 10
        assert stats["跳過（已是最新）"] == 50
        assert stats["失敗"] == 2


# ── DiffordsConfig 測試 ────────────────────────────────────────────


class TestDiffordsConfig:
    def test_all_constants_importable(self):
        """驗證 diffords_config 中的所有常數都能正常導入。"""
        assert DEFAULT_DELAY_MIN == 2.0
        assert DEFAULT_DELAY_MAX == 4.0
        assert SITEMAP_URL == "https://www.diffordsguide.com/sitemap/cocktail.xml"
        assert DIFFORDS_DB_DEFAULT == "diffords.db"
        assert DIFFORDS_DB_PATH == "diffords.db"
        assert DIFFORDS_NOTIFY_SOURCE == "Difford's Guide"

    def test_headers_structure(self):
        """驗證 HTTP Header 的正確結構。"""
        assert isinstance(_HEADERS, dict)
        assert "User-Agent" in _HEADERS
        assert "Accept-Language" in _HEADERS
        assert "Accept" in _HEADERS
        assert len(_HEADERS["User-Agent"]) > 0

    def test_gcs_diffords_db_blob_default(self):
        """驗證 GCS blob 名稱預設值。"""
        assert GCS_DIFFORDS_DB_BLOB == "diffords.db"

    def test_constants_types(self):
        """驗證常數的型別。"""
        assert isinstance(DEFAULT_DELAY_MIN, float)
        assert isinstance(DEFAULT_DELAY_MAX, float)
        assert isinstance(SITEMAP_URL, str)
        assert isinstance(DIFFORDS_DB_DEFAULT, str)
        assert isinstance(DIFFORDS_NOTIFY_SOURCE, str)


# ── IngredientMapping 測試 ─────────────────────────────────────────────

_MAPPING_PATH = Path(__file__).parent.parent.parent / "ingredient_mapping.json"

REQUIRED_SPIRIT_KEYS = [
    "gin",
    "bourbon",
    "rye whiskey",
    "scotch whisky",
    "vodka",
    "rum",
    "dark rum",
    "white rum",
    "tequila",
    "tequila blanco",
    "cognac",
    "sweet vermouth",
    "dry vermouth",
    "campari",
    "triple sec",
    "amaretto",
    "maraschino liqueur",
    "green chartreuse",
    "crème de violette",
    "absinthe",
    "coffee liqueur",
]

NON_SPIRIT_KEYS = [
    "lemon juice",
    "lime juice",
    "sugar syrup",
    "simple syrup",
    "egg white",
    "bitters",
    "angostura bitters",
    "soda water",
    "ginger beer",
    "grenadine",
]


class TestIngredientMapping:
    @pytest.fixture(scope="class")
    def mapping(self):
        with open(_MAPPING_PATH, encoding="utf-8") as f:
            return json.load(f)

    def test_json_loads_successfully(self, mapping):
        """JSON 能正常載入。"""
        assert isinstance(mapping, dict)

    def test_minimum_entry_count(self, mapping):
        """≥30 個 entries（不計 _comment 等非成分 key）。"""
        real_entries = {k: v for k, v in mapping.items() if not k.startswith("_")}
        assert len(real_entries) >= 30, f"Only {len(real_entries)} entries found"

    def test_not_exceed_300_entries(self, mapping):
        """不超過 300 個 entries。"""
        real_entries = {k: v for k, v in mapping.items() if not k.startswith("_")}
        assert len(real_entries) <= 300

    def test_required_spirit_keys_present(self, mapping):
        """必要的烈酒 key 均存在。"""
        missing = [k for k in REQUIRED_SPIRIT_KEYS if k not in mapping]
        assert not missing, f"Missing keys: {missing}"

    def test_spirit_values_are_lists(self, mapping):
        """烈酒 key 的 value 應為 list（非 '_non_spirit'）。"""
        for key in REQUIRED_SPIRIT_KEYS:
            val = mapping[key]
            assert isinstance(val, list), (
                f"Key '{key}' should be a list, got {type(val)}"
            )
            assert len(val) > 0, f"Key '{key}' has empty list"

    def test_non_spirit_keys_present(self, mapping):
        """非烈酒 key 均存在。"""
        missing = [k for k in NON_SPIRIT_KEYS if k not in mapping]
        assert not missing, f"Missing non-spirit keys: {missing}"

    def test_non_spirit_values_are_sentinel(self, mapping):
        """非烈酒 key 的 value 應為 '_non_spirit'。"""
        for key in NON_SPIRIT_KEYS:
            assert mapping[key] == "_non_spirit", (
                f"Key '{key}' should be '_non_spirit', got {mapping[key]!r}"
            )

    def test_gin_includes_london_dry(self, mapping):
        """gin 映射包含 London Dry Gin。"""
        assert "London Dry Gin" in mapping["gin"]

    def test_bourbon_maps_to_bourbon(self, mapping):
        """bourbon 映射包含 Bourbon。"""
        assert "Bourbon" in mapping["bourbon"]

    def test_rye_whiskey_maps_to_rye(self, mapping):
        """rye whiskey 映射包含 Rye。"""
        assert "Rye" in mapping["rye whiskey"]

    def test_scotch_whisky_includes_single_malt(self, mapping):
        """scotch whisky 映射包含 Single Malt。"""
        assert "Single Malt" in mapping["scotch whisky"]

    def test_vodka_maps_to_unflavored(self, mapping):
        """vodka 映射包含 Unflavored Vodka。"""
        assert "Unflavored Vodka" in mapping["vodka"]

    def test_rum_includes_silver_and_dark(self, mapping):
        """rum 映射包含 Silver Rum 與 Dark Rum。"""
        assert "Silver Rum" in mapping["rum"]
        assert "Dark Rum" in mapping["rum"]

    def test_tequila_blanco_maps_correctly(self, mapping):
        """tequila blanco 映射包含 Tequila Blanco。"""
        assert "Tequila Blanco" in mapping["tequila blanco"]

    def test_cognac_maps_to_cognac(self, mapping):
        """cognac 映射包含 Cognac。"""
        assert "Cognac" in mapping["cognac"]

    def test_sweet_vermouth_maps_to_vermouth(self, mapping):
        """sweet vermouth 映射包含 Vermouth。"""
        assert "Vermouth" in mapping["sweet vermouth"]

    def test_campari_maps_to_bitter_liqueur(self, mapping):
        """campari 映射包含 Bitter Liqueurs 或 Amaro。"""
        val = mapping["campari"]
        assert "Bitter Liqueurs" in val or "Amaro" in val
