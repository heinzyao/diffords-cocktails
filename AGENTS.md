# Collaboration Notes

This repository is now centered on Difford's Guide cocktail recipes.

Current scope:
- Scrape Difford's Guide cocktail recipe pages from the public cocktail sitemap.
- Store recipes and normalized ingredients in `diffords.db`.
- Query recipes through `query.py` and the LINE Bot in `bot.py`.
- Deploy one Cloud Run Job for scraping and one Cloud Run Service for the bot.

Out of scope:
- Legacy spirit-review scraping.
- Spirit reviews, flavor profiles, and Selenium/Chrome automation.
- Cross-query recommendations based on a user's owned spirits.
