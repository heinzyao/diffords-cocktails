import json
from unittest.mock import MagicMock, patch

from diffords_guide.config import SITEMAP_URL
from diffords_guide.scraper import DiffordsGuideScraper, SitemapEntry
from diffords_guide.selectors import DiffordsExtractor
from diffords_guide.storage import DiffordsStorage


SAMPLE_JSON_LD = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Negroni",
    "description": "A classic Italian cocktail.",
    "recipeIngredient": ["30 ml Gin", "30 ml Red bitter liqueur", "30 ml Rosso/sweet vermouth"],
    "keywords": ["Classic/vintage", "Bittersweet"],
    "aggregateRating": {"ratingValue": "4.5", "ratingCount": "500"},
    "nutrition": {"calories": "200 calories"},
    "totalTime": "PT03M0S",
    "datePublished": "2020-01-01",
}

SAMPLE_HTML = f"""
<html><body>
<script type="application/ld+json">{json.dumps(SAMPLE_JSON_LD)}</script>
<h3 class="m-0">Glass:</h3><p>Photographed in a Old Fashioned Glass</p>
<h3 class="m-0">Garnish:</h3><p>Orange peel twist</p>
<h3 class="m-0">Prepare:</h3><p>Chill glass.</p>
<h3 class="m-0">How to make:</h3><p>STIR all ingredients with ice.</p>
<h3 class="m-0">Review:</h3><p>The iconic Italian aperitivo.</p>
<h3 class="m-0">History:</h3><p>Created in Florence.</p>
<table class="legacy-ingredients-table"><tbody>
<tr><td>30 ml</td><td>Tanqueray Gin</td></tr>
<tr><td>30 ml</td><td>Campari</td></tr>
<tr><td>30 ml</td><td>Martini Rosso</td></tr>
</tbody></table>
<ul><li>16.14% alc./vol. (32.28 proof)</li></ul>
</body></html>
"""

# 2026-08 網站改版後的結構（實測 recipe/1036 取得）：
#   h3.m-0 標籤全數消失，改為 h2/h3.cocktail-*-heading 且不帶冒號；
#   食材表改名 cocktail-ingredients__table；
#   garnish/prepare/history 不再有獨立區塊，garnish 併入 JSON-LD 的 HowToStep。
SAMPLE_JSON_LD_V2 = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Jack Frost #2",
    "description": "Discover how to make a Jack Frost #2 using Cognac.",
    "recipeIngredient": ["45 ml Cognac (brandy)", "15 ml Lime juice"],
    "recipeInstructions": [
        {"@type": "HowToStep", "name": "Prepare glass", "text": "Select and pre-chill a COUPE GLASS."},
        {"@type": "HowToStep", "name": "Prepare garnish", "text": "Prepare garnish of sugar rim."},
        {"@type": "HowToStep", "name": "SHAKE", "text": "SHAKE all ingredients with ice."},
        {"@type": "HowToStep", "name": "Garnish", "text": "Garnish with lime wedge."},
    ],
    "keywords": ["Fruity"],
    "aggregateRating": {"ratingValue": "4.0", "ratingCount": "12"},
    "datePublished": "2024-01-01",
}

SAMPLE_HTML_V2 = f"""
<html><body>
<script type="application/ld+json">{json.dumps(SAMPLE_JSON_LD_V2)}</script>
<h1 class="cocktail-title">Jack Frost #2</h1>
<h2 class="cocktail-heading">How to make</h2>
<h3 class="cocktail-sub-heading">Glassware</h3><p>Serve in a Coupe glass</p>
<h2 class="cocktail-sub-heading">Method</h2>
<ol><li>Select and pre-chill a COUPE GLASS.</li><li>SHAKE all ingredients with ice.</li></ol>
<h2 class="cocktail-heading">Review</h2>
<p>Fruits of the forest and cranberry burst forth from this cognac laced drink.</p>
<table class="cocktail-ingredients__table"><tbody>
<tr><td>45 ml</td><td>Remy Martin Cognac</td></tr>
<tr><td>15 ml</td><td>Lime juice (freshly squeezed)</td></tr>
</tbody></table>
<ul><li>12.79% alc./vol. (25.59 proof)</li></ul>
</body></html>
"""

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.diffordsguide.com/cocktails/recipe/1/abacaxi-ricaco</loc>
    <lastmod>2024-11-01</lastmod>
  </url>
  <url>
    <loc>https://www.diffordsguide.com/cocktails/recipe/1254/negroni</loc>
    <lastmod>2026-04-01</lastmod>
  </url>
