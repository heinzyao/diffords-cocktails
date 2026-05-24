"""
Difford's Guide 資料提取工具

資料來源優先順序（已驗證 2026-04-12）：
  1. JSON-LD (schema.org) — 最穩定，覆蓋主要欄位，無需 JavaScript 渲染
  2. HTML parsing (BeautifulSoup) — 補充玻璃杯、裝飾、歷史、ABV 等欄位

HTML 結構（實際驗證）：
  - 玻璃杯：h3.m-0[text="Glass:"] → nextElementSibling.text
             結果含 "Photographed in a …" 前綴，自動移除
  - 調製步驟：h3.m-0[text="How to make:"] → nextElementSibling.text
  - 裝飾：h3.m-0[text="Garnish:"] → nextElementSibling.text
  - 準備：h3.m-0[text="Prepare:"] → nextElementSibling.text
  - 評語：h3.m-0[text="Review:"] → nextElementSibling.text
  - 歷史：h3.m-0[text="History:"] → nextElementSibling.text
  - 食材：table.legacy-ingredients-table tbody tr → td[0]=amount, td[1]=name
  - ABV：li 含 "alc./vol." 文字

JSON-LD 欄位對應：
  name, description, recipeIngredient, recipeInstructions,
  keywords, aggregateRating, nutrition.calories, totalTime, datePublished
"""

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

# 各單位的 regex（用於解析 JSON-LD recipeIngredient 字串）
_AMOUNT_PATTERN = re.compile(
    r"^((?:\d+(?:[./]\d+)?|[½¼¾⅓⅔])\s*"
    r"(?:ml|cl|oz|fl\.?oz|dashes?|tsp|tbsp|parts?|drops?|barspoon|splashes?|pinch)?)\s+"
    r"(.+)$",
    re.IGNORECASE,
)
_ABV_PATTERN = re.compile(r"([\d.]+)%\s*alc", re.IGNORECASE)
_CALORIES_PATTERN = re.compile(r"(\d+)")
_ISO_DURATION_PATTERN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?")


