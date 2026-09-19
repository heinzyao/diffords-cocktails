# LLM 語意理解整合

研究日期：2026-09-18。起因是評估「這個專案能不能整合 LLM 的語意理解」。

結論先講：**機會不在語意檢索，在「自然語言 → 查詢參數」。**

（2026-09-08 的部署資源收斂紀錄已歸檔至 `tasks/2026-09-08-deploy-convergence.md`。）

## 盤點結果

| 項目 | 數字 |
|---|---|
| 酒譜 | 6,955 筆 |
| 食材（正規化） | 32,520 列 / 1,061 種 `item_generic` |
| `diffords.db` | 8.8 MB |
| 查詢介面 | `query.py` CLI + `bot.py` 中文 DSL |

### 發現一：沒有語意素材

原本預期用 `description` 做向量檢索。實際抽樣後不成立 —— 那是機器模板：

```
Negroni Cocktail | Discover how to make a Negroni Cocktail using Gin,
                   Red bitter liqueur and Rosso/sweet vermouth in just 5 easy steps
```

語意資訊等於食材欄位的重述。嵌入它做向量檢索，只會拿到「食材相似」的結果，而那用
`WHERE item_generic IN (...)` 就能做到，不需要 LLM。

真正有風味描述的欄位則是空的：

```
desc|6684  review|0  history|0  instr|4645  tags|6684  garnish|4515
```

> **訂正（2026-09-18 實作時）**：初診寫成「`Review:` 這個 label 對不上」，
> 範圍低估了。實際是**網站在 2026-08 中旬全面改版**，`h3.m-0` 在頁面上
> 一個都不剩，`legacy-ingredients-table` 也改名 —— 壞掉的是整條 HTML 提取路徑，
> 不只 review。詳見下方「階段 0 執行紀錄」。

### 發現二：痛點在輸入端

`bot.py:485-540` 手刻了一套中文 DSL 解析器（`_scan_conditions`，約 60 行，
外加三段消歧義註解）。使用者必須說：

```
雞尾酒列表 材料 gin 評分 4.2 排序 酒精濃度 降序 15筆
```

而不能說「幫我找琴酒基底、評價不錯、酒精別太烈的」。這裡才是語意理解值錢的地方，
而且條件很好：輸出是結構化 kwargs（不是自由文字），可驗證、可 fallback。

## 決定

1. **先修 `review` selector**，這是「後面能不能做語意檢索」的分水嶺，且獨立有價值。
2. **主軸做 NL → 查詢參數**，掛在 `parse_command()` 的 `unknown` 分支，舊指令零影響。
3. **語意檢索延後**，等階段 0 回填結果出來再評估，不預先建索引。

## 待辦

### 階段 0：修 selector（前置）—— 程式碼已完成，待回填

- [x] `_h3_next_text` → `_heading_next_text`：吃 h2/h3、不限 class、忽略大小寫與尾隨冒號
- [x] `glassware` 前綴移除同時支援 `Serve in a` / `Photographed in a`
- [x] `instructions` 改以 JSON-LD `recipeInstructions` 為主，HTML `Method` 為輔
- [x] `garnish` 改由 JSON-LD `HowToStep` 中 name 含 garnish 的步驟合併
- [x] 食材表同時吃 `cocktail-ingredients__table` 與 `legacy-ingredients-table`
- [x] `storage._upsert_cocktail` 對 HTML 欄位加 COALESCE（見下方風險說明）
- [x] 單元測試釘住新舊兩種結構 + COALESCE 保護行為（72 passed）
- [x] `--mode test` 冒煙：新增 9 筆，9/9 都有 review / glassware / garnish
- [x] 跑 `--mode full` 回填 —— 2026-09-19 04:03→11:04，6,955 筆全數重爬，**失敗 0**
- [x] 回填後統計 `count(review)` → **6,738 筆**，階段 2 的素材成立

