"""D53：低置信检索的 fail-closed 兜底。

Spec: docs/specs/RAG/01-low-confidence-fallback.md

断在哪：`knowledge_search` 无论检索到什么都照单返回，**没有任何置信信息**——
"检索到 0 段"与"检索到 4 段无关内容"在调用方看来一模一样。
而唯一的置信信号 `rerank_score` 早就算好了（`rag/reranker.py`），**没有任何人读它**
（"实现了但从未接线"，与 D42 的 `fit_to_budget` 同一种病）。

本卡的判断：不做提召回（那是 query 改写/多跳），只做**提可信**——
让调用方知道这检索值不值得信，并让低置信的内容**进不了模型上下文**（结构性保证）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

CORPUS = (
    ("口径-营收.md",
     "营收口径：营收 = 支付成功金额 - 退款金额，不含运费与税费。"
     "计算营收时必须使用支付成功时间，未支付订单不计入。"),
    ("口径-转化率.md",
     "转化率口径：转化率 = 支付成功订单数 / 当日访问 UV（去重访客）。"
     "分母为访问 UV，不是 PV，也不是注册用户数。"),
    ("口径-华东区域.md",
     "华东区域定义：包含江苏、浙江、上海、安徽四省市。华南包含广东、福建、广西、海南。"),
    # 这两条是"字面沾边但答不了"的样本：与负例问句共享 bigram「公司」。
    # 必须**至少两条**命中，否则 `rerank_score` 的归一化项（`(score-lo)/span`）在单候选时
    # 恒为 0，"重排打开会虚高"这个场景根本复现不出来——本文件曾被这个漏洞放过一次假绿。
    ("指标-客户数.md",
     "客户数按 customer_id 去重计数；公司内部员工账号与 guest 访客均不计入客户数。"),
    ("业务规则-退款.md",
     "退款在发生退款的月份冲减当月营收，不追溯调整原订单月份；公司内部冲账同样适用。"),
)
#: 黄金集里的负例：语料中没有答案（原样取自 app/eval/rag_golden.py）
POSITIVE_QUERIES = ("营收口径包含退款吗", "转化率的分母到底是什么", "华东包含哪些省份")
NEGATIVE_QUERY = "公司年会在哪天举办"

#: 实测（2026-09-15）：正例首条亲和分 0.188~0.500，负例 0.111/0.000 → 阈值 0.15 两侧干净
EXPECTED_THRESHOLD = 0.15


@pytest.fixture
def bm25_only(monkeypatch):
    """强制纯 BM25：离线、确定性、不加载嵌入模型（本机加载要十几秒）。"""
    from app.core.tools import knowledge_tool

    monkeypatch.setattr(knowledge_tool, "_embed", lambda _t: None)


@pytest.fixture
def seeded_store(tmp_path, monkeypatch, bm25_only):
    """离线可用的知识库（BM25 通道，不需要嵌入模型）。"""
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.tools.knowledge_tool import KnowledgeStore

    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    for source, text in CORPUS:
        store.add(text, source)
    monkeypatch.setattr("app.core.tools.knowledge_tool.get_store", lambda: store)
    return store


def _chunks(store, query, top_k: int = 4):
    return store.search(query, top_k)


# --------------------------------------------------------------------------- #
# 一、置信度口径
# --------------------------------------------------------------------------- #
def test_confidence_reuses_the_single_phrase_affinity():
    """口径只有一个来源：判级分必须复用 `reranker._phrase_affinity`，不许写第二份。

    D42/D47 的老教训：同一件事写两遍，迟早分叉（"已溯源"曾有两个定义）。
    """
    src = (Path(__file__).resolve().parent.parent
           / "app" / "core" / "rag" / "confidence.py").read_text(encoding="utf-8")
    assert "from .reranker import" in src and "_phrase_affinity" in src, \
        "判级分必须复用 reranker 的短语亲和实现"
    assert "def _affinity_tokens" not in src, \
        "不得在 confidence 里重写一份分词/亲和实现（口径会分叉）"


def test_high_confidence_when_content_matches(seeded_store):
    from app.core.rag.confidence import confidence_of

    conf = confidence_of("营收口径包含退款吗", _chunks(seeded_store, "营收口径包含退款吗"))
    assert conf.level == "high", conf
    assert conf.score > 0
    assert conf.basis == "phrase_affinity"


def test_low_confidence_when_content_is_irrelevant(seeded_store):
    """**字面沾边但答不了**：检索非空、亲和分低于阈值 → `low`。

    这是 RAG 最阴的失败模式——调用方看到"有 1 段结果"会以为查到了。
    """
    from app.core.rag.confidence import confidence_of

    chunks = _chunks(seeded_store, NEGATIVE_QUERY)
    assert chunks, "该用例的前提是检索**非空**（否则退化成 no_chunks，测不到这条）"
    conf = confidence_of(NEGATIVE_QUERY, chunks)
    assert conf.level == "low", f"库里没有答案的问句不该判成高置信：{conf}"
    assert conf.score < EXPECTED_THRESHOLD


def test_no_chunks_is_none(seeded_store):
    from app.core.rag.confidence import confidence_of

    conf = confidence_of("随便问问", [])
    assert conf.level == "none"
    assert conf.basis == "no_chunks"
    assert conf.score == 0.0


def test_uncomputable_signal_fails_closed_to_low():
    """**fail-closed**：算不出信号时按 `low` 处理，绝不能默认"高置信"。

    走神了也要往安全的那边倒——这与脱敏"出错必须丢掉行样本"是同一条纪律。
    """
    from app.core.rag.confidence import confidence_of

    # 只有 id/source，既无 rerank_score 也无可用文本 → 无从判断
    conf = confidence_of("营收口径", [{"id": 1, "source": "x.md", "text": ""}])
    assert conf.level == "low", f"算不出信号必须 fail-closed 到 low，实际 {conf}"
    assert conf.basis == "no_signal"


def test_threshold_is_configurable_and_invalid_falls_back(monkeypatch):
    from app.config import get_settings
    from app.core.rag.confidence import confidence_of

    chunks = [{"id": 1, "source": "a.md", "text": "订单支付口径说明：不含退款。",
               "score": 0.5}]     # 与 "营收口径" 只共享一个 bigram（口径）→ 亲和 0.25

    monkeypatch.setenv("RAG_MIN_CONFIDENCE", "0.9")     # 抬到实际亲和分之上 → 变低置信
    get_settings.cache_clear()
    low = confidence_of("营收口径", chunks)
    assert low.level == "low"
    assert 0 < low.score < 0.9, "分数本身不该被阈值改写"

    monkeypatch.setenv("RAG_MIN_CONFIDENCE", "abc")      # 非法值 → 退回默认，不抛
    get_settings.cache_clear()
    assert confidence_of("营收口径", chunks).level == "high"
    get_settings.cache_clear()


def test_level_does_not_change_with_rerank_toggle(seeded_store, monkeypatch):
    """**判级与重排开关无关**——否则"拨一下开关，同一次检索从 high 变 low"。

    实测依据（spec §2.1.1）：`rerank_score = 0.8×亲和 + 0.2×归一`，而**归一化有个地板分**——
    候选不止一条时，字面完全不相干的首条照样能拿到 `0.2×1.0 = 0.2`，越过 0.15 的阈值。
    本用例的前提是负例**命中 ≥2 段**（单候选时 span 恒为 0，复现不出虚高）。
    """
    from app.config import get_settings
    from app.core.rag.confidence import confidence_of

    queries = (*POSITIVE_QUERIES, NEGATIVE_QUERY)

    def levels() -> list[str]:
        return [confidence_of(q, _chunks(seeded_store, q)).level for q in queries]

    monkeypatch.setenv("RERANK_ENABLED", "false")
    get_settings.cache_clear()
    off = levels()

    monkeypatch.setenv("RERANK_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert len(_chunks(seeded_store, NEGATIVE_QUERY)) >= 2, \
            "前提不成立：负例只命中 0/1 段，测不到归一化的地板分"
        raws = _chunks(seeded_store, NEGATIVE_QUERY)
        assert raws[0].get("rerank_score", 0) >= EXPECTED_THRESHOLD, \
            ("前提不成立：重排开启时负例首条分没越过阈值，"
             "说明这个用例此刻抓不住'拿 rerank_score 判级'的写法")
        on = levels()
    finally:
        get_settings.cache_clear()

    assert off == on, f"重排开关改变了判级：关={off} 开={on}"
    assert off[0] == "high" and off[-1] == "low", "前提：这批样本本就该是一高/一低"


def test_retrieval_returns_rerank_score_but_it_is_not_the_basis(seeded_store, monkeypatch):
    """`rerank_score` 仍在 chunk 里透出（不丢信号），**只是不当判据**。"""
    from app.config import get_settings
    from app.core.rag.confidence import confidence_of

    monkeypatch.setenv("RERANK_ENABLED", "true")
    get_settings.cache_clear()
    try:
        chunks = _chunks(seeded_store, "营收口径包含退款吗")
        assert chunks and chunks[0].get("rerank_score") is not None, "重排开启时应有该字段"
        conf = confidence_of("营收口径包含退款吗", chunks)
        assert conf.basis == "phrase_affinity", "判据仍是亲和分，不是 rerank_score"
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 二、工具输出：低置信**不下发内容**
# --------------------------------------------------------------------------- #
def test_tool_returns_confidence_fields(seeded_store):
    from app.core.tools import knowledge_tool

    out = knowledge_tool.run({"query": "营收口径包含退款吗"})
    assert out["ok"] is True
    assert out["confidence"]["level"] == "high"
    assert out["low_confidence"] is False
    assert len(out["chunks"]) >= 1, "高置信时照常返回内容"


def test_tool_drops_chunks_when_low_confidence(seeded_store):
    """**本卡的要害**：低置信时 `chunks` 必须为空——让模型**读不到**，而不是求它别引用。

    复核发现今天**不存在**"知识引用通路"（`sources.append_citations` 只处理数值 evidence），
    所以"在引用环节过滤"没有可挂的钩子；唯一的结构性保证就是**不下发**。
    """
    from app.core.tools import knowledge_tool

    out = knowledge_tool.run({"query": NEGATIVE_QUERY})
    assert out["ok"] is True, "查不到内容不是错误——是**知识库没有**，要说清楚"
    assert out["low_confidence"] is True
    assert out["chunks"] == [], "低置信内容不得进模型上下文（否则只能靠 prompt 纪律）"
    assert out["note"], "必须明说知识缺失，不能让模型以为'查过了、没问题'"


def test_default_threshold_matches_the_measured_calibration():
    """默认阈值必须是**量出来的那一个**，改动它就得重新标定两侧。

    实测分布（spec §2.1.1）：正例首条亲和 0.188~0.500，负例 0.111/0.000。
    这里把"为什么是 0.15"钉在代码里——否则下一个人随手调大调小，两侧标定就白做了。
    """
    from app.config import get_settings

    assert get_settings().rag_min_confidence == pytest.approx(EXPECTED_THRESHOLD)


def test_note_reports_hit_count_but_not_content(seeded_store):
    """`note` 让人知道"差点匹配到几段"，但**不含内容**——人工可排查，模型拿不到。"""
    from app.core.tools import knowledge_tool

    out = knowledge_tool.run({"query": NEGATIVE_QUERY})
    note = out["note"]
    hits = len(seeded_store.search(NEGATIVE_QUERY, 4))
    assert hits >= 1, "前提：该负例确实检索到了内容（否则测的是 none 分支）"
    assert str(hits) in note, f"note 里的命中段数不是实话（实际 {hits} 段）：{note!r}"
    for _, text in CORPUS:
        assert text[:18] not in note, "note 不得夹带知识内容（等于绕开 fail-closed）"


def test_low_confidence_is_counted(seeded_store):
    """可观测：低置信要被计数，且**只计数、不记查询内容**（查询可能含敏感词）。"""
    from app.core.tools import knowledge_tool
    from app.infrastructure.observability.metrics import metrics

    def counted() -> float:
        return metrics.snapshot()["counters"].get("rag_low_confidence_total", 0)

    before = counted()
    knowledge_tool.run({"query": NEGATIVE_QUERY})
    after = counted()
    assert after == before + 1, "低置信没有进 /metrics"

    knowledge_tool.run({"query": "营收口径包含退款吗"})   # 高置信不该计数
    assert counted() == after

    keys = list(metrics.snapshot()["counters"]) + list(metrics.snapshot()["gauges"])
    leaked = [k for k in keys if NEGATIVE_QUERY in k]
    assert not leaked, f"指标名里出现了查询原文：{leaked}"


def test_tool_search_failure_stays_fail_closed(monkeypatch, seeded_store):
    """检索**抛异常**时不得 fail-open 成 `low_confidence=False`。"""
    from app.core.tools import knowledge_tool

    class _Boom:
        def search(self, *a, **kw):
            raise RuntimeError("store down")

    monkeypatch.setattr("app.core.tools.knowledge_tool.get_store", lambda: _Boom())
    out = knowledge_tool.run({"query": "营收口径"})
    assert out["ok"] is False, "出错必须报错，不能伪装成一次成功的检索"
    assert out.get("low_confidence") is not False


# --------------------------------------------------------------------------- #
# 三、报告侧「明说」的接线（spec §2.3）
# --------------------------------------------------------------------------- #
def test_low_confidence_note_reaches_the_analyst_context(seeded_store, monkeypatch):
    """低置信说明必须**真的进到 analyst 的上下文**，否则等于没说。

    只清空 `chunks` 而不把"知识库没查到"送到模型眼前，模型看到的就只是
    "这一步成功了、啥也没有"——它会当作无事发生继续编。
    这里钉住的是**接线**（`nodes.run_analyst` 把整个 tool output 序列化进 payload）：
    一旦有人为了省 token 把空 `chunks` 的结果过滤掉，本用例当场失败。
    """
    import json as _json

    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.state import (
        AgentState, ContextModel, PlanModel, ToolResult,
    )
    from app.core.tools import knowledge_tool

    tool_out = knowledge_tool.run({"query": NEGATIVE_QUERY})
    assert tool_out["low_confidence"] is True and tool_out["note"]

    seen: dict[str, str] = {}

    def fake_llm(stage, user, json_mode=True):     # noqa: ANN001
        seen["message"] = user
        return _json.dumps({"findings": [{"finding": "x", "confidence": 0.5}]},
                           ensure_ascii=False)

    monkeypatch.setattr(nodes, "_llm", fake_llm)
    state = AgentState(session_id="rag_note", user_query=NEGATIVE_QUERY)
    state.context = ContextModel(objective=NEGATIVE_QUERY)
    state.plan = PlanModel(goal="g", steps=[])
    state.tool_results.append(ToolResult(
        step_id="k1", tool="knowledge_search", status="SUCCESS", output=tool_out))

    nodes.run_analyst(state)

    message = seen["message"]
    assert "知识库" in message and "没有知识依据" in message, \
        "低置信说明没进 analyst 上下文——模型只会以为'查过了、没问题'"
    for _, text in CORPUS:
        assert text[:18] not in message, \
            "低置信的内容出现在了模型上下文里（fail-closed 被绕开）"


# --------------------------------------------------------------------------- #
# 四、两侧标定（阈值不许放水，也不许误伤）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("query", POSITIVE_QUERIES)
def test_golden_positive_queries_stay_high_confidence(seeded_store, query):
    """黄金集里**已知能命中**的问句不得被判成低置信——阈值不能把正确的检索误伤掉。"""
    from app.core.rag.confidence import confidence_of

    conf = confidence_of(query, _chunks(seeded_store, query))
    assert conf.level == "high", f"{query!r} 被误判为低置信（阈值过严）：{conf}"


def test_golden_echo_chunk_is_not_high_confidence():
    """D62：首条 = query 的字面复述（"华东区域年会在哪里办？"这类 FAQ 的**问**）→ 不得 high。

    这块字面沾边到亲和分 ≈ 1.0，若只用 D53 的短语亲和判级，就被这款 FAQ 问到
    的段落骗过去；E9/03 专门堵这一种"假高"：拿 query 抄一遍的段落是噪声，不是知识。
    """
    from app.core.rag.confidence import confidence_of

    conf = confidence_of(
        "华东区域的年会在哪里办",
        [{"id": 1, "source": "faq.md",
          "text": "用户问：华东区域的年会在哪里办？"}],
    )
    assert conf.level != "high", f"字面回声被判成高置信（D62 应把它降到 low）：{conf}"
    assert conf.basis == "echo_question"


def test_golden_negative_query_is_not_high_confidence(seeded_store):
    """反向：黄金集里的负例（语料无答案）不得判成高置信——阈值不能放水。"""
    from app.core.rag.confidence import confidence_of

    conf = confidence_of(NEGATIVE_QUERY, _chunks(seeded_store, NEGATIVE_QUERY))
    assert conf.level != "high", f"负例被判成高置信（阈值过松）：{conf}"


def test_calibration_holds_on_the_real_golden_set(tmp_path, monkeypatch, bm25_only):
    """**在 `app/eval/rag_golden.py` 的真身黄金集上再标定一遍**。

    上面用的是为造负例而缩编的 4 段语料；阈值最终要在**评测用的那 6 段语料**
    上站得住，否则"两侧标定"只是在自造样本上自证。
    """
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.rag.confidence import confidence_of
    from app.core.tools.knowledge_tool import KnowledgeStore
    from app.eval.rag_golden import CORPUS as GOLDEN_CORPUS, QUERIES

    store = KnowledgeStore(db_path=tmp_path / "golden.db")
    for source, text in GOLDEN_CORPUS:
        store.add(text, source)

    wrong: list[str] = []
    for query, relevant in QUERIES:
        conf = confidence_of(query, store.search(query, 4))
        if relevant:                       # 正例：不得被误伤
            if conf.level != "high":
                wrong.append(f"正例 {query!r} 判成 {conf.level}（分数 {conf.score}）")
        elif conf.level == "high":         # 负例：不得放水
            wrong.append(f"负例 {query!r} 判成 high（分数 {conf.score}）")
    assert not wrong, "黄金集两侧标定被打破：\n  " + "\n  ".join(wrong)


# --------------------------------------------------------------------------- #
# 五、CE 置信档位（cross_encoder basis + 独立阈值）—— RAG-01 §4
# --------------------------------------------------------------------------- #
@pytest.fixture
def ce_enabled(monkeypatch):
    """模拟 CE 可用：绕开网络加载，直接注入一个可调的假 CE 模型。"""
    from app.config import get_settings

    monkeypatch.setenv("RERANK_CROSS_ENCODER", "fake/bge-reranker")
    get_settings.cache_clear()
    from app.core.rag import reranker as rr

    class _FakeCE:
        def predict(self, pairs):
            return [0.9 if any(w in t for w in ("营收", "退款", "转化")) else 0.1
                    for _, t in pairs]

    rr._ce_model = _FakeCE()
    rr._ce_error = None
    try:
        yield
    finally:
        rr._ce_model = None
        rr._ce_error = None
        get_settings.cache_clear()


def test_ce_high_rerank_score_is_high_confidence(ce_enabled):
    """CE 可用 + 首条 rerank_score >= 阈值 → basis=cross_encoder, level=high。"""
    from app.core.rag.confidence import confidence_of

    chunks = [{"id": 1, "source": "口径-营收.md", "text": "营收 = 支付成功金额 - 退款",
               "rerank_score": 0.85}]
    conf = confidence_of("营收口径包含退款吗", chunks)
    assert conf.basis == "cross_encoder", conf
    assert conf.level == "high", conf
    assert conf.score == 0.85, "分数本身必须如实透出，不得被阈值改写"


def test_ce_low_rerank_score_is_low_confidence(ce_enabled):
    """CE 可用 + 首条 rerank_score < 阈值 → level=low（独立于 phrase_affinity）。"""
    from app.core.rag.confidence import confidence_of

    chunks = [{"id": 1, "source": "无关.md", "text": "公司体育节筹备注意事项",
               "rerank_score": 0.1}]
    conf = confidence_of("随便查一个主题", chunks)
    assert conf.basis == "cross_encoder", conf
    assert conf.level == "low", conf
    assert conf.score == 0.1


def test_ce_echo_chunk_is_still_demoted(ce_enabled):
    """CE 下 echo 首条仍强制 low——覆盖层独立于 basis。"""
    from app.core.rag.confidence import confidence_of

    q = "华东区域的年会在哪里办"
    chunks = [{"id": 1, "source": "faq.md", "text": f"用户问：{q}？",
               "rerank_score": 0.95}]
    conf = confidence_of(q, chunks)
    assert conf.level == "low", conf
    assert conf.basis == "echo_question"


def test_ce_disabled_falls_back_to_phrase_affinity(ce_enabled, monkeypatch):
    """CE 不可用 → confidence_of 退回 phrase_affinity 单一尺度（既有契约）。"""
    from app.config import get_settings
    from app.core.rag import reranker as rr
    from app.core.rag.confidence import confidence_of

    # 中途让 CE 不可用（模拟加载失败/降级）
    rr._ce_error = "boom"
    rr._ce_model = None
    try:
        chunks = [{"id": 1, "source": "口径.md", "text": "营收口径说明：营收",
                   "rerank_score": 0.9}]
        conf = confidence_of("营收口径", chunks)
        assert conf.basis == "phrase_affinity", f"CE 不可用应退 phrase_affinity，实际 {conf}"
    finally:
        rr._ce_error = None
        get_settings.cache_clear()


