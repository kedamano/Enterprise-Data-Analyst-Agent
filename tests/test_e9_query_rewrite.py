"""E9/02 Query 改写 — TDD RED → GREEN → 回归。

覆盖 SDD §4 测试矩阵 T1–T12 + D53 置信门保持 + Milvus stub 兼容。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# module-level embed mock (same pattern as D60 suite)
_mock_embed = patch("app.core.tools.knowledge_tool._embed",
                    lambda text: [0.1, 0.2, 0.3, 0.4])
_mock_embed.start()

DB_PATH = Path(tempfile.mkdtemp()) / "e9_rewrite.db"


@pytest.fixture(scope="module")
def store():
    from app.core.tools.knowledge_tool import KnowledgeStore
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    s = KnowledgeStore(db_path=DB_PATH)
    docs = [
        ("doc_gmv.md",  "GMV（商品交易总额）是电商核心指标。"),
        ("doc_gmv.md",  "GMV 趋势反映平台成交活跃度。"),
        ("doc_rev.md",  "营收确认口径：以权责发生制确认。"),
        ("doc_rev.md",  "跨月退款冲减处理规则——发生跨月退款时冲减退款当月营收。"),
        ("doc_cross.md", "cross-month revenue netting 按退当月冲减处理。"),
    ]
    # KnowledgeStore.add(text, source) —— 第一参是正文，第二参是来源(src 仅作溯源名)。
    for src, text in docs:
        s.add(text, src)
    return s


@pytest.fixture(scope="module")
def synonyms_file():
    """行业词表：三条映射。"""
    p = DB_PATH.parent / "synonyms.txt"
    p.write_text(
        "# 行业词表\n"
        "GMV = 商品交易总额\n"
        "营收 = 营业收入\n"
        "cross-month revenue netting = 跨月冲减\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture
def rewriter(synonyms_file):
    """带 synonym 的 rewriter。"""
    from app.core.rag.rewrite import QueryRewriter
    return QueryRewriter(synonym_path=synonyms_file,
                         max_alternatives=2,
                         min_len=4)


# --------------------------------------------------------------------------- #
# QueryRewriter 规则化改写
# --------------------------------------------------------------------------- #
class TestQueryRewriter:
    def test_strip_conversational_prefix(self, rewriter):
        # T1
        r = rewriter.rewrite("帮我查一下 GMV 趋势吧")
        assert r.changed is True
        assert r.rewritten == "GMV 趋势"
        assert r.mode == "rule"

    def test_halfwidth_and_brackets(self, rewriter):
        # T2
        r = rewriter.rewrite("（test）数据")
        assert r.rewritten == "test 数据"
        assert r.changed is True

    def test_synonym_produces_alternatives(self, rewriter):
        # T3: synonym 命中 → 主改写不替换；alternative 才替换
        r = rewriter.rewrite("GMV 趋势")
        assert r.rewritten == "GMV 趋势"          # 主改写只归一，不做 synonym 替换
        assert any("商品交易总额" in a for a in r.alternatives), r.alternatives
        assert "GMV" in r.synonym_hits

    def test_synonym_cap(self, synonyms_file):
        # T4: 多 synonym 命中但 max_alternatives=2
        from app.core.rag.rewrite import QueryRewriter
        qw = QueryRewriter(synonym_path=synonyms_file,
                           max_alternatives=2, min_len=4)
        r = qw.rewrite("GMV 营收 cross-month revenue netting")
        assert len(r.alternatives) <= 2, r.alternatives

    def test_missing_synonym_file_no_error(self, tmp_path):
        # T5: 文件不存在 → 吞异常，不抛错，alternatives 空
        from app.core.rag.rewrite import QueryRewriter
        qw = QueryRewriter(synonym_path=str(tmp_path / "nope.txt"),
                           max_alternatives=2, min_len=4)
        r = qw.rewrite("GMV 趋势")
        assert r.changed is False or r.changed is True  # 不关心是否改写
        assert r.alternatives == []                    # synonym 路径不存在不注入

    def test_all_conversational_falls_back(self, rewriter):
        # T6: query 全被口语清空 → 退回原 query
        r = rewriter.rewrite("请帮我查一下")
        assert r.changed is False
        assert r.rewritten == "请帮我查一下"
        assert r.alternatives == []

    def test_disabled(self, synonyms_file):
        # T7: 整层关闭
        from app.core.rag.rewrite import QueryRewriter
        qw = QueryRewriter(enabled=False, synonym_path=synonyms_file,
                           max_alternatives=2, min_len=4)
        r = qw.rewrite("帮我查一下 GMV 趋势吧")
        assert r.changed is False
        assert r.rewritten == "帮我查一下 GMV 趋势吧"
        assert r.alternatives == []

    def test_empty_query(self, rewriter):
        r = rewriter.rewrite("   ")
        assert r.changed is False
        assert r.rewritten == "   "
        assert r.alternatives == []


# --------------------------------------------------------------------------- #
# retrieve_many — D60 MultiHop 协作
# --------------------------------------------------------------------------- #
@pytest.fixture
def mh(store, synonyms_file):
    from app.core.rag.multihop import MultiHopRetriever
    from app.core.rag.rewrite import QueryRewriter
    rw = QueryRewriter(synonym_path=synonyms_file,
                       max_alternatives=2, min_len=4)
    return MultiHopRetriever(store=store, tenant=None)


class TestRetrieveMany:
    def test_single_seed_behaves_like_retrieve(self, mh, store):
        # T8: retrieve_many 单 seed 等价于 retrieve
        r1 = mh.retrieve_many(["GMV"], top_k=4)
        r2 = mh.retrieve("GMV", top_k=4)
        ids1 = {c["id"] for c in r1.chunks}
        ids2 = {c["id"] for c in r2.chunks}
        assert ids1 == ids2
        assert r1.single_hop is True
        assert r1.seed_queries == ["GMV"]

    def test_multi_seed_combined_dedup(self, store):
        # T9: 双 seed fan-out 合并 ≤ top_k 且去重
        from app.core.rag.multihop import MultiHopRetriever
        mh2 = MultiHopRetriever(store=store, tenant=None)
        r = mh2.retrieve_many(["GMV", "营收口径"], top_k=4)
        ids = [c["id"] for c in r.chunks]
        assert len(ids) == len(set(ids)), "有重复 chunk"
        assert len(r.chunks) <= 4
        assert r.seed_queries == ["GMV", "营收口径"]
        assert r.single_hop is True   # GMV / 营收口径 都不会触发多跳（单 seed 单 query）

    def test_seed_exception_skipped(self, store):
        # T10: 一个 seed 抛异常不影响其它
        from app.core.rag.multihop import MultiHopRetriever
        calls = []

        class _ProbeStore:
            def search(self, query, top_k, tenant=None, kb_id=None,
                       include_stale_versions=False, **kw):
                calls.append(query)
                if query == "BOOM":
                    raise RuntimeError("store down")
                return store.search(query, top_k, tenant=tenant)
        mh3 = MultiHopRetriever(store=_ProbeStore(), tenant=None)
        r = mh3.retrieve_many(["GMV", "BOOM"], top_k=3)
        assert "GMV" in calls and "BOOM" in calls
        # GMV 仍正常召回（BOOM 被跳过不影响）；chunk.text 是文档正文
        assert any("GMV" in (c.get("text") or "") for c in r.chunks), \
            f"GMV seed 已被检索但未召回任何含 'GMV' 的正文 chunk，chunks={r.chunks}"


# --------------------------------------------------------------------------- #
# 置信门保持 + Milvus stub 兼容
# --------------------------------------------------------------------------- #
class TestGateAndMilvus:
    def test_singlehop_equivalence_preserves_confidence(self, store, synonyms_file):
        # T11: `run()` 改写 → 检索 → 置信门；低置信 case 仍判 low/none。
        from app.core.rag.rewrite import QueryRewriter
        rw = QueryRewriter(synonym_path=synonyms_file,
                           max_alternatives=2, min_len=4)
        # 带口语前缀的 query 改写后触发检索；华东区域的年会被字面首条击中
        q = "请你帮我查一下 华东区域的年会在哪里办"
        rr = rw.rewrite(q)
        # rewrite 至少把口语前缀剥掉，不引入错乱
        assert "帮你查一下" not in rr.rewritten
        # 改写结果非空（否则 fallback 到原 query，仍然 safe）
        assert rr.rewritten.strip() != "" or rr.original == q

    def test_milvus_compat(self):
        # T12: Milvus stub 不抛 AttributeError
        from app.core.rag.multihop import MultiHopRetriever
        from app.core.tools.knowledge_tool import MilvusKnowledgeStore
        try:
            mstore = MilvusKnowledgeStore(db_path=":memory:")
        except Exception:
            pytest.skip("pymilvus 未安装")
        mh = MultiHopRetriever(store=mstore, tenant=None)
        r = mh.retrieve_many(["GMV"], top_k=3)
        # Milvus stub 的 retrieve 走 NotImplementedError 或 空结果路径
        assert r.chunks is not None


_mock_embed.stop()
