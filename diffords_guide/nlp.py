"""自然語言 → `storage.query_cocktails()` 參數的解析層（Gemini）。

設計理由
--------
為什麼是「解析」而不是「生成」？
- 輸出是一組 kwargs，不是自由文字。可用 response_schema 在協議層約束結構，
  再經白名單驗證，最後餵給既有的 query_cocktails()。LLM 碰不到 SQL。
- 失敗時能乾淨退回 bot 既有的中文指令解析，原有行為零影響。

為什麼掛在 `parse_command()` 的 unknown 分支？
- 舊指令（「雞尾酒列表 材料 gin」）完全不經過這裡：零延遲、零成本、零風險。
- 只有本來就會回「無法識別此指令」的訊息才交給 Gemini，是純粹的加分項。

沒有 GEMINI_API_KEY 時整條路徑靜默停用（parse_query 回傳 None），
呼叫端退回原本的錯誤訊息 —— LLM 是增強，不是必要路徑。

資料是英文的
-----------
DB 裡的酒名、食材、標籤全是英文（"Gin"、"Campari"、"Classic/vintage"），
使用者卻是用中文問。把中文詞彙對應成英文查詢值是這一層的主要工作，
也是規則解析做不到、非得用 LLM 的原因。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from diffords_guide.storage import SORT_KEYS

logger = logging.getLogger(__name__)

# 預設模型與 cat-lendar 一致（該專案已實測穩定），可用環境變數覆寫
DEFAULT_MODEL = "gemini-3.8-flash"
# Gemini API 拒絕小於 10 秒的 deadline（400 INVALID_ARGUMENT），所以這是下限，
# 不是我們想要的值。實際回應多在 1-2 秒，10 秒只有異常時才會用盡；
# LINE 的 reply token 有 30 秒，撐得住。超時就退回舊行為。
DEFAULT_TIMEOUT_MS = 10_000

# 白名單：必須與 storage.query_cocktails() 的 kwargs 一致。
# 這是唯一允許進入查詢層的鍵集合 —— LLM 回傳的其他欄位一律丟棄。
_TEXT_KEYS = ("keyword", "description", "ingredient", "tag")
_RANGE_KEYS: dict[str, tuple[float, float]] = {
    "min_sweet_sour": (0.0, 10.0),
    "max_sweet_sour": (0.0, 10.0),
    "min_rating": (0.0, 5.0),
    "max_rating": (0.0, 5.0),
    "min_abv": (0.0, 100.0),
    "max_abv": (0.0, 100.0),
}
_LIMIT_MAX = 20

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string", "description": "酒名關鍵字（英文）"},
        "description": {"type": "string", "description": "描述關鍵字（英文）"},
        "ingredient": {"type": "string", "description": "食材，英文單數，如 gin、rum"},
        "tag": {"type": "string", "description": "標籤，如 Classic/vintage、Summer"},
        "min_rating": {"type": "number", "description": "最低評分 0-5"},
        "max_rating": {"type": "number", "description": "最高評分 0-5"},
        "min_abv": {"type": "number", "description": "最低酒精濃度 %"},
        "max_abv": {"type": "number", "description": "最高酒精濃度 %"},
        "min_count": {"type": "integer", "description": "最低評分人數"},
        "min_sweet_sour": {"type": "integer", "description": "甜酸下限 0-10"},
        "max_sweet_sour": {"type": "integer", "description": "甜酸上限 0-10"},
        "sort": {"type": "string", "enum": list(SORT_KEYS)},
        "desc": {"type": "boolean", "description": "true 為降序（預設）"},
        "limit": {"type": "integer", "description": f"筆數 1-{_LIMIT_MAX}"},
        "semantic_query": {
            "type": "string",
            "description": "使用者描述的風味口感，原話保留；沒描述風味就省略",
        },
    },
}

# tag 在 storage.query_cocktails() 是**精確比對**（LOWER(t.value) = LOWER(?)），
# 猜一個近似值會查到零筆 —— 例如 "Sour" 查不到，實際分類叫 "Sours (citrus)"。
# 所以把實際分類值給 Gemini 挑。這是 Difford's 的固定分類法，很少變動；
# 下面列的是出現 100 次以上的分類，更新方式：
#   sqlite3 diffords.db "SELECT value FROM cocktails, json_each(cocktails.tags)
#     GROUP BY value HAVING count(*) >= 100 ORDER BY count(*) DESC;"
_KNOWN_TAGS = (
    "Aperitivo/aperitif", "Spirit-forward", "Nightcap/sipping", "Classic/vintage",
    "Fruity (e.g. Pornstar Martini)", "Sours (citrus)", "Summer",
    "After dinner/digestif", "Citrusy", "Bittersweet (e.g. Negroni)",
    "Short and stirred", "Long drinks and highballs", "Herbal", "Anytime",
    "Elevenses/afternoon", "Martini-style", "Hall of Fame and must know/try",
    "Creamy (e.g. Dirty banana)", "Modern", "Dessert cocktails",
    "Spicy (e.g. Spicy Fifty)", "Tiki/tropical", "Champagne", "Breakfast/brunch",
    "Autumn/fall", "Shot cocktails", "Floral (e.g. Elderflower spritz)",
    "Contemporary classic", "Savoury (e.g. Bloody Mary)",
)

_SYSTEM_PROMPT = f"""你把使用者的中文雞尾酒查詢轉成資料庫查詢參數。

