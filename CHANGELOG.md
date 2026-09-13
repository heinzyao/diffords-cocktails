# Changelog

## 彈性排序與搜尋

- `storage`：六個寫死的查詢方法收斂為單一 `query_cocktails()`，條件可疊加、
  排序鍵與方向可選；標籤篩選改用 SQLite `json_each` 取代全表 Python 掃描。
- `query.py`：`list` 的 flag 從互斥改為可疊加，新增 `--keyword` `--description`
  `--max-rating` `--max-abv` `--min-count` `--sort` `--asc`。
- LINE bot：`雞尾酒列表` / `雞尾酒搜尋` 支援疊加條件與 `排序`／`升序`／`降序`，
  新增 `描述`／`最高評分`／`最高酒精濃度`。舊指令字串全部沿用；但 `雞尾酒列表 評分 N`
  不再隱含 5 票門檻，低票數酒譜會一併列出（依票數排在後面）。

## 3.0.0

- Re-centered the project on Difford's Guide cocktail recipes.
- Removed legacy spirit-review scraping, spirit queries, browser automation runtime, and old spirit CSV outputs.
- Renamed the Python package to `diffords_guide`.
- Simplified CLI commands to `search`, `info`, `stats`, and `list`.
- Simplified LINE Bot commands to cocktail recipe lookup, list filters, stats, scraper trigger, and status.
- Updated Cloud Build and GitHub Actions deployment to build only `diffords-cocktails-scraper` and `diffords-cocktails-bot`.
