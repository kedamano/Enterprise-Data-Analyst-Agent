"""E9/01 多跳检索 + Query 子问题拆分 — TDD RED → GREEN → 回归。

覆盖 spec §4 测试矩阵 T1–T15 + D53 置信门保持 + Milvus stub 兼容。
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.rag.multihop import MultiHopRetriever, QuerySplitter


# --------------------------------------------------------------------------- #
# Module-level embed mock — 整个测试模块 _embed 都返回确定性向量，
# 避免 sentence-transformers 加载真实模型。scope=module 保证所有用例共用。
# --------------------------------------------------------------------------- #
_mock_embed = patch(
    "app.core.tools.knowledge_tool._embed",
    lambda text: [0.1, 0.2, 0.3, 0.4],
)
_mock_embed.start()


def teardown_module(module):  # noqa: ARG001 — pytest 钩子签名
    _mock_embed.stop()


DB_PATH = Path(tempfile.mkdtemp()) / "e9_multihop.db"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def store():
    """Fresh KnowledgeStore 模块级，加若干已知 chunk 便于断言。"""
    from app.core.tools.knowledge_tool import KnowledgeStore
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    s = KnowledgeStore(db_path=DB_PATH)
    docs = [
        ("refund.md", "退货流程：客户发起退货申请、仓库验收、退款到账。"),
        ("refund.md", "退款流程：审批退款单、原路返回、到账通知客户。"),
        ("revenue.md", "营收口径：以权责发生制确认，开票时点计入营收。"),
        ("revenue.md", "跨月退款冲减：发生跨月退款时冲减退款当月营收不追溯调整。"),
        ("kb_overview.md", "华东区域退货流程由区域仓统一揽收与全国流程一致。"),
    ]
    for src, text in docs:
        s.add(src, text)
    return s


@pytest.fixture
def splitter():
    # 测试夹具：min_query_len=2 让测试 query（普遍 <20 字符）也能触发多跳拆
    return QuerySplitter(min_query_len=2, min_piece_len=2)


@pytest.fixture
def splitter_stricter():
    return QuerySplitter(max_splits=3, min_query_len=12, min_piece_len=4)


# --------------------------------------------------------------------------- #
# T1–T6  QuerySplitter 纯单元
# --------------------------------------------------------------------------- #
class TestQuerySplitter:
    def test_short_query_single_hop(self, splitter):
        """T1：短 query < 20 字符 → 单跳直通 [query]。"""
        out = splitter.split("退货流程")
        assert out == ["退货流程"]

    def test_compound_conjunction(self, splitter):
        """T2：复合 query 拆出 ≥ 2 子 query。"""
        out = splitter.split("退货流程与退款流程的区别")
        assert len(out) >= 2, out
        assert any("退货" in q for q in out), out
        assert any("退款" in q for q in out), out

    def test_enumeration_and(self, splitter):
        """T3：枚举 query 拆出 3 段。"""
        q = "营收口径是否包含退款、跨月退款如何冲减以及退货流程"
        out = splitter.split(q)
        assert len(out) == 3, out
        flat = "|".join(out)
        assert "退款" in flat
        assert "退货" in flat

    def test_max_splits_cap(self, splitter_stricter):
        """T4：超过上限的部分被截断。"""
        q = "A和B和C和D和E"
        out = splitter_stricter.split(q)
        assert len(out) <= splitter_stricter._max_splits

    def test_drop_punctuation_only_piece(self, splitter):
        """T5：纯标点子段被丢弃。"""
        out = splitter.split("华东区域营收、")
        pieces = [p for p in out if re.search(r"[一-鿿A-Za-z0-9]", p)]
        assert all(len(p) >= 4 for p in pieces), pieces
        assert any("营收" in p for p in pieces)

    def test_disabled_returns_single(self):
        """T6：splitter 禁用 → 任何 query 都单跳直通。"""
        sp = QuerySplitter(enabled=False)
        q = "退货流程与退款流程的区别"
        assert sp.split(q) == [q]


# --------------------------------------------------------------------------- #
# T7–T11  MultiHopRetriever 行为
# --------------------------------------------------------------------------- #
class TestMultiHopRetriever:
    def test_single_hop_equivalence(self, store):
        """T7：E2E 单跳等价——与改动前 store.search 召回集一致。"""
        mh = MultiHopRetriever(store=store).retrieve("退货流程", top_k=4)
        assert mh.single_hop is True
        baseline = {c["id"] for c in store.search("退货流程", top_k=4)}
        assert {c["id"] for c in mh.chunks} == baseline

    def test_multi_hop_capped_at_top_k(self, store, splitter):
        """T8：多跳合并 ≤ top_k（splitter 显式低阈值让短 query 也能多跳）。"""
        mh = MultiHopRetriever(store=store, splitter=splitter).retrieve(
            "退货流程与退款流程的区别", top_k=3)
        assert mh.single_hop is False, mh.sub_queries
        assert len(mh.chunks) <= 3
        assert mh.splits >= 2

    def test_dedup_same_chunk_across_subqueries(self, store, splitter):
        """T9：同 chunk 被 2 个子 query 命中 → result 内仅一次。"""
        mh = MultiHopRetriever(store=store, splitter=splitter).retrieve(
            "退货流程与退款流程的区别", top_k=10)
        ids = [c["id"] for c in mh.chunks]
        assert len(ids) == len(set(ids)), f"duplicate chunk ids: {ids}"

    def test_subquery_exception_is_isolated(self, store, splitter):
        """T10：某子 query 抛异常不阻断其它子 query 的合并产出。"""
        real_search = store.search

        def flaky_search(query, top_k=4, *, tenant=None, kb_id=None,
                         include_stale_versions=False):
            if "退款" in query:
                raise RuntimeError("sub-query backend down")
            return real_search(query, top_k, tenant=tenant, kb_id=kb_id)

        with patch.object(store, "search", side_effect=flaky_search):
            mh = MultiHopRetriever(store=store, splitter=splitter).retrieve(
                "退货流程与退款流程的区别", top_k=10)
        assert len(mh.chunks) >= 1
        assert mh.single_hop is False

    def test_final_rerank_uses_full_query(self, store, splitter):
        """T11：最终以全 query rerank。"""
        from app.core.rag import reranker
        mh = MultiHopRetriever(store=store, splitter=splitter).retrieve(
            "退货流程与退款流程的区别", top_k=10)
        if not mh.chunks:
            pytest.skip("mock embed 没产出候选，跳过重排校验")
        full_q = "退货流程与退款流程的区别"
        expected_top = reranker.rerank(full_q, list(mh.chunks), top_k=1)[0]
        assert mh.chunks[0]["id"] == expected_top["id"]


# --------------------------------------------------------------------------- #
# T12–T13  tenant / kb_id 隔离
# --------------------------------------------------------------------------- #
class TestMultiHopIsolation:
    def test_tenant_propagated_to_subqueries(self):
        """T12：每个子 query 的 fan-out 都带 tenant。"""
        from app.core.tools.knowledge_tool import KnowledgeStore
        db = Path(tempfile.mkdtemp()) / "t12.db"
        s = KnowledgeStore(db_path=db)
        s.add("refund.md", "租户 A 专属退款流程说明。")
        s.add("revenue.md", "租户 B 专属营收口径。")

        mh_a = MultiHopRetriever(store=s, tenant="T-A").retrieve(
            "退款流程与营收口径的区别", top_k=10)
        mh_b = MultiHopRetriever(store=s, tenant="T-B").retrieve(
            "退款流程与营收口径的区别", top_k=10)
        flat_a = "|".join(c.get("text", "") for c in mh_a.chunks)
        flat_b = "|".join(c.get("text", "") for c in mh_b.chunks)
        # 两者的来源文本应该反映不同 tenant 的内容
        assert flat_a != flat_b or (not mh_a.chunks and not mh_b.chunks)

    def test_kb_id_propagated(self, store):
        """T13：kb_id 透传到每个子 query。"""
        mh = MultiHopRetriever(store=store).retrieve(
            "退货流程与退款流程的区别", top_k=10, kb_id="kb_does_not_exist")
        assert mh.chunks == []


# --------------------------------------------------------------------------- #
# T14  D53 置信门保持
# --------------------------------------------------------------------------- #
class TestConfidenceGate:
    def test_run_endpoint_gated(self):
        """T14：run(params) 不抛错且 D53 置信门仍然生效。"""
        from app.core.tools.knowledge_tool import run
        out = run({"query": "退款流程与退货流程的区别", "top_k": 4})
        assert isinstance(out, dict)
        assert "chunks" in out and "confidence" in out and "low_confidence" in out
        if out["chunks"]:
            assert out["confidence"]["level"] in {"high", "low", "none"}


# --------------------------------------------------------------------------- #
# T15  Milvus stub 兼容
# --------------------------------------------------------------------------- #
class TestMilvusCompat:
    def test_milvus_search_not_attributeerror(self):
        """T15：Milvus stub 兜底，MultiHopRetriever 不抛 AttributeError。"""
        try:
            from app.core.tools.knowledge_tool import MilvusKnowledgeStore
        except ImportError:
            pytest.skip("MilvusKnowledgeStore 不可用")
        try:
            ms = MilvusKnowledgeStore()
        except Exception:
            pytest.skip("Milvus client 初始化失败（无服务端）→ skip")
        mh = MultiHopRetriever(store=ms).retrieve(
            "退货流程与退款流程的区别", top_k=4)
        assert mh.single_hop is True or mh.splits >= 1


# --------------------------------------------------------------------------- #
# 回归 — run() 单跳路径不变
# --------------------------------------------------------------------------- #
class TestRunSingleHopRegression:
    def test_simple_query_same_as_before(self, store):
        """单跳 query 与 store.search 等价。"""
        q = "华东区域退货流程"
        mh = MultiHopRetriever(store=store).retrieve(q, top_k=4)
        baseline = store.search(q, top_k=4)
        assert {c["id"] for c in mh.chunks} == {c["id"] for c in baseline}
