"""INTERVIEW/01 ⑤ RAG 评估：检索命中率 + 忠实度（八股文 03.9）。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §5
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.eval.rag_eval import (
    evaluate,
    faithfulness,
    hit_at_k,
    mrr,
    number_grounding,
    recall_at_k,
    render_markdown,
    seed_corpus,
    token_coverage,
)
from app.infrastructure.llm.router import reset_llm

DOCS = [
    {"source": "口径-营收.md", "text": "营收 = 支付成功金额 - 退款金额，不含运费。"},
    {"source": "口径-转化率.md", "text": "转化率 = 支付成功订单数 / 当日访问 UV。"},
]


@pytest.fixture
def rag_env(monkeypatch, tmp_path):
    """独立知识库文件：不污染 data/knowledge.db。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    yield tmp_path
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 1. 检索指标
# --------------------------------------------------------------------------- #
def test_hit_and_recall_and_mrr():
    retrieved = [{"source": "口径-转化率.md"}, {"source": "口径-营收.md"}]
    assert hit_at_k(retrieved, ("口径-营收.md",), 2) is True
    assert hit_at_k(retrieved, ("不存在的.md",), 2) is False
    assert hit_at_k(retrieved, ("口径-营收.md",), 1) is False, "top-1 里没有就是没命中"
    assert recall_at_k(retrieved, ("口径-营收.md", "口径-转化率.md"), 2) == 1.0
    assert mrr(retrieved, ("口径-营收.md",)) == 0.5, "第二条命中 → 1/2"
    assert mrr([{"source": "无关.md"}], ("口径-营收.md",)) == 0.0


def test_negative_case_excluded_from_metrics():
    """负例（无相关文档）不该拉低或抬高平均，必须返回 None。"""
    assert hit_at_k([{"source": "x"}], (), 4) is None
    assert recall_at_k([{"source": "x"}], (), 4) is None
    assert mrr([{"source": "x"}], ()) is None


# --------------------------------------------------------------------------- #
# 2. 忠实度
# --------------------------------------------------------------------------- #
def test_number_grounding_detects_fabricated_number():
    grounded = number_grounding("营收 = 支付成功金额 - 退款金额", DOCS)
    assert grounded is None, "没有数字 → 不参与加权"

    ok = number_grounding("营收不含运费，退款在当月冲减", DOCS)
    assert ok is None

    bad = number_grounding("共 9876 种口径变体", DOCS)
    assert bad == 0.0, "文档里没有的数字必须判为未落地"


def test_number_grounding_partial():
    docs = [{"source": "s", "text": "转化率 6%，UV 1000"}]
    assert number_grounding("转化率 6%，UV 1000", docs) == 1.0
    assert number_grounding("转化率 6%，UV 9999", docs) == 0.5


def test_token_coverage_and_faithfulness_drop_on_fabrication():
    honest = faithfulness("营收 = 支付成功金额 - 退款金额，不含运费", DOCS)
    fabricated = faithfulness("营收 = 支付成功金额 - 退款金额，共 9876 种口径变体", DOCS)
    assert honest["faithfulness"] > fabricated["faithfulness"], (honest, fabricated)
    assert fabricated["number_grounding"] == 0.0


def test_faithfulness_without_numbers_uses_token_coverage():
    f = faithfulness("营收 = 支付成功金额 - 退款金额", DOCS)
    assert f["number_grounding"] is None
    assert f["faithfulness"] == f["token_coverage"] > 0.5


# --------------------------------------------------------------------------- #
# 3. 端到端（独立知识库）
# --------------------------------------------------------------------------- #
def test_evaluate_end_to_end_on_golden(rag_env):
    from app.core.tools.knowledge_tool import KnowledgeStore

    store = KnowledgeStore(rag_env / "kb.db")
    assert seed_corpus(store) > 0, "语料必须灌进去"

    report = evaluate(top_k=4, store=store, seed=False)
    assert report["cases"] >= 6
    assert report["hit_rate"] is not None
    # 语料与查询是精心对齐的，命中率必须高（否则说明检索或评估写错了）
    assert report["hit_rate"] >= 0.8, report["details"]
    assert report["mrr"] and report["mrr"] > 0.8
    # 忠实度：含伪造数字的那条会低于其他条
    rows = {r["query"]: r for r in report["details"]}
    lying = rows["营收口径包含退款吗"]
    honest = rows["华东包含哪些省份"]
    assert lying["number_grounding"] == 0.0
    assert lying["faithfulness"] < honest["faithfulness"]


def test_evaluate_reports_empty_store_explicitly(rag_env):
    """空库不得"零分通过"——必须显式报未执行。"""
    from app.core.tools.knowledge_tool import KnowledgeStore

    empty = KnowledgeStore(rag_env / "empty.db")
    report = evaluate(top_k=4, store=empty, seed=False, cases=[])
    assert report["hit_rate"] is None
    assert report["cases"] == 0


def test_negative_query_is_in_golden_and_scores_none(rag_env):
    from app.core.tools.knowledge_tool import KnowledgeStore

    store = KnowledgeStore(rag_env / "kb2.db")
    seed_corpus(store)
    report = evaluate(top_k=4, store=store, seed=False)
    neg = [r for r in report["details"] if r["query"] == "公司年会在哪天举办"]
    assert neg and neg[0]["hit"] is None, "负例不该计入命中率"


def test_render_markdown_has_table_and_note(rag_env):
    from app.core.tools.knowledge_tool import KnowledgeStore

    store = KnowledgeStore(rag_env / "kb3.db")
    seed_corpus(store)
    md = render_markdown(evaluate(top_k=2, store=store, seed=False))
    assert "hit@" in md and "faithfulness" in md and "|" in md
    assert "待真实验证" in md, "忠实度是下界，必须显式标注"