> **訂正**：實作當下只抽驗 3 頁就斷定「`History` / `Prepare` 區塊已從網站移除」，
> 這是錯的。回填後 `history` 有 5,901 筆、`prepare` 有 4,992 筆。
> 真相是**新版頁面版型不只一種** —— 有的酒款用 `Glassware`、有的用 `Glass`，
> `Prepare` / `Garnish` / `History` 也只有部分酒款具備。我最初抽驗的三頁
> 剛好都是精簡版型。實作本身沒問題（`rstrip(":")` 讓無冒號標題照樣命中），
> 錯的是據此寫下的結論。抽樣結論要標明樣本數。

`--mode full` 會重爬全站，別在週日 04:00 的 launchd 排程前後跑，避免兩個寫入者
同時動 GCS 上的 `diffords.db`（同 `CLAUDE.md` 的排程說明）。

#### 階段 0 執行紀錄（2026-09-18）

改版時間點由 DB 的 `scraped_at` 分組鎖定在 **2026-08-15 至 08-22 之間**：
08-15 當週抓的 15 筆杯型/裝飾/作法都有值，08-22 之後三批共 351 筆全空。

| 提取路徑 | 改版後狀態 | 處置 |
|---|---|---|
| `h3.m-0` | 0 個命中 | 改用 `_heading_next_text`，新舊 label 並存 |
| `legacy-ingredients-table` | 不存在（改名 `cocktail-ingredients__table`） | class 清單同時吃兩種 |
| JSON-LD | 完整，且含 `recipeInstructions` | 反而升為 instructions / garnish 的主來源 |
| ABV `li` | 正常 | 不動 |
| `prepare` / `history` 區塊 | 僅部分版型具備（見上方訂正） | label 去冒號比對，抓不到時靠 COALESCE 保住舊值 |
| `Flavour Profile` 區塊 | 新增，尚未提取 | 見下方「後續機會」 |

#### 回填結果（2026-09-19）

| 欄位 | 回填前 | 回填後 | 變化 |
|---|---:|---:|---|
| review 評語 | 0 | 6,738 | **+6,738** |
| history 歷史 | 0 | 5,901 | **+5,901** |
| instructions 作法 | 4,645 | 6,953 | +2,308 |
| garnish 裝飾 | 4,515 | 5,654 | +1,139 |
| glassware 杯型 | 6,601 | 6,952 | +351 |
| prepare 準備 | 4,645 | 4,992 | +347 |
| abv | 5,882 | 5,884 | +2 |
| description 描述 | 6,684 | 6,679 | **−5** |

食材品牌名同步恢復：32,522 列 / 1,179 種品牌名 / 1,044 種通用名。

`description` 少 5 筆是預期行為 —— 它來自 JSON-LD，不受 COALESCE 保護（需要能
更新），那 5 頁現在沒有 description 就被清成 NULL。量小且來源穩定，不處理。

#### 後續機會：Flavour Profile

改版新增了 `h2[text="Flavour Profile"]` 區塊，內容是口味維度的滑桿標籤，
例如 `No alcohol | Medium Boozy | Sweet | Medium Dry/sour`。

這是**結構化資料而非散文**，所以對階段 2 的向量檢索幫助不大，但對階段 1 的
NL→查詢參數很有價值 —— 「幫我找不太甜的」目前無法對應到任何欄位，有了口味
維度就能變成一個 SQL 可篩的條件。做階段 1 時一併評估。

**差點造成資料流失**：`_upsert_cocktail` 原本無條件覆寫所有欄位。新版 `prepare`
必為 None，若照原樣跑一次 `--mode full`，會把 4,645 筆改版前抓到的 prepare
連同其他欄位清成 NULL。已改為對 HTML 來源欄位使用
`COALESCE(:欄位, 欄位)`，並加測試釘住。

**資料格式變化（非 bug，但查詢時要知道）**：新版 garnish 來自步驟文字，
所以會是完整句子（如 `Prepare garnish of lime wedge.`），而非改版前的名詞短語
（如 `Orange peel twist`）；無裝飾的酒譜會存成 `No garnish to prepare...`。
回填後 `garnish` 欄位會是兩種風格並存。

真實頁面驗證（Jack Frost #2 / Abacaxi Ricaço / Milo）三頁全部正確提取，
`review` 首次取得值，食材品牌名也一併恢復。

