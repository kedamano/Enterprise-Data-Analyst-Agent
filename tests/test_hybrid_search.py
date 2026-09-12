"""TDD suite for hybrid retrieval: BM25 + vector + RRF fusion (project-python pattern).

Contract under test:

* ``bm25_scores`` implements Okapi BM25 (k1=1.5, b=0.75) over tokenized docs.
* ``rrf_fuse`` implements Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank_i(d)),
  k=60, fusing multiple rankings into one.
* The SQLite knowledge store uses BOTH rankings (keyword BM25 + vector cosine)
  and fuses them — a doc that is the top hit in either channel surfaces at
  (or near) the top of the fused result, instead of whichever single channel
  the old either/or scoring happened to pick.
"""
from __future__ import annotations

import math

import pytest

from app.core.tools.knowledge_tool import (
    KnowledgeStore,
    _bm25_scores,
    _rrf_fuse,
    _tokenize,
)


# --------------------------------------------------------------------------- #
# 1. BM25
# --------------------------------------------------------------------------- #
def test_tokenize_mixed_language():
    toks = _tokenize("营收 Revenue 123 a 中")
    assert "营收" in toks and "revenue" in toks and "123" in toks
    assert "a" not in toks  # 单字符丢弃


def test_bm25_ranks_relevant_doc_first():
    docs = {
        1: "营收 revenue 收入 销售额 订单 金额 净额",      # 高度相关
        2: "客户 customer 用户 留存 活跃 复购",            # 无关
        3: "营收 的 定义 见 客户 手册 附录",                # 部分相关
    }
    scores = _bm25_scores("营收 定义", docs)
    assert scores, "应有非零得分"
    best = max(scores, key=scores.get)
    assert best == 1 or best == 3, f"最相关应为 1 或 3，实际 {best}"
    assert scores.get(2, 0) == 0 or scores[2] < scores[1], "无关文档得分应最低"


def test_bm25_empty_query_returns_empty():
    assert _bm25_scores("", {1: "a b"}) == {}


# --------------------------------------------------------------------------- #
# 2. RRF fusion
# --------------------------------------------------------------------------- #
def test_rrf_fuses_two_rankings():
    ranking_a = [10, 20, 30]   # doc 10 最相关
    ranking_b = [20, 10, 40]   # doc 20 最相关
    fused = _rrf_fuse([ranking_a, ranking_b], k=60)
    # doc 10: 1/61 + 1/62; doc 20: 1/61 + 1/62 —— 并列双通道靠前的应排最前
    assert set(fused[:2]) == {10, 20}, fused
    # doc 30 只在一个通道排第 3，doc 40 同理；均应低于 10/20
    assert fused.index(10) < fused.index(30)
    assert fused.index(20) < fused.index(40)


def test_rrf_missing_doc_still_scores_from_single_channel():
    fused = _rrf_fuse([[1], [2]], k=60)
    # 两文档各在一个通道第一：得分相同（1/61），都应保留
    assert set(fused) == {1, 2}


# --------------------------------------------------------------------------- #
# 3. Store-level hybrid search（monkeypatch 嵌入为确定性伪向量）
# --------------------------------------------------------------------------- #
@pytest.fixture
def store_with_fake_vectors(monkeypatch, tmp_path):
    """用确定性伪向量替换 sentence-transformers，离线可测向量通道。"""
    def fake_embed(text: str):
        # "营收" 文档的向量与查询同向；其他文档正交
        if "营收" in text:
            return [1.0, 0.0]
        if "客户" in text:
            return [0.0, 1.0]
        return [0.7, 0.7]

    import app.core.tools.knowledge_tool as kt
    monkeypatch.setattr(kt, "_embed", fake_embed)
    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    store.add("营收是订单金额扣除退款后的净额定义说明", "metric")
    store.add("客户留存率的计算口径与活跃客户定义", "kpi")
    return store


def test_hybrid_search_returns_relevant_doc(store_with_fake_vectors):
    hits = store_with_fake_vectors.search("营收的定义", top_k=2)
    assert hits, "混合检索应有结果"
    assert "营收" in hits[0]["text"], f"top1 应为营收文档，实际: {hits[0]['text'][:30]}"
    # 融合分数字段存在且非负
    assert hits[0]["score"] > 0


def test_hybrid_uses_both_channels_not_either_or(store_with_fake_vectors):
    """旧实现是 '有向量就只算向量、否则只算关键词'——混合实现应两路都算。

    构造：查询与文档 1 向量同向(1,0)，但词面上与文档 2 有 BM25 重叠。
    旧实现（向量可用时）文档 2 得 0 分直接消失；RRF 混合下文档 2 仍应出现。
    """
    hits = store_with_fake_vectors.search("客户 留存 营收 口径", top_k=5)
    texts = [h["text"] for h in hits]
    assert any("营收" in t for t in texts), "向量通道命中应保留"
    assert any("客户" in t for t in texts), "BM25 通道命中也应保留（而非被向量通道挤掉归零）"