class DiffordsExtractor:
    """從 Difford's Guide 單頁 HTML 提取雞尾酒所有欄位。"""

    # ------------------------------------------------------------------
    # JSON-LD 提取
    # ------------------------------------------------------------------

    @staticmethod
    def extract_json_ld(html: str) -> Optional[dict]:
        """提取 <script type='application/ld+json'> 的 schema.org Recipe 資料。"""
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", type="application/ld+json")
        if not script or not script.string:
            return None
        try:
            data = json.loads(script.string)
            return data if data.get("@type") == "Recipe" else None
        except (json.JSONDecodeError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # HTML 欄位提取
    # ------------------------------------------------------------------

    @staticmethod
    def _h3_next_text(soup: BeautifulSoup, label: str) -> Optional[str]:
        """找 h3.m-0[text=label] 後的第一個兄弟元素文字。"""
        for h3 in soup.find_all("h3", class_="m-0"):
            if h3.get_text(strip=True) == label:
                nxt = h3.find_next_sibling()
                if nxt:
                    return nxt.get_text(separator=" ", strip=True)
        return None

    @staticmethod
    def extract_glassware(soup: BeautifulSoup) -> Optional[str]:
        """提取玻璃杯類型，移除 'Photographed in a ' 前綴。"""
        text = DiffordsExtractor._h3_next_text(soup, "Glass:")
        if not text:
            return None
        # 移除 "Photographed in a " 常見前綴
        return re.sub(r"(?i)^photographed\s+in\s+a?\s*", "", text).strip() or text

    @staticmethod
    def extract_ingredients_html(soup: BeautifulSoup) -> list[dict]:
        """從 table.legacy-ingredients-table 提取食材（含實際品牌名稱）。

        HTML 結構：
            <table class="legacy-ingredients-table">
              <tbody>
                <tr><td>45 ml</td><td>Strucchi Red Bitter...</td></tr>
        """
        table = soup.find("table", class_="legacy-ingredients-table")
        if not table:
            return []
        rows = []
        for i, tr in enumerate(table.find_all("tr")):
            tds = tr.find_all("td")
            if len(tds) >= 2:
                amount = tds[0].get_text(strip=True)
                name = tds[1].get_text(separator=" ", strip=True)
                if name:
                    rows.append({"sort_order": i + 1, "amount": amount, "item": name})
        return rows

    @staticmethod
    def parse_ingredients_json_ld(ld_ingredients: list[str]) -> list[dict]:
        """解析 JSON-LD recipeIngredient 為結構化清單（通用名稱）。

        格式範例：
            "45 ml Red bitter liqueur"  → amount="45 ml", item="Red bitter liqueur"
            "2 dashes Angostura bitters"→ amount="2 dashes", item="Angostura bitters"
        """
        result = []
        for i, raw in enumerate(ld_ingredients or []):
            raw = raw.strip()
            m = _AMOUNT_PATTERN.match(raw)
            if m:
                result.append({
                    "sort_order": i + 1,
                    "amount": m.group(1).strip(),
                    "item": m.group(2).strip(),
                })
            else:
                result.append({"sort_order": i + 1, "amount": "", "item": raw})
        return result

    @staticmethod
    def extract_abv(soup: BeautifulSoup) -> Optional[float]:
        """提取酒精度數（如 '16.14% alc./vol.'）。"""
        for li in soup.find_all("li"):
            text = li.get_text(strip=True)
            if "alc./vol" in text or "alc./" in text:
                m = _ABV_PATTERN.search(text)
                if m:
                    try:
                        return float(m.group(1))
                    except ValueError:
                        pass
        return None

    @staticmethod
    def extract_calories(ld: dict) -> Optional[int]:
        """從 JSON-LD nutrition.calories 提取整數卡路里。"""
        cal_str = (ld.get("nutrition") or {}).get("calories", "")
        m = _CALORIES_PATTERN.search(str(cal_str))
        return int(m.group(1)) if m else None

    @staticmethod
    def extract_prep_time_minutes(ld: dict) -> Optional[int]:
        """解析 ISO 8601 duration (e.g. 'PT03M0S' 或 'PT1H30M') → 總分鐘數。"""
        duration = ld.get("totalTime") or ""
        m = _ISO_DURATION_PATTERN.search(str(duration))
        if not m:
            return None
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        return hours * 60 + minutes or None

    # ------------------------------------------------------------------
    # 整合入口
    # ------------------------------------------------------------------

    @classmethod
    def _extract_html_only(cls, soup: BeautifulSoup) -> Optional[dict]:
        """純 HTML fallback：當 JSON-LD 不存在時，從 HTML 結構提取雞尾酒資料。

        可提取欄位：name、glassware、garnish、prepare、instructions、
                    review、history、abv、ingredients_html。
        無法提取欄位（設為 None）：description、tags、rating_value、
                    rating_count、calories、prep_time_minutes、
                    date_published、ingredients_generic。
        """
        # 名稱：從 <h1> 取得
        h1 = soup.find("h1")
        name = h1.get_text(strip=True) if h1 else None
        if not name:
            return None  # 連名稱都沒有，確實不是有效的雞尾酒頁面

        # 食材：從 HTML 表格取得（與 JSON-LD 路徑使用相同方法）
        ingredients_html = cls.extract_ingredients_html(soup)

        return {
            # ── 無 JSON-LD，基本欄位設為 None ──
            "name":               name,
            "description":        None,
            "tags":               [],
            "rating_value":       None,
            "rating_count":       None,
            "calories":           None,
            "prep_time_minutes":  None,
            "date_published":     None,
            # ── HTML 欄位（正常提取）──
            "glassware":          cls.extract_glassware(soup),
            "garnish":            cls._h3_next_text(soup, "Garnish:"),
            "prepare":            cls._h3_next_text(soup, "Prepare:"),
            "instructions":       cls._h3_next_text(soup, "How to make:"),
            "review":             cls._h3_next_text(soup, "Review:"),
            "history":            cls._h3_next_text(soup, "History:"),
            "abv":                cls.extract_abv(soup),
            # ── 食材（僅 HTML 來源，無通用名稱）──
            "ingredients_generic": [],
            "ingredients_html":    ingredients_html,
        }

    @classmethod
    def extract_all(cls, html: str) -> Optional[dict]:
        """從完整 HTML 提取所有欄位，回傳標準化 dict；失敗時回傳 None。

        優先使用 JSON-LD（穩定），HTML parsing 補充其餘欄位。
        若 JSON-LD 不存在但 HTML 結構有效，使用 HTML-only fallback 提取。
        """
        ld = cls.extract_json_ld(html)
        soup = BeautifulSoup(html, "html.parser")

        if not ld:
            return cls._extract_html_only(soup)

        rating = ld.get("aggregateRating") or {}

        return {
            # ── JSON-LD 欄位 ──
            "name":               ld.get("name", ""),
            "description":        ld.get("description"),
            "tags":               ld.get("keywords") or [],
            "rating_value":       float(rating["ratingValue"]) if rating.get("ratingValue") else None,
            "rating_count":       int(rating["ratingCount"]) if rating.get("ratingCount") else None,
            "calories":           cls.extract_calories(ld),
            "prep_time_minutes":  cls.extract_prep_time_minutes(ld),
            "date_published":     ld.get("datePublished"),
            # ── HTML 欄位 ──
            "glassware":          cls.extract_glassware(soup),
            "garnish":            cls._h3_next_text(soup, "Garnish:"),
            "prepare":            cls._h3_next_text(soup, "Prepare:"),
            "instructions":       cls._h3_next_text(soup, "How to make:"),
            "review":             cls._h3_next_text(soup, "Review:"),
            "history":            cls._h3_next_text(soup, "History:"),
            "abv":                cls.extract_abv(soup),
            # ── 食材（雙來源）──
            # ingredients_generic：JSON-LD 通用名稱，供推薦引擎匹配烈酒類別
            # ingredients_html：HTML 真實品牌名稱，供顯示用
            "ingredients_generic": cls.parse_ingredients_json_ld(ld.get("recipeIngredient") or []),
            "ingredients_html":    cls.extract_ingredients_html(soup),
        }