### 階段 1：NL → 查詢參數（主菜）

流程：

```
使用者訊息
   ↓
parse_command()      ← 舊指令命中就直接走，不呼叫 LLM
   ↓ "unknown"
llm_parse()          → {"ingredient": "gin", "min_rating": 4.0, "max_abv": 25, ...}
   ↓ 白名單驗證      ← 沿用 storage._SORT_COLUMNS / bot._NUMERIC_KEYS 的鍵集
fmt_cocktail_list(db_path, **args)   ← 既有函式，不動
```

- [ ] 新增 `diffords_guide/nlp.py`，照抄 cat-lendar `app/services/nlp.py` 的 Gemini 模式
- [ ] 輸出 schema 對齊 `storage.query_cocktails()` 的 kwargs：
      `keyword / description / ingredient / tag / min_rating / max_rating /
       min_abv / max_abv / min_count / sort / desc / limit`
- [ ] 白名單驗證層：非白名單鍵、非法 `sort` 值、超出範圍的數值一律丟棄
- [ ] `parse_command()` 的 `unknown` 分支接上，LLM 失敗時退回現有錯誤訊息
- [ ] timeout 設 3s（LINE reply token 撐得住 30s，但體感要快）
- [ ] 單元測試：mock LLM 回傳，涵蓋「合法 JSON」「非法欄位」「timeout」三條路徑

模型選型：

| 選項 | 成本 | 取捨 |
|---|---|---|
| **Gemini Flash**（採用） | 可忽略 | cat-lendar 已在用，`GEMINI_API_KEY` 已在 Secret Manager，同一個 GCP 專案，零新基礎設施 |
| Claude Haiku 4.5 | $1/$5 per MTok，約 $0.001/次查詢 | 結構化輸出可用 JSON schema 保證 schema 合法，但要新增 secret 與相依 |

以個人使用的查詢量，兩者成本都在雜訊等級。選 Gemini 純粹因為不新增一套東西。

風險：

- **延遲** —— Flash 約 0.5-1.5s，疊上 Cloud Run cold start 可能到 3-5s。靠 timeout + fallback 兜。
- **併發** —— bot 跑 `--workers 1`（`CLAUDE.md` 有說明原因），LLM call 是阻塞的。
  要擴充只能加 `--threads`，**不要動 `--workers`**，否則 `_scrape_lock` 會失效。

### 階段 2：風味語意檢索（條件性）

**啟動條件已達成** —— 階段 0 回填出 6,738 筆 `review`，內容確實是風味描述
（如 Paper Plane：「Bittersweet with underlying bourbon character and lemon zest」），
不再是 `description` 那種模板文字。素材約 600 KB。

但仍建議**排在階段 1 之後**：階段 1 的 NL→查詢參數能解決大部分實際查詢，
而階段 2 會讓 DB 膨脹三倍（見下），先確認前者不夠用再做。

- [ ] embedding 存 SQLite BLOB，查詢時 numpy 暴力算 cosine
      —— 6,955 × 768 維全表掃描 <50ms，不需要向量資料庫 / FAISS / pgvector
- [ ] 先量 DB 膨脹的實際代價：6,955 × 768 × 4 bytes ≈ 21 MB，
      `diffords.db` 會從 8.8 MB 漲到約 30 MB

膨脹是階段 2 的主要代價，不是運算成本 —— `bot._ensure_db_from_gcs()` 每次 cold start
都要從 GCS 下載整顆 DB。決定做之前先確認這個延遲可接受。

## 明確不做

- **RAG 對話機器人 / 自由問答** —— 使用者要的是查酒譜，不是聊天。
- **LLM 生成酒譜** —— 這是爬蟲專案，資料來源是 Difford's，生成內容會污染資料可信度。
- **tag 清洗** —— 目前 1,931 種 tag，長尾是 `1920`、`2 Guns Miller`、`50/50 Martini`
  這類雜訊（各 1 筆）。確實是問題，但跟 LLM 整合無關，別綁在一起做。

## Review

（待階段 0 完成後填寫）
