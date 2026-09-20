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
uv run python query.py list --ingredient gin --rating 4.2 --sort abv --limit 15
# 條件可疊加：--keyword --description --ingredient --tag --rating --max-rating
#             --abv --max-abv --min-count --sweet-sour --max-sweet-sour
# 排序：--sort {rating,abv,sweet_sour,calories,date,name,count} [--asc]

uv run python query.py index                      # build/refresh flavour vector index
uv run python query.py similar "smoky and bitter" # semantic search (needs index)

uv run python bot.py                              # LINE bot on PORT (default 8000)
```

## Architecture

Two deployables, one shared package (`diffords_guide/`) and DB (`diffords.db`):

- **Scraper job** — `run_diffords.py` → `scraper.py`. Entry point orchestrates optional GCS sync around the run.
- **Bot service** — `bot.py` (Flask). Reads recipes for queries; triggers the scraper job.

`Dockerfile.diffords` builds the scraper (Cloud Run Job), `Dockerfile.bot` builds the bot (Cloud Run Service).

The bot runs under gunicorn with **`--workers 1`, deliberately**. `_scrape_state` /
`_scrape_lock` (the guard against launching a second scrape) and `_token_cache` are
in-process state — extra workers each get their own copy, which would silently defeat
the lock and let two scrapers write the same SQLite blob. Scale with `--threads`
(same process, so the lock still holds), never with `--workers`. Real horizontal
scaling requires moving that state to shared storage first.

### LLM features (bot only, always optional)

Two Gemini-backed paths, both **pure add-ons** — every failure mode returns to the
pre-LLM behaviour, and neither is on the path of any existing command:

- `nlp.py` — natural language → `query_cocktails()` kwargs. Wired into the
  `unknown` branch of `parse_command()`, so known commands never call the API.
  Output is constrained by `response_schema` and then filtered through a
  whitelist; the LLM never touches SQL.
- `embeddings.py` — flavour semantic search over the `review` column
  (256-dim vectors in `cocktail_embeddings`, brute-force cosine via numpy).
  Built with `query.py index`; the table lives in `diffords.db` so it syncs
  through GCS like everything else.

Both return `None` when `GEMINI_API_KEY` is unset, the call times out, or nothing
usable comes back — **keep that contract**. Deployment mounts the key on the bot
service only (the scraper does no NLP). Note Gemini rejects deadlines under 10s.

### Scrape flow (the core logic)
1. `scraper.parse_sitemap()` reads `SITEMAP_URL` → list of URLs + `lastmod`.
2. Incremental skip (`_should_skip`): compare sitemap `lastmod` against DB's per-URL `lastmod` map (`storage.get_url_lastmod_map()`). sitemap `lastmod` ≤ DB `lastmod` → skip; no `lastmod` in sitemap → skip conservatively. This is why incremental runs are cheap — don't break the `lastmod` round-trip.
3. `selectors.py` parses each page: **JSON-LD is the primary source, static HTML supplements it.**
4. `storage.py` writes SQLite tables `cocktails`, `cocktail_ingredients`, `diffords_scrape_runs`.

### GCS sync (Cloud Run only)
`diffords.db` is the source of truth and lives in GCS in prod. Sync is **gated entirely on the `GCS_BUCKET` env var** — unset locally, so `gcs_storage.py` is never called and everything uses the local file. `run_diffords.py` downloads before scraping and uploads after; `bot.py` (`_ensure_db_from_gcs`) re-downloads when the blob's updated time is newer. When touching scrape/bot startup, preserve the "no `GCS_BUCKET` → local file" path.

### Bot → scraper trigger
`bot._start_diffords()` runs the scraper **only** when both `GCS_BUCKET` and `GOOGLE_CLOUD_PROJECT` are set, via `run_v2.JobsClient().run_job()` against `DIFFORDS_JOB_NAME`; otherwise it's a no-op path. Command parsing lives in `parse_command()` / `handle_message()`; the `雞尾酒*` Chinese commands and their `fmt_*` formatters are the bot's public surface — see README for the full command table.

## Deployment and scheduling

Two Cloud Run resources, one bucket:

- Service `diffords-cocktails-bot` — the LINE bot. Deployed by `.github/workflows/deploy.yml` on push to main.
- Job `diffords-cocktails-scraper` — the scraper. Also deployed by the same workflow; triggered on demand by the bot (`DIFFORDS_JOB_NAME`).
- Bucket `diffords-cocktails-data` holds `diffords.db`, the source of truth in prod.

**Scheduled scraping runs on the local Mac, not in the cloud.** `~/Library/LaunchAgents/com.distiller.diffords.plist`
runs `scripts/run_diffords.sh` every Sunday at 04:00, which writes to `diffords-cocktails-data`.
That script passes `--build-index`, so the flavour vectors for newly scraped recipes are
rebuilt **before** the GCS upload — one upload, and the online DB never has recipes the
semantic search can't find. A failed index blocks the upload rather than shipping a
half-indexed DB.
This is deliberate — Cloud Scheduler was tried and removed (2026-09-08). Don't add cloud
scheduling back without deciding what to do about the local job first; running both would
have two writers on one SQLite blob.

Consequence: if the Mac is off on Sunday, that week's update is skipped. Catch up by
running the scraper manually, or trigger the Cloud Run job from the bot's LINE command.

## Conventions

- Config is module-level constants in `config.py` (deliberately not a class — scrape params are immutable at runtime).
- Comments and docstrings are in Traditional Chinese; match that when editing.
- The scraper delays 2–4s between requests (`DEFAULT_DELAY_MIN/MAX`) to respect rate limits — don't remove.
