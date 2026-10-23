# 待辦

## 完成：說明拆成雞尾酒／音樂兩份（2026-10-23）

- [x] `雞尾酒說明`／`cocktail help` 所有人可用；`音樂說明`／`music help` 只在 admin 1:1 解析，其他情況 = unknown
- [x] `說明`：admin 1:1 顯示總覽；其他人與群組 = `雞尾酒說明`（原內容，不提音樂）
- [x] README 中英指令表；pytest 139 passed

## 進行中：LINE bot 音樂指令轉送（2026-10-23）

契約：`~/.hermes/cache/scratch/music_deploy_contract.md`。commit 後不 push，等 Hermes 驗證。

- [x] `parse_command(admin=)`：四個音樂指令、「N首」、指示 1–300 字
- [x] 非 admin 與 unknown 完全相同；`fmt_help(admin)` 只對 admin 顯示 🎧
- [x] `diffords_guide/music_client.py`：ID token、未設定／503／逾時／其他錯誤 → 一句繁中
- [x] loading → 45 秒內 reply，否則先回再 push；文字截斷 ≤ 5000
- [x] deploy.yml 帶 `MUSIC_SERVICE_URL`（secret 不存在 → 空字串 → 未啟用）；依賴加 `google-auth`
- [x] 測試（全 mock）、ruff（新檔無新增類型問題）
- [x] 本機簽章模擬 webhook：假音樂服務 + 攔截所有 LINE API
- [x] README／AGENTS.md
- [x] Opus 第二意見審查

### Review

驗證：pytest 136 passed；ruff 新增只有 FA102（repo 既有慣例）與刻意的 BLE001。
本機 gunicorn + 簽章 webhook + 假音樂服務（所有 LINE API 攔截、ID token mock）：
admin「音樂預覽 下雨天的爵士」→ loading + reply 歌單；非 admin 同訊息 → 與「隨便打的字」
相同的 unknown 回覆、假服務未被呼叫；慢速（等待改 2 秒）→ reply「還在挑歌」+ push；
錯誤簽章 → 400；說明只有 admin 有 🎧 區塊。

Opus 第二意見：
- 採納：admin 在群組時音樂與 🎧 說明會被群組成員看到 → 只在 `source.type == "user"` 開放。
- 採納：`_handle_event` 裡的 parse 例外會變沉默 → `_is_music` 包例外、退回 `_safe_handle`。
- 採納：loading 呼叫最多 10 秒吃掉 45 秒預算 → 先送出服務呼叫再開動畫。
- 採納：secret 尾端換行 → client 端 `strip()`。
- 駁回：gunicorn 30 秒砍 worker —— Dockerfile 是 `--threads 8`（gthread），不受影響。
- 駁回：LINE 重送造成重複建歌單 —— webhook 已丟棄 `isRedelivery` 事件。
- 駁回：`_is_admin(None)` 變管理員 —— 已有 `bool(admin) and` 擋住。
- 駁回：push 失敗無聲 —— `_post_line` 已記 warning（含狀態碼）。
- 記錄不改：音樂請求佔一條 gunicorn thread 最多約 4 分鐘，Cloud Run concurrency 80 > threads 8；
  單一管理員使用量下可接受，見 AGENTS.md。

## 已歸檔的工作紀錄

- [`2026-09-21-llm-integration.md`](2026-09-21-llm-integration.md) —— LLM 語意理解整合。
  修復 2026-08 網站改版造成的提取失效、自然語言查詢、風味語意檢索、
  Flavour Profile 口味維度，以及排程自動重建索引。
- [`2026-09-08-deploy-convergence.md`](2026-09-08-deploy-convergence.md) —— 部署資源收斂。
  雲端只留一個 service、一個 job、一個 bucket；排程刻意留在本機 launchd。

## 下次可以考慮的方向

沒有急迫性，想到才做：

- **語意檢索加相似度門檻** —— 目前無論多不相關都會回傳 top N。全量索引後
  分數落在 0.72-0.88，但沒實際量過「無關查詢」的分數分布，貿然設門檻可能
  誤殺。要做的話先蒐集一批查詢看分布。
- **`strength` 尚未開放查詢** —— 欄位有存（4,992 筆），但沒加篩選參數，
  因為「烈一點」目前用 `min_abv` 已經夠用。等真的遇到 abv 為 NULL 但需要
  篩強度的情況再加。
- **tag 長尾清理** —— 1,931 種 tag 裡有 `1920`、`2 Guns Miller` 這類雜訊
  （各 1 筆）。不影響查詢（NLP 只從常用清單挑），純粹是資料整潔問題。
- **`_KNOWN_TAGS` 會過時** —— 寫死在 `nlp.py`，Difford's 若調整分類法需要
  手動更新，該檔案裡有重新產生的 SQL。
