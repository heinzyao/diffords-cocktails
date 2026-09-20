"""風味語意檢索：把 review 文字轉成向量，用 cosine 相似度排序。

為什麼是 review 而不是 description
----------------------------------
`description` 是機器模板（「Discover how to make X using A, B, C in N easy
steps」），嵌入它等於嵌入食材欄位，做語意檢索只會拿到「食材相似」的結果 ——
那用 `WHERE item_generic IN (...)` 就夠了。`review` 才是真正的風味描述，
例如 Paper Plane 的「Bittersweet with underlying bourbon character and
lemon zest」。

為什麼不用向量資料庫
------------------
6,961 筆 × 256 維 float32 = 約 7 MB，全部讀進記憶體做矩陣乘法不到 10 毫秒。
FAISS / pgvector 要解決的是這個規模的一千倍以上的問題。
# ponytail: 暴力全表掃描，資料量大一個數量級再考慮近似最近鄰

維度選擇
-------
gemini-embedding-001 預設 3072 維，但支援 Matryoshka 截斷。用 256 維是為了
DB 大小：3072 維會讓 diffords.db 從 8.8 MB 漲到 90 MB 以上，而 bot 每次
cold start 都要從 GCS 下載整顆。

**截斷後的向量不是單位長度**（實測 L2 範數約 0.42），所以存進去之前一律
正規化，檢索時才能直接用點積當 cosine 相似度。
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

EMBED_MODEL = "gemini-embedding-001"
DIMS = 256
# Gemini 對單次 embed_content 的 contents 數量有上限，100 是保守值
BATCH_SIZE = 100

_DDL = """
CREATE TABLE IF NOT EXISTS cocktail_embeddings (
    cocktail_id INTEGER PRIMARY KEY REFERENCES cocktails(id) ON DELETE CASCADE,
    vector      BLOB NOT NULL,
    dims        INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _source_hash(text: str) -> str:
    """review 文字的指紋，用來判斷重爬後是否需要重新生成向量。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(vector: list[float]) -> np.ndarray:
    """轉成單位向量，讓之後的點積直接等於 cosine 相似度。"""
    arr = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(arr)
    return arr / norm if norm else arr


def embed_texts(texts: list[str], *, task_type: str) -> Optional[list[np.ndarray]]:
    """呼叫 Gemini 取得正規化後的向量；不可用時回傳 None。

    task_type 要與用途相符：建索引用 RETRIEVAL_DOCUMENT，
    查詢用 RETRIEVAL_QUERY —— 兩者的向量空間是對齊的，但方向不同。
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai 未安裝，語意檢索停用")
        return None

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.embed_content(
            model=os.getenv("GEMINI_EMBED_MODEL", EMBED_MODEL),
            contents=texts,
            config=types.EmbedContentConfig(
                task_type=task_type, output_dimensionality=DIMS
            ),
        )
    except Exception as exc:
        logger.warning("Embedding 失敗（%s）：%s", type(exc).__name__, exc)
        return None

    return [_normalize(e.values) for e in response.embeddings]


def build_index(db_path: str, *, rebuild: bool = False) -> dict[str, int]:
    """為所有有 review 的酒譜建立／更新向量索引。

    只處理 review 有變動的（比對 source_hash），所以重爬後再跑一次很便宜。
    rebuild=True 會忽略既有 hash 全部重算。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)

    rows = conn.execute(
        "SELECT id, review FROM cocktails WHERE review IS NOT NULL AND review != ''"
    ).fetchall()
    existing = {
        r["cocktail_id"]: r["source_hash"]
        for r in conn.execute("SELECT cocktail_id, source_hash FROM cocktail_embeddings")
    }

    pending = [
        (r["id"], r["review"])
        for r in rows
        if rebuild or existing.get(r["id"]) != _source_hash(r["review"])
    ]
    stats = {"總數": len(rows), "需更新": len(pending), "已寫入": 0, "失敗": 0}
    logger.info("向量索引：共 %d 筆，需更新 %d 筆", len(rows), len(pending))

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        vectors = embed_texts([text for _, text in batch], task_type="RETRIEVAL_DOCUMENT")
        if vectors is None:
            stats["失敗"] += len(batch)
            continue
        with conn:
            conn.executemany(
                "INSERT INTO cocktail_embeddings"
                " (cocktail_id, vector, dims, source_hash, created_at)"
                " VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)"
                " ON CONFLICT(cocktail_id) DO UPDATE SET"
                " vector=excluded.vector, dims=excluded.dims,"
                " source_hash=excluded.source_hash, created_at=CURRENT_TIMESTAMP",
                [
                    (cid, vec.tobytes(), DIMS, _source_hash(text))
                    for (cid, text), vec in zip(batch, vectors)
                ],
            )
        stats["已寫入"] += len(batch)
        logger.info("  已處理 %d/%d", stats["已寫入"], len(pending))

    conn.close()
    return stats


def load_index(conn: sqlite3.Connection) -> tuple[list[int], Optional[np.ndarray]]:
    """讀出整份索引：(cocktail_id 清單, 形狀為 (N, DIMS) 的矩陣)。

    索引表可能根本不存在（DB 是爬蟲建的，從沒跑過 build_index），
    這是正常狀態而非錯誤 —— 當成空索引處理，讓呼叫端退回舊行為。
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cocktail_embeddings'"
    ).fetchone()
    if not table_exists:
        return [], None

    rows = conn.execute(
        "SELECT cocktail_id, vector FROM cocktail_embeddings WHERE dims = ?", (DIMS,)
    ).fetchall()
    if not rows:
        return [], None
    ids = [r[0] for r in rows]
    matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32)
    return ids, matrix.reshape(len(ids), DIMS)


def search(
    db_path: str,
    query: str,
    *,
    limit: int = 10,
    candidate_ids: Optional[list[int]] = None,
) -> Optional[list[dict[str, Any]]]:
    """風味語意檢索。索引不存在或 API 不可用時回傳 None（讓呼叫端退回舊行為）。

    candidate_ids 用來與結構化條件組合：「琴酒做的，苦苦的」應該先用
    query_cocktails 篩出琴酒調酒，再在那個子集裡做風味排序 —— 否則純語意
    檢索會回傳一堆不含琴酒的苦味酒。傳 None 代表在全庫裡找。
    """
    vectors = embed_texts([query], task_type="RETRIEVAL_QUERY")
    if not vectors:
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ids, matrix = load_index(conn)
        if matrix is None:
            logger.info("向量索引尚未建立，語意檢索略過")
            return None

        if candidate_ids is not None:
            allowed = set(candidate_ids)
            keep = [i for i, cid in enumerate(ids) if cid in allowed]
            if not keep:
                return []
            ids = [ids[i] for i in keep]
            matrix = matrix[keep]

        # 兩邊都已正規化，點積即 cosine 相似度
        scores = matrix @ vectors[0]
        top = np.argsort(-scores)[:limit]
        top_ids = [ids[i] for i in top]

        placeholders = ",".join("?" * len(top_ids))
        found = {
            r["id"]: dict(r)
            for r in conn.execute(
                f"SELECT id, name, review, rating_value, abv, sweet_sour"
                f" FROM cocktails WHERE id IN ({placeholders})",
                top_ids,
            )
        }
    finally:
        conn.close()

    # 依相似度排序輸出（SQL 的 IN 不保證順序）
    results = []
    for idx in top:
        row = found.get(ids[idx])
        if row:
            row["score"] = float(scores[idx])
            results.append(row)
    return results
