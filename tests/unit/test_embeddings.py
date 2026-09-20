"""向量索引與語意檢索的測試。

Gemini 的回應全部 mock —— 這裡要驗的是我們自己的邏輯：
正規化、增量重建的判斷、候選集過濾，以及向量存取的 round-trip。
"""

import sqlite3
from unittest.mock import patch

import numpy as np
import pytest

from diffords_guide import embeddings
from diffords_guide.storage import DiffordsStorage


def _fake_vectors(n: int, seed: int = 0) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    return [
        embeddings._normalize(rng.normal(size=embeddings.DIMS).tolist())
        for _ in range(n)
    ]


@pytest.fixture
def db(tmp_path):
    """三筆有 review 的酒譜。"""
    path = str(tmp_path / "t.db")
    with DiffordsStorage(path) as st:
        for cid, name, review in [
            (1, "Negroni", "Bittersweet and herbal."),
            (2, "Daiquiri", "Crisp, citrus-forward and refreshing."),
            (3, "White Russian", "Rich, creamy and sweet."),
        ]:
            st.save_cocktail({
                "name": name,
                "url": f"https://www.diffordsguide.com/cocktails/recipe/{cid}/{name.lower()}",
                "review": review,
            })
    return path


def test_normalize_produces_unit_vector():
    """截斷後的 Gemini 向量不是單位長度，必須自己正規化，點積才等於 cosine。"""
    vec = embeddings._normalize([3.0, 4.0] + [0.0] * 10)

    assert np.isclose(np.linalg.norm(vec), 1.0)
    assert vec.dtype == np.float32


def test_normalize_survives_zero_vector():
    """全零向量不該變成 NaN（除以零）。"""
    vec = embeddings._normalize([0.0, 0.0, 0.0])

    assert not np.isnan(vec).any()


def test_build_index_writes_all_rows_then_skips_unchanged(db):
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t))):
        first = embeddings.build_index(db)
        second = embeddings.build_index(db)

    assert first == {"總數": 3, "需更新": 3, "已寫入": 3, "失敗": 0}
    # review 沒變，第二次不該再打 API
    assert second["需更新"] == 0
    assert second["已寫入"] == 0


def test_build_index_reembeds_when_review_changes(db):
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t))):
        embeddings.build_index(db)

        conn = sqlite3.connect(db)
        with conn:
            conn.execute("UPDATE cocktails SET review = 'Completely new tasting note.' WHERE id = 1")
        conn.close()

        after = embeddings.build_index(db)

    assert after["需更新"] == 1


def test_build_index_rebuild_ignores_hashes(db):
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t))):
        embeddings.build_index(db)
        forced = embeddings.build_index(db, rebuild=True)

    assert forced["需更新"] == 3


def test_build_index_reports_failure_without_writing(db):
    """API 不可用時要記在 stats 裡，不能靜靜地寫出空索引。"""
    with patch.object(embeddings, "embed_texts", return_value=None):
        stats = embeddings.build_index(db)

    assert stats["失敗"] == 3
    assert stats["已寫入"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM cocktail_embeddings").fetchone()[0] == 0
    conn.close()


def test_vectors_survive_blob_round_trip(db):
    """存成 BLOB 再讀回來必須完全一致，否則相似度會悄悄算錯。"""
    vectors = _fake_vectors(3, seed=7)
    with patch.object(embeddings, "embed_texts", return_value=vectors):
        embeddings.build_index(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ids, matrix = embeddings.load_index(conn)
    conn.close()

    assert matrix.shape == (3, embeddings.DIMS)
    order = {cid: i for i, cid in enumerate(ids)}
    for original, cid in zip(vectors, [1, 2, 3]):
        assert np.allclose(matrix[order[cid]], original)


def test_search_restricts_to_candidate_ids(db):
    """有結構化條件時，語意排序只能在候選集內進行。"""
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t), seed=3)):
        embeddings.build_index(db)
        results = embeddings.search(db, "creamy", limit=10, candidate_ids=[2, 3])

    assert {r["id"] for r in results} <= {2, 3}


def test_search_returns_empty_list_when_no_candidate_matches(db):
    """候選集與索引無交集時回空清單（不是 None —— None 代表功能不可用）。"""
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t))):
        embeddings.build_index(db)
        results = embeddings.search(db, "anything", candidate_ids=[999])

    assert results == []


def test_search_returns_none_when_index_missing(db):
    """索引沒建過 → None，呼叫端據此退回舊行為。"""
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t))):
        assert embeddings.search(db, "smoky") is None


def test_search_orders_by_similarity(db):
    """回傳順序必須是相似度由高到低。"""
    with patch.object(embeddings, "embed_texts", side_effect=lambda t, **k: _fake_vectors(len(t), seed=11)):
        embeddings.build_index(db)
        results = embeddings.search(db, "herbal", limit=3)

    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