</urlset>
"""


def _sample_cocktail(name="Negroni", url="https://www.diffordsguide.com/cocktails/recipe/1254/negroni"):
    data = DiffordsExtractor.extract_all(SAMPLE_HTML)
    data["name"] = name
    data["url"] = url
    data["lastmod"] = "2026-04-01"
    return data


def test_extract_recipe_fields_from_json_ld_and_html():
    data = DiffordsExtractor.extract_all(SAMPLE_HTML)

    assert data["name"] == "Negroni"
    assert data["rating_value"] == 4.5
    assert data["rating_count"] == 500
    assert data["calories"] == 200
    assert data["prep_time_minutes"] == 3
    assert data["glassware"] == "Old Fashioned Glass"
    assert data["garnish"] == "Orange peel twist"
    assert data["abv"] == 16.14
    assert data["ingredients_generic"][0]["item"] == "Gin"
    assert data["ingredients_html"][1]["item"] == "Campari"


def test_extract_handles_2026_08_site_redesign():
    """改版後的頁面（無 h3.m-0、無 legacy 食材表）仍要取得主要欄位。"""
    data = DiffordsExtractor.extract_all(SAMPLE_HTML_V2)

    assert data["name"] == "Jack Frost #2"
    assert data["glassware"] == "Coupe glass"          # 移除 "Serve in a " 前綴
    assert data["review"].startswith("Fruits of the forest")
    assert data["abv"] == 12.79
    assert data["ingredients_html"][0]["item"] == "Remy Martin Cognac"
    # instructions 改走 JSON-LD 的 HowToStep，與網頁 Method 區塊一致（含 garnish 步驟）
    assert "SHAKE all ingredients with ice." in data["instructions"]
    assert "Select and pre-chill a COUPE GLASS." in data["instructions"]
    # garnish 由 HowToStep 的 garnish 步驟合併而來（可能有多個）
    assert "sugar rim" in data["garnish"]
    assert "lime wedge" in data["garnish"]


def test_extract_still_handles_pre_redesign_html():
    """舊結構不可回歸 —— GCS 上仍有大量改版前抓到的資料。"""
    data = DiffordsExtractor.extract_all(SAMPLE_HTML)

    assert data["glassware"] == "Old Fashioned Glass"
    assert data["garnish"] == "Orange peel twist"
    assert data["review"] == "The iconic Italian aperitivo."
    assert data["history"] == "Created in Florence."


def test_html_only_fallback_extracts_name_and_ingredients():
    html = """
    <html><body>
    <h1>Fallback Sour</h1>
    <h3 class="m-0">How to make:</h3><p>SHAKE with ice.</p>
    <table class="legacy-ingredients-table"><tbody>
      <tr><td>45 ml</td><td>Whiskey</td></tr>
      <tr><td>30 ml</td><td>Lemon juice</td></tr>
    </tbody></table>
    </body></html>
    """
    data = DiffordsExtractor.extract_all(html)

    assert data["name"] == "Fallback Sour"
    assert data["instructions"] == "SHAKE with ice."
    assert data["ingredients_html"][0]["item"] == "Whiskey"


def test_storage_saves_and_queries_cocktail(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        assert storage.save_cocktail(_sample_cocktail()) is True

        found = storage.get_cocktail_by_name("negroni")
        assert found["name"] == "Negroni"
        assert len(found["ingredients"]) == 3
        assert found["tags"] == ["Classic/vintage", "Bittersweet"]

        by_ingredient = storage.query_cocktails(ingredient="campari")
        assert by_ingredient[0]["name"] == "Negroni"

        stats = storage.get_stats()
        assert stats["總雞尾酒數"] == 1


def test_storage_keeps_existing_html_fields_when_rescrape_returns_none(tmp_path):
    """改版後抓不到的欄位不可清空既有資料（否則一次 full scrape 就流失）。"""
    db = tmp_path / "t.db"
    with DiffordsStorage(str(db)) as storage:
        storage.save_cocktail({
            "name": "Negroni", "url": "https://www.diffordsguide.com/cocktails/recipe/1/negroni",
            "garnish": "Orange peel twist", "prepare": "Chill glass.",
            "glassware": "Old Fashioned Glass", "abv": 16.14,
        })
        # 改版後的重爬：HTML 欄位抓不到，但 JSON-LD 欄位有新值
        storage.save_cocktail({
            "name": "Negroni", "url": "https://www.diffordsguide.com/cocktails/recipe/1/negroni",
            "garnish": None, "prepare": None, "glassware": None, "abv": None,
            "description": "Updated description.",
        })
        row = storage.get_cocktail_by_name("Negroni")

    assert row["garnish"] == "Orange peel twist"
    assert row["prepare"] == "Chill glass."
    assert row["glassware"] == "Old Fashioned Glass"
    assert row["abv"] == 16.14
    assert row["description"] == "Updated description."  # JSON-LD 欄位仍正常更新


def test_storage_upserts_cocktail_when_diffords_slug_changes(tmp_path):
    db_path = tmp_path / "diffords.db"
    changed_url = "https://www.diffordsguide.com/cocktails/recipe/1254/negroni-cocktail"
    with DiffordsStorage(str(db_path)) as storage:
        assert storage.save_cocktail(_sample_cocktail()) is True
        assert storage.save_cocktail(_sample_cocktail("Negroni Cocktail", changed_url)) is True

        found = storage.get_cocktail_by_id(1254)
        assert found["name"] == "Negroni Cocktail"
        assert found["url"] == changed_url
        assert storage.get_stats()["總雞尾酒數"] == 1


def test_scraper_parse_sitemap():
    response = MagicMock()
    response.content = SITEMAP_XML
    response.raise_for_status.return_value = None

    scraper = DiffordsGuideScraper(storage=None, delay_min=0, delay_max=0)
    scraper.session.get = MagicMock(return_value=response)
    try:
        entries = scraper.parse_sitemap()
    finally:
        scraper.close()

    assert scraper.session.get.call_args[0][0] == SITEMAP_URL
    assert [entry.cocktail_id for entry in entries] == [1, 1254]
    assert entries[1].slug == "negroni"


def test_scraper_incremental_skip_uses_lastmod(tmp_path):
    db_path = tmp_path / "diffords.db"
    with DiffordsStorage(str(db_path)) as storage:
        storage.save_cocktail(_sample_cocktail())

        scraper = DiffordsGuideScraper(storage=storage, delay_min=0, delay_max=0)
        entry = SitemapEntry(
            cocktail_id=1254,
            slug="negroni",
            url="https://www.diffordsguide.com/cocktails/recipe/1254/negroni",
            lastmod="2026-04-01",
        )
        try:
            assert scraper._should_skip(entry, incremental=True) is True
            assert scraper._should_skip(entry, incremental=False) is False
        finally:
            scraper.close()


def test_scraper_fetches_and_stores_recipe_without_network_sleep(tmp_path):
    db_path = tmp_path / "diffords.db"
    entry = SitemapEntry(
        cocktail_id=1254,
        slug="negroni",
        url="https://www.diffordsguide.com/cocktails/recipe/1254/negroni",
        lastmod="2026-04-01",
    )
    response = MagicMock()
    response.text = SAMPLE_HTML
    response.raise_for_status.return_value = None

    with DiffordsStorage(str(db_path)) as storage:
        scraper = DiffordsGuideScraper(storage=storage, delay_min=0, delay_max=0)
        scraper.session.get = MagicMock(return_value=response)
        with patch("diffords_guide.scraper.time.sleep"):
            ok = scraper.scrape(entries=[entry], incremental=True)
        scraper.close()

        assert ok is True
        assert storage.get_cocktail_by_name("Negroni")["url"] == entry.url
