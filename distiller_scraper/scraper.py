#!/usr/bin/env python3
"""
Distiller.com 爬蟲 V2：烈酒評分資料爬取器。

架構設計
--------
本爬蟲採用「API 優先 + Selenium fallback」的雙模式架構：

┌─────────────────────────────────────────┐
│  DistillerScraperV2                     │
│                                         │
│  discover_api()  ← XHR 捕獲 + 候選探測  │
│       ↓                                 │
│  scrape_category_paginated()            │
│       ├─ _fetch_spirit_urls()           │
│       │    ├─ [API mode]  api_client    │  ← 快速、穩定
│       │    └─ [Selenium]  WebDriver    │  ← 較慢，作為 fallback
│       └─ scrape_spirit_detail()        │
│            ├─ [API mode]  api_client   │
│            └─ [Selenium]  BeautifulSoup│
└─────────────────────────────────────────┘

爬取策略
--------
1. 分頁模式（預設）：對每個類別的 search URL 翻頁爬取
   優點：可取得遠超頁面顯示上限的資料，且效率高
   停止條件：連續 N 頁重複 / 重複率過高 / 達到頁數上限

2. 滾動模式（fallback）：讓 Selenium 滾動頁面觸發 lazy-loading
   適用於：分頁機制無效時（第 2 頁與第 1 頁內容相同）

去重機制
--------
seen_urls：在記憶體中維護已爬取的 URL 集合，初始化時從 SQLite 載入
優點：爬取過程中的即時去重，避免重複請求相同頁面
設計選擇：Set[str] 而非 DB 查詢，因記憶體查詢 O(1) 遠快於磁碟 IO

Session 恢復機制
----------------
Selenium WebDriver 偶爾因 Chrome 崩潰或記憶體不足出現 session 斷開，
scrape_spirit_detail() 偵測到 "invalid session id" / "session deleted" 錯誤時
自動呼叫 restart_driver() 重新啟動瀏覽器並重試（最多 MAX_RETRIES 次）
"""

import json
import logging
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import pandas as pd
from selenium.common.exceptions import JavascriptException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