資料庫內容全是英文：酒名、食材、標籤都是英文。使用者用中文問，
你要輸出對應的英文查詢值。例如「琴酒」→ ingredient: "gin"、
「蘭姆酒」→ "rum"、「威士忌」→ "whiskey"、「龍舌蘭」→ "tequila"。

可用的 sort 值：{", ".join(SORT_KEYS)}（分別是評分、酒精濃度、卡路里、
發布日期、名稱、評分人數）。

tag 必須從下列清單原字照抄（精確比對，自創的值會查到零筆）。
挑不到合適的就不要輸出 tag：
{chr(10).join("  " + t for t in _KNOWN_TAGS)}

判斷原則：
- 只輸出你有把握的欄位。無法判斷的就省略，不要猜測或填預設值。
- 「好喝的」「評價好的」→ min_rating 約 4.0
- 「烈一點」→ min_abv 約 30；「不要太烈」「順口」→ max_abv 約 20
- 甜酸是 0-10 的軸，**數值越高越酸/乾，越低越甜**（甜點調酒約 4-5、
  酸味調酒約 7-8）。「甜一點」→ max_sweet_sour 約 5；
  「不要太甜」「清爽」→ min_sweet_sour 約 6；「很酸」→ min_sweet_sour 約 7
- 沒有提到筆數就不要輸出 limit。
- 若訊息根本不是在查雞尾酒（例如閒聊、問天氣），回傳空物件 {{}}。

風味描述走另一條路：
如果使用者描述的是**喝起來的感覺**（「苦苦的」「有草本味」「清爽」
「奶味濃」「煙燻感」），把那段描述原話放進 semantic_query —— 這些
形容詞在資料庫裡沒有對應欄位，要靠語意比對酒譜的品飲評語。
可以和其他條件並存：「琴酒做的、苦苦的」→ ingredient="gin" 且
semantic_query="苦苦的"。純粹是分類或數值的條件（材料、評分、標籤、
酒精濃度、甜酸）不要放進 semantic_query。"""


def _clean_text(value: Any) -> Optional[str]:
    """文字欄位：非字串或空白一律丟棄。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_number(value: Any, low: float, high: float) -> Optional[float]:
    """數值欄位：可轉為 float 且落在範圍內才保留。

    bool 是 int 的子類別，必須先擋掉，否則 True 會變成 1.0。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if low <= number <= high else None


def sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 輸出過濾成 query_cocktails() 可安全接受的 kwargs。

    逐欄位檢查而非整批拒絕 —— 一個欄位不合法不該讓整次查詢失敗，
    剩下的條件通常仍然有用。
    """
    out: dict[str, Any] = {}

    for key in _TEXT_KEYS:
        value = _clean_text(data.get(key))
        if value is not None:
            out[key] = value

    for key, (low, high) in _RANGE_KEYS.items():
        value = _clean_number(data.get(key), low, high)
        if value is not None:
            out[key] = value

    count = _clean_number(data.get("min_count"), 0, 10**6)
    if count is not None:
        out["min_count"] = int(count)

    # 甜酸軸是整數刻度，_RANGE_KEYS 走 float，這裡統一轉回 int
    for key in ("min_sweet_sour", "max_sweet_sour"):
        if key in out:
            out[key] = int(out[key])

    semantic = _clean_text(data.get("semantic_query"))
    if semantic:
        out["semantic_query"] = semantic

    sort = _clean_text(data.get("sort"))
    if sort in SORT_KEYS:
        out["sort"] = sort

    if isinstance(data.get("desc"), bool):
        out["desc"] = data["desc"]

    limit = _clean_number(data.get("limit"), 1, _LIMIT_MAX)
    if limit is not None:
        out["limit"] = int(limit)

    return out


def parse_query(text: str) -> Optional[dict[str, Any]]:
    """自然語言 → query_cocktails kwargs；無法使用或解析不出條件時回傳 None。

    回傳 None 的情況全部由呼叫端以「退回既有行為」處理：
      - 沒設 GEMINI_API_KEY（本機開發、未掛 secret 的環境）
      - API 逾時、網路錯誤、額度用盡
      - 回傳非 JSON，或清理後沒有任何合法條件
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai 未安裝，自然語言查詢停用")
        return None

    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
    timeout_ms = int(os.getenv("GEMINI_TIMEOUT_MS", DEFAULT_TIMEOUT_MS))

    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms)
        )
        response = client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                # 我們沒有給 tools，AFC 只會產生警告噪音
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        raw = (response.text or "").strip()
    except Exception as exc:
        # 逾時、額度、網路問題都走同一條路：安靜退回舊行為
        logger.warning("Gemini 查詢解析失敗（%s）：%s", type(exc).__name__, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Gemini 回傳非 JSON：%s", raw[:200])
        return None

    if not isinstance(data, dict):
        return None

    args = sanitize(data)
    return args or None
