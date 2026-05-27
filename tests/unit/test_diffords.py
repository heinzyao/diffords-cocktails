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

        by_ingredient = storage.filter_by_ingredient("campari")
        assert by_ingredient[0]["name"] == "Negroni"

        stats = storage.get_stats()
        assert stats["總雞尾酒數"] == 1


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
