"""
Difford's Guide 雞尾酒譜爬蟲

架構設計
--------
採用「Sitemap 優先 + requests 爬取」策略，不需要 Selenium：

┌──────────────────────────────────────────────────┐
│  DiffordsGuideScraper                            │
│                                                  │
│  parse_sitemap()  ← requests + xml.etree         │  Phase 1: URL 收集
│       ↓                                          │
│  scrape()         ← 主流程                       │
│       ├─ 增量更新判斷（seen_urls + lastmod 比對） │
│       └─ _fetch_recipe() ← requests              │  Phase 2: 內容爬取
│              └─ DiffordsExtractor.extract_all()  │
│                   ├─ JSON-LD 解析（主要欄位）     │
│                   └─ BeautifulSoup（補充欄位）   │
└──────────────────────────────────────────────────┘

為何不需要 Selenium
-------------------
Difford's Guide 在初始 HTML 中嵌入完整的 schema.org JSON-LD 結構化資料，
包含食材、步驟、評分、標籤等主要欄位，requests 直接可取得，無需瀏覽器渲染。
其餘欄位（玻璃杯、裝飾、歷史）透過 BeautifulSoup 解析靜態 HTML 補充。

增量更新機制（兩層）
-------------------
1. seen_urls：啟動時從 DB 載入所有已爬 URL → Set（O(1) 查詢）
2. lastmod 比對：sitemap lastmod ≤ DB lastmod → 跳過
"""

import logging
import random
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DEFAULT_DELAY_MAX, DEFAULT_DELAY_MIN, SITEMAP_URL, _HEADERS
from .selectors import DiffordsExtractor
from .storage import DiffordsStorage

logger = logging.getLogger(__name__)

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass
class SitemapEntry:
    cocktail_id: int
    slug: str
    url: str
    lastmod: Optional[str] = None


@dataclass
class ScrapeStats:
    scraped: int = 0
    skipped: int = 0
    failed: int = 0
    start_time: datetime = field(default_factory=datetime.now)

    @property
    def duration_secs(self) -> int:
        return int((datetime.now() - self.start_time).total_seconds())

    def to_dict(self) -> dict:
        d, r = divmod(self.duration_secs, 60)
        return {
            "爬取新增": self.scraped,
            "跳過（已是最新）": self.skipped,
            "失敗": self.failed,
            "耗時": f"{d}m {r}s",
        }


def _build_session() -> requests.Session:
    """建立帶自動重試的 HTTP Session。"""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


