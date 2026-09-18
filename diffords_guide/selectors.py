"""
Difford's Guide 資料提取工具

資料來源優先順序（已驗證 2026-09-18）：
  1. JSON-LD (schema.org) — 最穩定，覆蓋主要欄位，無需 JavaScript 渲染
  2. HTML parsing (BeautifulSoup) — 補充玻璃杯、評語、ABV 等欄位

網站在 2026-08 中旬改版，提取器同時支援新舊兩種結構（`_heading_next_text`
接受多個 label，比對時忽略大小寫與尾隨冒號）。

改版後（現行）：
  - 玻璃杯：h3[text="Glassware"] → 下一個兄弟，含 "Serve in a …" 前綴，自動移除
  - 調製步驟：JSON-LD recipeInstructions（HowToStep 陣列）優先，
              HTML fallback 為 h2[text="Method"] → 下一個兄弟
  - 裝飾：JSON-LD HowToStep 中 name 含 "garnish" 的步驟，可能多個，依序串接
  - 評語：h2[text="Review"] → 下一個兄弟
  - 食材：table.cocktail-ingredients__table tbody tr → td[0]=amount, td[1]=name
  - ABV：li 含 "alc./vol." 文字（頁面資料不足時會顯示說明文字而非數值，此時為 None）
  - 準備、歷史：**改版後已無對應區塊**，一律為 None

改版前（仍支援，GCS 上多數資料抓於此時期）：
  - 標籤為 h3.m-0 且帶冒號："Glass:"、"Garnish:"、"Prepare:"、
    "How to make:"、"Review:"、"History:"
  - 玻璃杯前綴為 "Photographed in a …"
  - 食材表為 table.legacy-ingredients-table

因為 prepare / history 在新版必為 None，`storage._upsert_cocktail` 對 HTML
來源欄位一律使用 COALESCE，避免重爬時把改版前抓到的資料清空。

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
    def _heading_next_text(soup: BeautifulSoup, *labels: str) -> Optional[str]:
        """找標題文字符合任一 label 的 h2/h3，回傳其下一個兄弟元素的文字。

        比對時忽略大小寫與尾隨冒號，且不限定 class —— 2026-08 改版把
        `<h3 class="m-0">Glass:</h3>` 換成
        `<h3 class="cocktail-sub-heading">Glassware</h3>`。
        同時傳新舊兩種 label 即可讓兩種結構都命中。
        """
        wanted = {label.rstrip(":").lower() for label in labels}
        for heading in soup.find_all(["h2", "h3"]):
            if heading.get_text(strip=True).rstrip(":").lower() in wanted:
                nxt = heading.find_next_sibling()
                if nxt:
                    return nxt.get_text(separator=" ", strip=True)
        return None

    @staticmethod
    def _steps(ld: Optional[dict]) -> list[dict]:
        """取出 JSON-LD 的 HowToStep 清單（非 dict 元素一律略過）。"""
        raw = (ld or {}).get("recipeInstructions") or []
        return [st for st in raw if isinstance(st, dict)]

    @staticmethod
    def extract_glassware(soup: BeautifulSoup) -> Optional[str]:
        """提取玻璃杯類型，移除 'Serve in a' / 'Photographed in a' 前綴。"""
        text = DiffordsExtractor._heading_next_text(soup, "Glassware", "Glass:")
        if not text:
            return None
        stripped = re.sub(
            r"(?i)^(?:photographed|serve[d]?)\s+in\s+an?\s*", "", text
        ).strip()
        return stripped or text

    @classmethod
    def extract_instructions(
        cls, ld: Optional[dict], soup: BeautifulSoup
    ) -> Optional[str]:
        """調製步驟。優先用 JSON-LD 的 HowToStep，其次讀 HTML 的 Method 區塊。

        改版後 HTML 的步驟藏在 <ol> 裡，JSON-LD 反而結構更乾淨，故改以它為主。
        """
        texts = [
            st.get("text", "").strip() for st in cls._steps(ld) if st.get("text")
        ]
        if texts:
            return " ".join(texts)
        return cls._heading_next_text(soup, "Method", "How to make:")

    @classmethod
    def extract_garnish(cls, ld: Optional[dict], soup: BeautifulSoup) -> Optional[str]:
        """裝飾。改版後沒有獨立區塊，改由 HowToStep 中與 garnish 相關的步驟合併。

        單一酒譜可能有多個 garnish 步驟（如 'Prepare garnish' 兩次加一次
        'Garnish'），全部保留並依原順序串接。
        """
        texts = [
            st.get("text", "").strip()
            for st in cls._steps(ld)
            if "garnish" in (st.get("name") or "").lower() and st.get("text")
        ]
        if texts:
            return " ".join(texts)
        return cls._heading_next_text(soup, "Garnish:")

    @staticmethod
    def extract_ingredients_html(soup: BeautifulSoup) -> list[dict]:
        """從 table.legacy-ingredients-table 提取食材（含實際品牌名稱）。

        HTML 結構：
            <table class="legacy-ingredients-table">
              <tbody>
                <tr><td>45 ml</td><td>Strucchi Red Bitter...</td></tr>
        """
        # cocktail-ingredients__table 是 2026-08 改版後的新 class，
        # legacy-ingredients-table 保留給改版前的頁面。
        table = soup.find(
            "table", class_=["cocktail-ingredients__table", "legacy-ingredients-table"]
        )
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
            "garnish":            cls.extract_garnish(None, soup),
            "prepare":            cls._heading_next_text(soup, "Prepare:"),
            "instructions":       cls.extract_instructions(None, soup),
            "review":             cls._heading_next_text(soup, "Review"),
            "history":            cls._heading_next_text(soup, "History:"),
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
            "garnish":            cls.extract_garnish(ld, soup),
            "prepare":            cls._heading_next_text(soup, "Prepare:"),
            "instructions":       cls.extract_instructions(ld, soup),
            "review":             cls._heading_next_text(soup, "Review"),
            "history":            cls._heading_next_text(soup, "History:"),
            "abv":                cls.extract_abv(soup),
            # ── 食材（雙來源）──
            # ingredients_generic：JSON-LD 通用名稱，供查詢與資料分析使用
            # ingredients_html：HTML 真實品牌名稱，供顯示用
            "ingredients_generic": cls.parse_ingredients_json_ld(ld.get("recipeIngredient") or []),
            "ingredients_html":    cls.extract_ingredients_html(soup),
        }
