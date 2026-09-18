# 部署資源收斂

盤點與執行日期：2026-09-08。起因是升級 GitHub Actions 版本時，發現
`deploy.yml` 有一筆未提交的服務改名，往下追出一組彼此不一致的部署資源。

盤點時沒有任何東西是壞的 —— 資料每週有更新、bot 正常運作。問題是同一件事
存在兩三套資源，而真正在運作的那套不是 CI 部署的那套。

## 盤點結果

真正在維護資料的是**本機 launchd**：`~/Library/LaunchAgents/com.distiller.diffords.plist`，
每週日 04:00 跑 `scripts/run_diffords.sh`，寫入 `gs://diffords-cocktails-data`。
最近一次 2026-09-06 04:00 成功（新增 104 筆、跳過 6842 筆）。

雲端則留著兩套從未收斂的舊資源：

| 資源 | 狀態 |
|---|---|
| Cloud Scheduler `distiller-weekly-scrape` / `distiller-weekly-diffords` | **PAUSED**，非執行中 |
| Cloud Run job `distiller-scraper` / `distiller-diffords` | 最後執行 2026-05-17，寫入 `distiller-data` |
| Cloud Run service `distiller-bot`（rev 00039）/ `diffords-cocktails-bot`（rev 00001） | 兩個並存，image 相同 |

> 訂正：盤點初稿寫成「Scheduler 每週準時執行、產出無人使用」，實際上兩個
> Scheduler 都是 PAUSED，自 2026-05 起就沒再觸發過。是休眠的殘留，不是在跑的殭屍。

## 決定

1. **服務名切到 `diffords-cocktails-bot`**，淘汰 `distiller-bot`。
2. **排程維持本機 launchd**，雲端排程不再使用，並寫進 `CLAUDE.md`。
3. **plist 只留在用的那份**（`com.distiller.diffords`）。

## 已完成

- [x] 刪除 Cloud Scheduler `distiller-weekly-scrape`、`distiller-weekly-diffords`
- [x] 刪除 Cloud Run job `distiller-scraper`、`distiller-diffords`
- [x] 刪除 repo 內未安裝也未使用的 `com.diffords-scraper.plist`
- [x] `CLAUDE.md` 新增 Deployment and scheduling 段落，寫明排程刻意放在本機
- [x] 提交 `deploy.yml` / `scripts/deploy_gcp.sh` 的服務改名

## 待辦

無。收斂已完成。

## 執行紀錄（2026-09-08 後續）

- [x] push 到 main（`4c51ba0..c5f06fd`），Tests 與 Deploy to Cloud Run 皆綠燈
      —— 順帶驗證升級後的 `auth@v3` / `setup-gcloud@v3` 在 WIF 流程下正常
- [x] LINE webhook 已由使用者切至新服務
- [x] `diffords-cocktails-bot` 部署出 rev 00002，`/health` 回 200
- [x] 刪除 `distiller-bot`（刪除前為 rev 00039，image tag `4c51ba0`）
- [x] 刪除 `gs://distiller-data/distiller.db`（4.98 MiB 舊烈酒評論資料庫）
- [x] 刪除 `gs://distiller-data` bucket
- [x] `deploy.yml` 加上 `paths-ignore`，純文件 commit 不再觸發部署

## 收斂後現況

| 類型 | 名稱 |
|---|---|
| Cloud Run service | `diffords-cocktails-bot` |
| Cloud Run job | `diffords-cocktails-scraper` |
| GCS bucket | `diffords-cocktails-data`（`diffords.db` 為正本） |
| 排程 | 本機 launchd `com.distiller.diffords`，每週日 04:00 |

`distiller` 命名的雲端資源已全數移除，包含 `gs://distiller-data` bucket。

## Review

三項決定全部落地，雲端只剩一個 service、一個 job、一個 bucket 在用。
排程刻意留在本機，已寫入 `CLAUDE.md` 的 Deployment and scheduling 段落，
並註明不要在未處理本機排程前加回雲端排程。