class DiffordsGuideScraper:
    """Difford's Guide 雞尾酒譜爬蟲（無需 Selenium，純 requests + BeautifulSoup）。

    使用方式：
        storage = DiffordsStorage("diffords.db")
        scraper = DiffordsGuideScraper(storage=storage)
        scraper.scrape()                         # 增量更新
        scraper.scrape(incremental=False)        # 全量重爬
        scraper.scrape(max_recipes=10)           # 測試：只爬 10 筆
    """

    def __init__(
        self,
        storage: Optional[DiffordsStorage] = None,
        delay_min: float = DEFAULT_DELAY_MIN,
        delay_max: float = DEFAULT_DELAY_MAX,
    ):
        self.storage = storage
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.stats = ScrapeStats()
        self.failed_urls: list[str] = []
        self.session = _build_session()

        # 啟動時從 DB 預載，避免重複請求
        self.seen_urls: set[str] = storage.get_existing_urls() if storage else set()
        self.lastmod_map: dict[str, str] = (
            storage.get_url_lastmod_map() if storage else {}
        )

        logger.info(
            "DiffordsGuideScraper 初始化完成：DB 已有 %d 筆雞尾酒",
            len(self.seen_urls),
        )

    # ── Phase 1：Sitemap 解析 ──────────────────────────────────────────

    def parse_sitemap(self) -> list[SitemapEntry]:
        """解析 sitemap.xml，回傳所有雞尾酒詳情頁 URL 清單（含 lastmod）。"""
        logger.info("解析 Sitemap: %s", SITEMAP_URL)
        try:
            resp = self.session.get(SITEMAP_URL, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Sitemap 取得失敗: %s", e)
            return []

        ns = {"sm": SITEMAP_NAMESPACE}
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            logger.error("Sitemap XML 解析失敗: %s", e)
            return []

        entries: list[SitemapEntry] = []
        for url_el in root.findall("sm:url", ns):
            loc = url_el.findtext("sm:loc", namespaces=ns) or ""
            lastmod = url_el.findtext("sm:lastmod", namespaces=ns)

            # 只取 /cocktails/recipe/{id}/{slug} 格式的 URL
            parts = loc.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2].isdigit() and "/cocktails/recipe/" in loc:
                try:
                    entries.append(
                        SitemapEntry(
                            cocktail_id=int(parts[-2]),
                            slug=parts[-1],
                            url=loc,
                            lastmod=lastmod,
                        )
                    )
                except (ValueError, IndexError):
                    continue

        logger.info("Sitemap 解析完成：共 %d 筆雞尾酒 URL", len(entries))
        return entries

    # ── Phase 2：詳情頁爬取 ─────────────────────────────────────────────

    def _should_skip(self, entry: SitemapEntry, incremental: bool) -> bool:
        """判斷此條目是否可跳過。"""
        if not incremental:
            return False
        if entry.url not in self.seen_urls:
            return False  # 新 URL，必爬
        if not entry.lastmod:
            return True  # 無 lastmod，保守跳過
        db_lastmod = self.lastmod_map.get(entry.url)
        if not db_lastmod:
            return False  # DB 無 lastmod，需重爬
        return entry.lastmod <= db_lastmod  # sitemap 未更新 → 跳過

    def _fetch_recipe(self, url: str) -> Optional[dict]:
        """爬取並解析單個雞尾酒頁面，失敗回傳 None。"""
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("請求失敗 %s: %s", url, e)
            return None

        data = DiffordsExtractor.extract_all(resp.text)
        if data is None:
            logger.warning("提取失敗（無 JSON-LD 且 HTML 無有效名稱）: %s", url)
        return data

    def scrape(
        self,
        max_recipes: Optional[int] = None,
        incremental: bool = True,
        entries: Optional[list[SitemapEntry]] = None,
    ) -> bool:
        """主爬取流程。

        Args:
            max_recipes: 最多爬取筆數（None = 無限制）
            incremental: True = 跳過未更新的 URL（預設）；False = 全量重爬
            entries:     預先解析的 sitemap 條目，None 則自動解析 sitemap

        Returns:
            True  = 執行成功（含部分失敗）
            False = 完全失敗（無法取得 sitemap 或所有請求均失敗）
        """
        if entries is None:
            entries = self.parse_sitemap()
        if not entries:
            logger.error("無 sitemap 條目，中止爬取")
            return False

        mode_label = "增量" if incremental else "全量"
        logger.info("開始 %s 爬取，共 %d 筆", mode_label, len(entries))

        for i, entry in enumerate(entries, 1):
            if max_recipes is not None and self.stats.scraped >= max_recipes:
                logger.info("已達最大爬取數量 %d，停止", max_recipes)
                break

            if self._should_skip(entry, incremental):
                self.stats.skipped += 1
                if self.stats.skipped % 200 == 0:
                    logger.info("已跳過 %d 筆（未更新）", self.stats.skipped)
                continue

            logger.info(
                "[%d/%d] %s",
                i,
                len(entries),
                entry.url,
            )
            data = self._fetch_recipe(entry.url)

            if data is None:
                self.stats.failed += 1
                self.failed_urls.append(entry.url)
                logger.warning("  ✗ 失敗")
            else:
                data["url"] = entry.url
                data["lastmod"] = entry.lastmod
                if self.storage:
                    saved = self.storage.save_cocktail(data)
                    if not saved:
                        self.stats.failed += 1
                        self.failed_urls.append(entry.url)
                        logger.warning("  ✗ 儲存失敗（save_cocktail 回傳 False）")
                        time.sleep(random.uniform(self.delay_min, self.delay_max))
                        continue
                self.seen_urls.add(entry.url)
                if entry.lastmod:
                    self.lastmod_map[entry.url] = entry.lastmod
                self.stats.scraped += 1
                ing_generic = data.get("ingredients_generic") or []
                ing_html = data.get("ingredients_html") or []
                ing_count = len(ing_generic) or len(ing_html)
                source = "JSON-LD" if ing_generic else "HTML"
                logger.info(
                    "  ✓ %s（評分 %s，%d 種食材，%s）",
                    data.get("name", "?"),
                    data.get("rating_value", "N/A"),
                    ing_count,
                    source,
                )

            # 隨機延遲（尊重網站頻率限制）
            time.sleep(random.uniform(self.delay_min, self.delay_max))

        logger.info(
            "爬取完成：新增 %d，跳過 %d，失敗 %d，耗時 %ds",
            self.stats.scraped,
            self.stats.skipped,
            self.stats.failed,
            self.stats.duration_secs,
        )
        return self.stats.scraped > 0 or self.stats.skipped > 0

    def get_statistics(self) -> dict:
        return self.stats.to_dict()

    def close(self):
        self.session.close()
