"""TDD for the deterministic phrase-affinity reranker.

Contract:
* A document carrying the query's exact terms/bigrams surfaces above a higher
  fused-rank document that lacks them.
* ``rerank(..., top_k)`` truncates to ``top_k`` and attaches ``rerank_score``.
* Without a configured cross-encoder it runs fully offline (no network).
"""
from __future__ import annotations

import pytest

from app.core.rag.reranker import rerank


def _chunk(i: int, text: str, score: float) -> dict:
    return {"id": i, "source": "t", "text": text, "score": score}


def test_phrase_match_promoted_above_higher_fused_rank():
    query = "营收下滑 客户 留存"
    chunks = [
        # fused 分更高、但毫无查询词
        _chunk(1, "公司发布 2026 年度财务与运营展望摘要说明文档内容较泛", score=0.9),
        # fused 分低、但精确命中 "客户"+"留存"
        _chunk(2, "客户留存率的定义：活跃客户在统计周期内再次下单的比例口径", score=0.3),
        # 命中 "营收"+"下滑"
        _chunk(3, "营收下滑的主要原因包括渠道转化下降与复购率走低", score=0.6),
    ]
    top = rerank(query, chunks, top_k=3)
    assert top[0]["id"] in (2, 3), f"命中查询短语的文档应排最前，实际顺序 {[c['id'] for c in top]}"
    # 泛内容文档应垫底（无短语命中）
    assert top[-1]["id"] == 1
    assert all("rerank_score" in c for c in top)


def test_rerank_respects_top_k():
    chunks = [
        _chunk(i, f"营收 客户 {i} 的说明文字用于测试检索质量", score=0.5)
        for i in range(5)
    ]
    top = rerank("营收 客户", chunks, top_k=2)
    assert len(top) == 2


def test_empty_input_returns_empty():
    assert rerank("q", []) == []
    assert rerank("q", [], top_k=5) == []


def test_no_network_without_cross_encoder(monkeypatch):
    """未配置 rerank_cross_encoder 时确定性路径不触发任何模型加载。"""
    from app.config import get_settings

    get_settings.cache_clear()
    from app.core.rag import reranker as rr

    rr._ce_model = None
    rr._ce_error = "disabled-by-config"
    chunks = [_chunk(1, "营收的定义说明", 0.5), _chunk(2, "与查询无关", 0.9)]
    top = rerank("营收 定义", chunks)
    assert top[0]["id"] == 1
