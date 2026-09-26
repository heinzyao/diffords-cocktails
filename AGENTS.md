# Collaboration Notes

This repository is centered on Difford's Guide cocktail recipes.

Current scope:
- Scrape Difford's Guide cocktail recipe pages from the public cocktail sitemap.
- Store recipes and normalized ingredients in `diffords.db`.
- Query recipes through `query.py` and the LINE Bot in `bot.py`.
- Deploy one Cloud Run Job for scraping and one Cloud Run Service for the bot.

Out of scope:
- Scraping spirit (bottle) review pages — only cocktail recipe pages are scraped.
  A recipe's own Flavour Profile and `review` text are in scope (flavour search uses them).
- Selenium/Chrome automation.
- Cross-query recommendations based on a user's owned spirits.
