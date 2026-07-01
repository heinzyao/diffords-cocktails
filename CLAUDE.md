# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Inherits the root `/Users/Henry/Project/AGENTS.md` (uv workflow, secrets, `uv sync` / `uv run pytest`, bilingual README). This file covers only what's specific to this project.

## What this is

A Python data pipeline centered on Difford's Guide cocktail recipes: a scraper, a SQLite store, a CLI query tool, and a LINE Bot. Legacy spirit-review scraping and Selenium/Chrome automation are **out of scope** — don't reintroduce them.

## Project-specific commands

```bash
uv run pytest tests/unit/test_diffords.py::test_name  # single test

uv run python run_diffords.py --mode test         # scrape 10 recipes (smoke test)
uv run python run_diffords.py --mode incremental  # default; lastmod-based update
uv run python run_diffords.py --mode full         # re-scrape everything

uv run python query.py stats
uv run python query.py list --ingredient gin --rating 4.5   # also --tag, --abv, --limit

uv run python bot.py                              # LINE bot on PORT (default 8000)
```

## Architecture

Two deployables, one shared package (`diffords_guide/`) and DB (`diffords.db`):

- **Scraper job** — `run_diffords.py` → `scraper.py`. Entry point orchestrates optional GCS sync around the run.
- **Bot service** — `bot.py` (Flask). Reads recipes for queries; triggers the scraper job.

`Dockerfile.diffords` builds the scraper (Cloud Run Job), `Dockerfile.bot` builds the bot (Cloud Run Service).

### Scrape flow (the core logic)
1. `scraper.parse_sitemap()` reads `SITEMAP_URL` → list of URLs + `lastmod`.
2. Incremental skip (`_should_skip`): compare sitemap `lastmod` against DB's per-URL `lastmod` map (`storage.get_url_lastmod_map()`). sitemap `lastmod` ≤ DB `lastmod` → skip; no `lastmod` in sitemap → skip conservatively. This is why incremental runs are cheap — don't break the `lastmod` round-trip.
3. `selectors.py` parses each page: **JSON-LD is the primary source, static HTML supplements it.**
4. `storage.py` writes SQLite tables `cocktails`, `cocktail_ingredients`, `diffords_scrape_runs`.

### GCS sync (Cloud Run only)
`diffords.db` is the source of truth and lives in GCS in prod. Sync is **gated entirely on the `GCS_BUCKET` env var** — unset locally, so `gcs_storage.py` is never called and everything uses the local file. `run_diffords.py` downloads before scraping and uploads after; `bot.py` (`_ensure_db_from_gcs`) re-downloads when the blob's updated time is newer. When touching scrape/bot startup, preserve the "no `GCS_BUCKET` → local file" path.

### Bot → scraper trigger
`bot._start_diffords()` runs the scraper **only** when both `GCS_BUCKET` and `GOOGLE_CLOUD_PROJECT` are set, via `run_v2.JobsClient().run_job()` against `DIFFORDS_JOB_NAME`; otherwise it's a no-op path. Command parsing lives in `parse_command()` / `handle_message()`; the `雞尾酒*` Chinese commands and their `fmt_*` formatters are the bot's public surface — see README for the full command table.

## Conventions

- Config is module-level constants in `config.py` (deliberately not a class — scrape params are immutable at runtime).
- Comments and docstrings are in Traditional Chinese; match that when editing.
- The scraper delays 2–4s between requests (`DEFAULT_DELAY_MIN/MAX`) to respect rate limits — don't remove.