from .api_client import DistillerAPIClient
from .config import ScraperConfig
from .selectors import DataExtractor, Selectors
from .storage import StorageBackend

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("distiller_scraper_v2.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class DistillerScraperV2:
    """改進版 Distiller.com 爬蟲。

    初始化參數設計：
    - headless：生產環境設 True（無頭），除錯時設 False 可觀察瀏覽器行為
    - delay_min/delay_max：隨機延遲範圍，過短容易被封鎖，過長則效率低落
    - storage：注入儲存後端（SQLite 或 CSV），None 表示僅在記憶體暫存
    - api_client：注入 HTTP API 客戶端，None 則完全使用 Selenium 模式

    依賴注入（Dependency Injection）的設計理由：
    - storage 和 api_client 以外部注入而非在內部建立，方便測試時 mock
    - 允許不同場景使用不同儲存後端（測試用 CSV，生產用 SQLite）
    """

    def __init__(
        self,
        headless: bool = True,
        delay_min: float = 2,
        delay_max: float = 4,
        storage: Optional[StorageBackend] = None,
        api_client: Optional[DistillerAPIClient] = None,
    ):
        self.headless = headless
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.storage = storage
        self.api_client = api_client
        self.driver: Optional[Any] = None  # webdriver.Chrome，延遲導入以加速初始化
        self.spirits_data: List[Dict] = []  # 本次執行爬取的所有結果（記憶體暫存）
        self.failed_urls: List[str] = []  # 爬取失敗的 URL 列表（用於事後重試或除錯）
        self.page_errors: int = 0  # 頁面載入失敗計數（timeout 等非 URL 問題）
        self.restart_count: int = 0
        self.driver_failed: bool = False
        # 去重集合：有 storage 時從 DB 載入已知 URLs，避免重複爬取
        # 設計理由：記憶體 Set 查詢 O(1)，遠比每次爬取前都 DB 查詢更有效率
        self.seen_urls: Set[str] = storage.get_existing_urls() if storage else set()

    def start_driver(self) -> bool:
        """啟動 Chrome WebDriver（使用 undetected-chromedriver 繞過 Cloudflare 偵測）。

        改用 undetected_chromedriver (uc) 的原因：
        - Cloudflare 封鎖標準 Selenium headless Chrome 的 navigator.webdriver 指紋
        - uc 自動 patch chromedriver binary，移除 webdriver 特徵碼
        - uc 不需要手動設定 excludeSwitches / useAutomationExtension（已內建）

        注意：Docker/Cloud Run 環境仍使用原始 Selenium（Dockerfile 不含 uc）
        """
        try:
            import subprocess
            import undetected_chromedriver as uc

            logger.info("正在啟動 Chrome WebDriver (undetected-chromedriver)...")
            options = uc.ChromeOptions()

            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument(f"--window-size={ScraperConfig.WINDOW_SIZE}")
            # Performance Logging for XHR API discovery
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            options.page_load_strategy = "none"

            # 偵測已安裝的 Chrome 主版號，避免 uc 自動下載不相符的 ChromeDriver
            chrome_main_version = None
            try:
                raw = subprocess.check_output(
                    ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"],
                    stderr=subprocess.DEVNULL,
                ).decode()
                chrome_main_version = int(raw.strip().split()[-1].split(".")[0])
                logger.info(f"偵測到 Chrome 版本：{chrome_main_version}")
            except Exception:
                pass

            self.driver = uc.Chrome(
                options=options,
                headless=self.headless,
                use_subprocess=True,
                version_main=chrome_main_version,
            )
            self.driver.set_page_load_timeout(ScraperConfig.PAGE_LOAD_TIMEOUT)

            logger.info("✓ Chrome WebDriver 已啟動")
            return True

        except Exception as e:
            logger.error(f"無法啟動 Chrome WebDriver: {e}")
            return False

    def close_driver(self):
        """關閉瀏覽器"""
        if self.driver:
            self.driver.quit()
            logger.info("瀏覽器已關閉")

    def random_delay(self, min_sec: float = None, max_sec: float = None):
        """隨機延遲"""
        min_sec = min_sec or self.delay_min
        max_sec = max_sec or self.delay_max
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)

    def _wait_for_body(self, timeout: int = None) -> bool:
        """等待 document.body 存在（page_load_strategy='none' 時 body 可能短暫為 null）。"""
        timeout = timeout or ScraperConfig.HEALTH_CHECK_TIMEOUT
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            return True
        except TimeoutException:
            logger.warning(f"等待 document.body 逾時（{timeout}s）")
            return False

    def scroll_page(self, max_scrolls: int = None):
        """滾動頁面載入更多內容"""
        max_scrolls = max_scrolls or ScraperConfig.MAX_SCROLL_ATTEMPTS

        if not self._wait_for_body():
            self.page_errors += 1
            return

        for attempt in range(ScraperConfig.MAX_SCROLL_RETRIES):
            try:
                last_height = self.driver.execute_script(
                    "return document.body.scrollHeight"
                )
                break
            except JavascriptException as exc:
                if attempt == ScraperConfig.MAX_SCROLL_RETRIES - 1:
                    logger.error(f"scrollPage 取得 scrollHeight 失敗，已放棄: {exc}")
                    self.page_errors += 1
                    return
                logger.warning(f"scrollPage 取得 scrollHeight 失敗，準備重試: {exc}")
                time.sleep(1)

        for i in range(max_scrolls):
            self.driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);"
            )
            time.sleep(ScraperConfig.SCROLL_DELAY)

            for attempt in range(ScraperConfig.MAX_SCROLL_RETRIES):
                try:
                    new_height = self.driver.execute_script(
                        "return document.body.scrollHeight"
                    )
                    break
                except JavascriptException as exc:
                    if attempt == ScraperConfig.MAX_SCROLL_RETRIES - 1:
                        logger.error(
                            f"scrollPage 取得 scrollHeight 失敗，已放棄: {exc}"
                        )
                        self.page_errors += 1
                        return
                    logger.warning(
                        f"scrollPage 取得 scrollHeight 失敗，準備重試: {exc}"
                    )
                    time.sleep(1)
            if new_height == last_height:
                logger.debug(f"滾動完成，共 {i + 1} 次")
                break
            last_height = new_height

    def extract_spirit_urls_from_list(self, soup: BeautifulSoup) -> List[str]:
        """從搜索結果頁面提取烈酒 URL"""
        urls = []
        items = soup.select(Selectors.SPIRIT_LIST_ITEM)

        for item in items:
            link = item.select_one("a")
            if link and link.get("href"):
                href = link["href"]
                if "/spirits/" in href:
                    full_url = (
                        f"https://distiller.com{href}" if href.startswith("/") else href
                    )
                    urls.append(full_url)

        return urls

    def restart_driver(self) -> bool:
        """重新啟動 Chrome WebDriver (用於 session 恢復)"""
        try:
            logger.info("正在重新啟動 Chrome WebDriver...")
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            time.sleep(2)
            return self.start_driver()
        except Exception as e:
            logger.error(f"重新啟動 WebDriver 失敗: {e}")
            return False

    def _should_restart(self, error_msg: str) -> bool:
        return any(
            trigger in error_msg for trigger in ScraperConfig.RESTART_TRIGGER_ERRORS
        )

    def _health_check(self) -> bool:
        """頁面健康檢查：在爬取前確認瀏覽器與目標網站可正常存取。"""
        url = (
            ScraperConfig.BASE_URL
            if hasattr(ScraperConfig, "BASE_URL")
            else "https://distiller.com"
        )
        try:
            self.driver.set_page_load_timeout(ScraperConfig.HEALTH_CHECK_TIMEOUT)
            self.driver.get(url)
            time.sleep(ScraperConfig.INITIAL_PAGE_DELAY)
            # 等待 <body> 元素存在，防止 "document.body is null" 錯誤
            # 原因：頁面可能因 React 非同步水合尚未完成，導致 DOM 未就緒
            WebDriverWait(self.driver, ScraperConfig.HEALTH_CHECK_TIMEOUT).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            self.driver.execute_script("return document.body.scrollHeight")
            # Cloudflare IP 封鎖偵測：封鎖頁面 body 能載入，但 title 為 "Attention Required"
            # 這種情況下選擇器會找不到任何資料，需明確報錯而非靜默回傳 0 筆
            title = self.driver.title
            if "Cloudflare" in title or "Attention Required" in title:
                logger.error(
                    f"健康檢查失敗（Cloudflare IP 封鎖）: 此 IP 已被 distiller.com 封鎖。"
                    f"請更換 IP（VPN/代理）或等待封鎖解除後再試。"
                )
                return False
            return True
        except TimeoutException as e:
            logger.error(f"健康檢查失敗（逾時）: {e}")
            return False
        except JavascriptException as e:
            logger.error(f"健康檢查失敗（JavaScript 錯誤）: {e}")
            return False
        except Exception as e:
            logger.error(f"健康檢查失敗: {e}")
            return False

    def capture_xhr_requests(self, url: str, scroll_count: int = 3) -> List[str]:
        """
        瀏覽指定頁面並捕獲滾動觸發的 XHR/Fetch 請求 URL。
        用於探測分頁 API 端點。回傳所有捕獲的請求 URL 列表。
        """
        import json as _json

        try:
            self.driver.get(url)
            time.sleep(ScraperConfig.INITIAL_PAGE_DELAY)

            # 等待 <body> 就緒，防止 scrollHeight null 錯誤
            if not self._wait_for_body():
                logger.warning("capture_xhr_requests: body 未就緒，跳過 XHR 擷取")
                return []

            # 清空現有 performance log
            self.driver.get_log("performance")

            # 滾動觸發可能的 XHR 請求
            for _ in range(scroll_count):
                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
                time.sleep(ScraperConfig.SCROLL_DELAY)

            # 讀取 performance log
            logs = self.driver.get_log("performance")
            xhr_urls = []

            for entry in logs:
                try:
                    msg = _json.loads(entry["message"])["message"]
                    if msg.get("method") != "Network.requestWillBeSent":
                        continue
                    req_url = msg.get("params", {}).get("request", {}).get("url", "")
                    resource_type = msg.get("params", {}).get("type", "")
                    if resource_type in ("XHR", "Fetch") and req_url:
                        xhr_urls.append(req_url)
                except (KeyError, ValueError):
                    continue

            return xhr_urls

        except Exception as e:
            logger.warning(f"捕獲 XHR 請求失敗: {e}")
            return []

    def _get_search_queries(self, category: str, use_styles: bool) -> List[tuple]:
        """回傳此類別要查詢的 (base_url, label) 列表"""
        style_map = {
            "whiskey": ScraperConfig.WHISKEY_STYLES,
            "gin": ScraperConfig.GIN_STYLES,
            "rum": ScraperConfig.RUM_STYLES,
            "vodka": ScraperConfig.VODKA_STYLES,
            "liqueurs-bitters": ScraperConfig.LIQUEURS_STYLES,
        }
        queries = []
        if use_styles and category in style_map:
            for style_id, style_name in style_map[category]:
                url = (
                    f"https://distiller.com/search"
                    f"?spirit_style_id={style_id}&sort=distiller_score"
                )
                queries.append((url, style_name))
        else:
            url = (
                f"https://distiller.com/search?category={category}&sort=distiller_score"
            )
            queries.append((url, category))
        return queries

    def _fetch_spirit_urls_from_page(self, page_url: str) -> List[str]:
        """
        載入搜索結果頁面，完整滾動後回傳所有 spirit URL。
        供分頁與滾動模式共用。
        """
        self.driver.get(page_url)
        time.sleep(ScraperConfig.INITIAL_PAGE_DELAY)

        if not self._wait_for_body():
            self.page_errors += 1
            return []

        self.scroll_page()
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        return self.extract_spirit_urls_from_list(soup)

    def _fetch_spirit_urls(self, base_url: str, page: int) -> List[str]:
        """
        取得指定查詢 + 頁碼的 spirit URL 列表。
        優先使用 API（快速），失敗時 fallback 至 Selenium。
        """
        from urllib.parse import parse_qs, urlparse

        if self.api_client and self.api_client.is_available():
            parsed = urlparse(base_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            category = params.get("category", [""])[0]
            style_id = params.get("spirit_style_id", [None])[0]

            urls = self.api_client.fetch_search_results(
                category=category, page=page, spirit_style_id=style_id
            )
            if urls is not None:  # [] 代表本頁無結果（合法），None 代表請求異常
                mode = "API"
                logger.info(f"  [{mode}] 第 {page} 頁: {len(urls)} 個連結")
                return urls
            logger.debug("  API 請求失敗，fallback 至 Selenium")

        # Selenium fallback
        page_url = base_url if page == 1 else f"{base_url}&page={page}"
        return self._fetch_spirit_urls_from_page(page_url)

    def discover_api(self, warm_up_category: str = "whiskey") -> bool:
        """
        探測 Distiller.com API 端點。
        載入一個搜索頁面並捕獲 XHR 請求，交給 api_client 分析。
        回傳 API 是否可用。
        若 api_client 未設定，直接回傳 False。
        """
        if not self.api_client:
            return False

        logger.info("=" * 60)
        logger.info("正在探測 API 端點...")
        warm_url = (
            f"https://distiller.com/search"
            f"?category={warm_up_category}&sort=distiller_score"
        )
        xhr_urls = self.capture_xhr_requests(warm_url, scroll_count=2)
        logger.info(f"捕獲 {len(xhr_urls)} 個 XHR 請求")

        result = self.api_client.discover(xhr_urls)

        if result["available"]:
            logger.info(f"✓ API 可用！搜索端點: {result['search_endpoint']}")
            if result.get("detail_endpoint_template"):
                logger.info(f"✓ 詳情端點: {result['detail_endpoint_template']}")
        else:
            logger.info("API 不可用，將使用 Selenium 模式")
        logger.info("=" * 60)

        return result["available"]

    def _scrape_urls(
        self,
        spirit_urls: List[str],
        category: str,
        results: List[Dict],
        max_spirits: int,
    ) -> None:
        """逐一爬取 spirit URL 列表，結果 append 至 results（就地修改）"""
        for spirit_url in spirit_urls:
            if len(results) >= max_spirits:
                break
            if spirit_url in self.seen_urls:
                continue
            logger.info(f"[{len(results) + 1}/{max_spirits}] 正在爬取詳情...")
            spirit_data = self.scrape_spirit_detail(spirit_url)
            if spirit_data:
                spirit_data["category"] = category
                results.append(spirit_data)
            self.random_delay()

    def scrape_category_paginated(
        self,
        category: str,
        max_spirits: int = None,
        use_styles: bool = False,
    ) -> List[Dict]:
        """
        分頁模式爬取特定類別。
        對每個查詢（category / style）逐頁翻頁，直到：
          - 已達 max_spirits 上限
          - 連續頁面無新 URL（重複比例 >= DUPLICATE_RATIO_THRESHOLD）
          - 頁面完全無結果
          - 達到 MAX_PAGES_PER_QUERY 上限
        若第一頁即判斷分頁無效，自動 fallback 至滾動模式。
        """
        max_spirits = max_spirits or ScraperConfig.MAX_SPIRITS_PER_CATEGORY
        results: List[Dict] = []
        queries = self._get_search_queries(category, use_styles)

        for base_url, label in queries:
            if len(results) >= max_spirits:
                break

            logger.info(f"[分頁] 開始爬取: {label}")
            pagination_works = False  # 確認分頁是否真的有效
            prev_page_urls: set = set()
            consecutive_dup_pages = 0

            for page in range(1, ScraperConfig.MAX_PAGES_PER_QUERY + 1):
                if len(results) >= max_spirits:
                    break

                logger.info(
                    f"  第 {page} 頁 ({'API' if self.api_client and self.api_client.is_available() else 'Selenium'})"
                )

                urls_on_page: List[str] = []
                page_loaded = False
                for attempt in range(ScraperConfig.PAGE_RETRY_COUNT):
                    if self.driver_failed:
                        break
                    try:
                        urls_on_page = self._fetch_spirit_urls(base_url, page)
                        page_loaded = True
                        break
                    except Exception as e:
                        if attempt < ScraperConfig.PAGE_RETRY_COUNT - 1:
                            logger.warning(
                                f"  第 {page} 頁嘗試 {attempt + 1} 失敗，重試..."
                            )
                            continue
                        if (
                            self._should_restart(str(e))
                            and self.restart_count < ScraperConfig.MAX_RESTART_ATTEMPTS
                        ):
                            self.restart_driver()
                            if self.driver_failed:
                                break
                            try:
                                urls_on_page = self._fetch_spirit_urls(base_url, page)
                                page_loaded = True
                                break
                            except Exception as retry_error:
                                logger.error(f"  載入第 {page} 頁失敗: {retry_error}")
                                self.page_errors += 1
                                break
                        logger.error(f"  載入第 {page} 頁失敗: {e}")
                        self.page_errors += 1
                if not page_loaded:
                    break

                if not urls_on_page:
                    logger.info(f"  第 {page} 頁無結果，停止分頁")
                    break

                # 計算新 URL 數量（未見過的）
                new_urls = [u for u in urls_on_page if u not in self.seen_urls]
                total_on_page = len(urls_on_page)
                duplicate_ratio = 1.0 - (len(new_urls) / total_on_page)
                current_page_set = set(urls_on_page)

                logger.info(
                    f"  第 {page} 頁找到 {total_on_page} 個連結，"
                    f"新增 {len(new_urls)} 個（重複率 {duplicate_ratio:.0%}）"
                )

                # ── 第一頁早停：重複率過高代表本類別資料已是最新，無需繼續分頁 ──
                if page == 1 and duplicate_ratio >= ScraperConfig.SKIP_CATEGORY_THRESHOLD:
                    logger.info(
                        f"  首頁重複率 {duplicate_ratio:.0%} >= {ScraperConfig.SKIP_CATEGORY_THRESHOLD:.0%}，"
                        f"跳過 {label}（資料已是最新）"
                    )
                    break

                # ── 分頁有效性判斷（僅在第 2 頁） ──
                if page == 2:
                    if current_page_set != prev_page_urls:
                        pagination_works = True
                        logger.info("  分頁機制有效，繼續翻頁")
                    else:
                        # 分頁無效：第 2 頁與第 1 頁完全相同
                        if all(u in self.seen_urls for u in urls_on_page):
                            logger.info("  此類別資料已存在於資料庫，跳過")
                        else:
                            logger.info("  分頁無效（第二頁無新內容），切換至滾動模式")
                            try:
                                first_page_urls = self._fetch_spirit_urls_from_page(
                                    base_url
                                )
                                self._scrape_urls(
                                    first_page_urls, category, results, max_spirits
                                )
                            except Exception as e:
                                logger.warning(f"  滾動模式 fallback 失敗: {e}")
                                self.page_errors += 1
                        break

                prev_page_urls = current_page_set

                # ── 有新 URL → 爬取並重置計數器 ──
                if new_urls:
                    consecutive_dup_pages = 0
                    self._scrape_urls(urls_on_page, category, results, max_spirits)
                elif pagination_works:
                    # ── 無新 URL 但分頁有效 → 累計連續重複頁 ──
                    consecutive_dup_pages += 1
                    logger.info(
                        f"  連續 {consecutive_dup_pages}/{ScraperConfig.MAX_CONSECUTIVE_DUP_PAGES} 頁無新 URL"
                    )
                    if consecutive_dup_pages >= ScraperConfig.MAX_CONSECUTIVE_DUP_PAGES:
                        logger.info("  此風格已完整收錄，停止分頁")
                        break
                else:
                    # page == 1 且無新 URL（首頁全部已知），繼續翻到第 2 頁判斷分頁有效性
                    self._scrape_urls(urls_on_page, category, results, max_spirits)

                # 重複率過高也停止（僅在有部分新 URL 時判斷）
                if (
                    page >= 2
                    and new_urls
                    and duplicate_ratio >= ScraperConfig.DUPLICATE_RATIO_THRESHOLD
                ):
                    logger.info(f"  重複率 {duplicate_ratio:.0%} 過高，停止分頁")
                    break

                # 每頁間延遲
                if len(results) < max_spirits:
                    time.sleep(ScraperConfig.SCROLL_DELAY)

            # 查詢間延遲（多風格時）
            if len(queries) > 1 and len(results) < max_spirits:
                time.sleep(ScraperConfig.CATEGORY_DELAY)

        return results

    def scrape_spirit_detail(self, url: str, retry_count: int = 0) -> Optional[Dict]:
        """爬取單個烈酒詳情頁（優先使用 API，失敗則 fallback Selenium）"""
        if url in self.seen_urls:
            logger.debug(f"跳過重複 URL: {url}")
            return None

        # ── API 模式 ────────────────────────────────────────────────
        if self.api_client and self.api_client.is_available():
            api_data = self.api_client.fetch_spirit_detail(url)
            if api_data:
                api_data["url"] = url
                self.seen_urls.add(url)
                if self.storage:
                    self.storage.save_spirit(api_data)
                logger.info(f"✓ [API] 已爬取: {api_data['name']}")
                return api_data
            logger.debug(f"  API 詳情失敗，fallback Selenium: {url}")

        # ── Selenium fallback ────────────────────────────────────────
        max_retries = 3

        try:
            self.driver.get(url)
            time.sleep(ScraperConfig.INITIAL_PAGE_DELAY)  # 等待 React 渲染完成

            soup = BeautifulSoup(self.driver.page_source, "html.parser")

            # 使用 DataExtractor 提取完整資料
            data = DataExtractor.extract_spirit_details(soup)

            # 驗證必要欄位
            if data["name"] == "N/A" or not data["name"]:
                logger.warning(f"無法提取品名: {url}")
                self.failed_urls.append(url)
                return None

            # 添加 URL
            data["url"] = url

            # 標記為已處理
            self.seen_urls.add(url)

            # 即時寫入 storage
            if self.storage:
                self.storage.save_spirit(data)

            logger.info(f"✓ 已爬取: {data['name']}")
            return data

        except Exception as e:
            error_msg = str(e)
            # 檢查是否為 session 斷開錯誤
            if self._should_restart(error_msg):
                if (
                    self.restart_count < ScraperConfig.MAX_RESTART_ATTEMPTS
                    and retry_count < max_retries
                ):
                    logger.warning(
                        f"Session 斷開，嘗試重新連接 (第 {retry_count + 1} 次)..."
                    )
                    try:
                        if self.restart_driver():
                            self.restart_count += 1
                            time.sleep(2)
                            return self.scrape_spirit_detail(url, retry_count + 1)
                    except Exception as restart_exc:
                        logger.error(f"重啟 driver 失敗: {restart_exc}")
                        self.driver_failed = True
                else:
                    self.driver_failed = True
                logger.error(f"重試 {max_retries} 次後仍失敗: {url}")
            else:
                logger.error(f"爬取錯誤 {url}: {e}")
            self.failed_urls.append(url)
            return None

    def scrape_category(
        self,
        category: str,
        max_spirits: int = None,
        use_styles: bool = False,
        use_pagination: bool = None,
    ) -> List[Dict]:
        """
        爬取特定類別的烈酒。
        use_pagination=True（預設）：分頁模式，可爬取遠超滾動上限的資料。
        use_pagination=False：傳統滾動模式（向後相容）。
        """
        max_spirits = max_spirits or ScraperConfig.MAX_SPIRITS_PER_CATEGORY
        if use_pagination is None:
            use_pagination = ScraperConfig.PAGINATION_ENABLED

        if use_pagination:
            return self.scrape_category_paginated(
                category, max_spirits=max_spirits, use_styles=use_styles
            )

        # ── 傳統滾動模式（fallback） ──────────────────────────────────────
        results: List[Dict] = []
        queries = self._get_search_queries(category, use_styles)

        for search_url, label in queries:
            if len(results) >= max_spirits:
                break

            logger.info(f"[滾動] 正在爬取: {label} ({search_url})")
            try:
                urls = self._fetch_spirit_urls_from_page(search_url)
                logger.info(f"在 {label} 中找到 {len(urls)} 個烈酒連結")
                self._scrape_urls(urls, category, results, max_spirits)
            except Exception as e:
                error_msg = str(e)
                if self._should_restart(error_msg):
                    if self.restart_count < ScraperConfig.MAX_RESTART_ATTEMPTS:
                        logger.warning("Session 斷開，嘗試重新連接...")
                        try:
                            if self.restart_driver():
                                self.restart_count += 1
                                time.sleep(2)
                                continue
                        except Exception as restart_exc:
                            logger.error(f"重啟 driver 失敗: {restart_exc}")
                            self.driver_failed = True
                    else:
                        self.driver_failed = True

            if len(queries) > 1:
                time.sleep(ScraperConfig.CATEGORY_DELAY)

        return results

    def scrape(
        self,
        categories: List[str] = None,
        max_per_category: int = None,
        use_styles: bool = True,
        use_pagination: bool = None,
    ):
        """執行爬蟲"""
        categories = categories or ScraperConfig.CATEGORIES
        max_per_category = max_per_category or ScraperConfig.MAX_SPIRITS_PER_CATEGORY
        if use_pagination is None:
            use_pagination = ScraperConfig.PAGINATION_ENABLED

        start_time = datetime.now()
        logger.info(f"\n{'=' * 80}")
        logger.info(f"開始爬取 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"類別: {categories}")
        logger.info(f"每類別上限: {max_per_category}")
        logger.info(f"使用風格篩選: {use_styles}")
        logger.info(f"分頁模式: {use_pagination}")
        logger.info(f"{'=' * 80}\n")

        if not self.start_driver():
            return False

        if not self._health_check():
            logger.error("Health check failed — aborting scrape")
            return False

        # API 端點探測（若 api_client 已設定）
        if self.api_client:
            self.discover_api(warm_up_category=categories[0])

        try:
            for cat_idx, category in enumerate(categories, 1):
                try:
                    logger.info(f"\n{'=' * 60}")
                    logger.info(f"類別 {cat_idx}/{len(categories)}: {category}")
                    logger.info(f"{'=' * 60}\n")

                    category_results = self.scrape_category(
                        category,
                        max_spirits=max_per_category,
                        use_styles=use_styles,
                        use_pagination=use_pagination,
                    )
                    self.spirits_data.extend(category_results)

                    logger.info(f"類別 {category} 完成: {len(category_results)} 筆")

                    if cat_idx < len(categories):
                        logger.info(
                            f"等待 {ScraperConfig.CATEGORY_DELAY} 秒後繼續...\n"
                        )
                        time.sleep(ScraperConfig.CATEGORY_DELAY)
                except Exception as e:
                    logger.error(f"類別 {category} 爬取失敗: {e}")
                    continue

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info(f"\n{'=' * 80}")
            logger.info(f"爬取完成！")
            logger.info(f"總筆數: {len(self.spirits_data)}")
            logger.info(f"失敗 URL 數: {len(self.failed_urls)}")
            logger.info(f"頁面載入失敗數: {self.page_errors}")
            logger.info(f"耗時: {duration / 60:.1f} 分鐘")
            logger.info(f"{'=' * 80}\n")

            return True

        except Exception as e:
            logger.error(f"爬蟲執行時發生錯誤: {e}")
            return False

        finally:
            self.close_driver()

    def to_dataframe(self) -> pd.DataFrame:
        """將資料轉換為 DataFrame"""
        if not self.spirits_data:
            return pd.DataFrame()

        # 展開 flavor_data 為字串
        df_data = []
        for item in self.spirits_data:
            row = item.copy()
            if "flavor_data" in row and isinstance(row["flavor_data"], dict):
                row["flavor_data"] = json.dumps(row["flavor_data"])
            df_data.append(row)

        return pd.DataFrame(df_data)

    def save_csv(self, filename: str = "distiller_spirits_v2.csv") -> bool:
        """儲存為 CSV"""
        if not self.spirits_data:
            logger.warning("沒有資料可儲存")
            return False

        try:
            df = self.to_dataframe()
            df.to_csv(filename, index=False, encoding=ScraperConfig.OUTPUT_ENCODING)
            logger.info(f"✓ 資料已儲存至 {filename}")
            logger.info(f"✓ 總共 {len(df)} 條記錄")
            return True

        except Exception as e:
            logger.error(f"儲存 CSV 時發生錯誤: {e}")
            return False

    def get_statistics(self) -> Dict:
        """獲取統計資訊"""
        if not self.spirits_data:
            return {
                "總記錄數": 0,
                "失敗 URL 數": len(self.failed_urls),
                "頁面載入失敗數": self.page_errors,
            }

        df = self.to_dataframe()

        # 計算各欄位的有效率
        field_stats = {}
        for col in df.columns:
            if col in ["url", "flavor_data"]:
                continue
            valid_count = (
                (df[col] != "N/A") & (df[col] != "") & (df[col].notna())
            ).sum()
            field_stats[col] = (
                f"{valid_count}/{len(df)} ({valid_count / len(df) * 100:.1f}%)"
            )

        return {
            "總記錄數": len(df),
            "失敗 URL 數": len(self.failed_urls),
            "頁面載入失敗數": self.page_errors,
            "類別分布": df["category"].value_counts().to_dict()
            if "category" in df.columns
            else {},
            "欄位有效率": field_stats,
        }


def run_test_scrape():
    """測試爬取 (少量)"""
    scraper = DistillerScraperV2(headless=True)

    scraper.scrape(
        categories=["whiskey"],
        max_per_category=5,
        use_styles=False,
    )

    scraper.save_csv("distiller_test_v2.csv")
    stats = scraper.get_statistics()
    logger.info(f"\n統計:\n{json.dumps(stats, indent=2, ensure_ascii=False)}")


def run_medium_scrape():
    """中等規模爬取"""
    scraper = DistillerScraperV2(headless=True)

    scraper.scrape(
        categories=["whiskey", "gin", "rum", "vodka"],
        max_per_category=50,
        use_styles=True,
    )

    scraper.save_csv("distiller_spirits_v2.csv")
    stats = scraper.get_statistics()
    logger.info(f"\n統計:\n{json.dumps(stats, indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    run_test_scrape()
