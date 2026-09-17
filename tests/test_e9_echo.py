"""D62：字面重合同语义误判 — 首条=问题的复述 → 强制降级 low（basis=echo_question）。

Spec: docs/specs/E9/03-echo-demotion.md

失败模式（本卡要堵的）：query "华东区域的年会在哪里办" 被 KB 里同款 FAQ 首条字面命中，
短语亲和 ≈ 1.0，但那段话只是把 question 重抄了一遍，**不是答案**。D53 只看亲和分，
会判 high → 模型读到拿问题答问题的段落，等于没查。E9/03：在 confidence_of 里加
一条"字面回声"短路 —— 亲和高也得降到 low。

测试形式：纯合成 chunk，构造结构化输入（不争抢 store fixture），保证：
(1) 正/负例可控；(2) 不依赖嵌入向量或真实语料。
"""
from __future__ import annotations

import pytest

from app.core.rag.confidence import (
    Confidence,
    _echo_ratio,
    _echo_strip,
    confidence_of,
)


# --------------------------------------------------------------------------- #
# §一、辅助函数（_echo_strip / _echo_ratio）                                   #
# --------------------------------------------------------------------------- #
class TestEchoStrip:
    def test_keeps_substantive_content(self):
        """只剥句末/头部噪声，保留主句。"""
        assert _echo_strip("营收口径包含退款吗") == "营收口径包含退款"

    def test_strips_trailing_fullwidth_question_mark(self):
        """全角问号也算疑问尾。"""
        assert _echo_strip("华东区域的年会在哪里办？") == "华东区域的年会在哪里办"

    def test_strips_faq_user_prefix(self):
        """KB 里常见的"用户问："头要去掉。"""
        assert _echo_strip("用户问：华东区域的年会在哪里办？") == "华东区域的年会在哪里办"

    def test_strips_english_q_prefix(self):
        """Q: 前缀也要剥。"""
        assert _echo_strip("Q: 华东区域的年会在哪里办") == "华东区域的年会在哪里办"

    def test_empty_after_strip_is_empty(self):
        assert _echo_strip("？？？") == ""


class TestEchoRatio:
    def test_when_chunk_is_exact_echo(self):
        """chunk 与 query 同文 → 1.0。"""
        assert _echo_ratio("华东区域的年会在哪里办",
                           "华东区域的年会在哪里办") == pytest.approx(1.0)

    def test_when_chunk_wraps_query(self):
        """chunk 是 query 的包裹（头/尾多几个字）→ 1 < ratio < ratio_max。"""
        q = "华东区域的年会在哪里办"          # 10 字符
        t = "关于华东区域的年会在哪里办"       # 头追加"关于"→ 12 字符，1.2×
        r = _echo_ratio(q, t)
        assert 1.0 < r < 1.5

    def test_when_chunk_is_real_answer(self):
        """真实答案远比 query 长 → ratio > ratio_max。"""
        q = "华东区域年会举办"  # 10 字符的短 query
        t = ("华东区域年会在上海浦东嘉里大酒店三楼宴会厅举办，"
             "时间是 2026 年 3 月 15 日，由 HR 行政统一组织。")
        assert _echo_ratio(q, t) > 1.5

    def test_empty_query(self):
        assert _echo_ratio("", "华东区域的年会在哪里办") == 0.0


# --------------------------------------------------------------------------- #
# §二、confidence_of —— 字面回声强制降级                                      #
# --------------------------------------------------------------------------- #
ECHO_QUERY = "华东区域的年会在哪里办"


def test_T1_echo_question_mark_chunk_is_demoted_to_low():
    """T1：FAQ 头 + 问号包裹的 query → low，basis=echo_question。"""
    chunk = {"id": 1, "source": "faq.md",
             "text": "用户问：华东区域的年会在哪里办？"}
    conf = confidence_of(ECHO_QUERY, [chunk])
    assert conf.level == "low", conf
    assert conf.basis == "echo_question"
    # 虽然分数仍按公式算（≈1.0），但判级不看分数，强制 low
    assert conf.score > 0.8


def test_T2_exact_echo_is_demoted_to_low():
    """T2：完全同文的 chunk → echo（ratio 1.0 ≤ 1.5）。"""
    chunk = {"id": 1, "source": "faq.md", "text": ECHO_QUERY}
    conf = confidence_of(ECHO_QUERY, [chunk])
    assert conf.level == "low"
    assert conf.basis == "echo_question"


def test_T3_real_answer_not_demoted():
    """T3：chunk 虽是同一主题但 ratio > 1.5（真答案）→ 保留 high。"""
    chunk = {"id": 1, "source": "policy.md",
             "text": ("华东区域年会由 HR 行政统一安排，"
                      "举办地点为上海浦东嘉里大酒店三楼宴会厅，"
                      "时间通常定在每年 3 月中旬。")}
    conf = confidence_of(ECHO_QUERY, [chunk])
    assert conf.level == "high", f"真答案被误判成 echo：{conf}"
    assert conf.basis == "phrase_affinity"


def test_T4_existing_positive_queries_still_high(seeded_store):
    """T4：已在 D53 验证的正例不会因为 D62 被误伤。

    用真实 store.search 检索，复刻标准检索路径；正例必须继续 high。
    """
    from app.core.rag.confidence import confidence_of

    queries = ("营收口径包含退款吗", "转化率的分母到底是什么", "华东包含哪些省份")
    for q in queries:
        chunks = seeded_store.search(q, 4)
        assert chunks, f"正例 {q!r} 检索为空（前提不成立）"
        conf = confidence_of(q, chunks)
        assert conf.level == "high", f"正例 {q!r} 被 D62 误伤：{conf}"


def test_T5_short_query_skips_echo_check():
    """T5：query 极短（< min_chars=8）→ 退避，echo 不介入，仍按亲和分判。"""
    short_q = "营收"       # 2 字符，远 < 8 → 不进场
    chunk = {"id": 1, "source": "a.md", "text": short_q}
    conf = confidence_of(short_q, [chunk])
    # 极短 query 的 echo 检查被 skip → 走 phrase_affinity → score 1.0 → high
    assert conf.level == "high", conf
    assert conf.basis == "phrase_affinity"


def test_T6_dirty_text_with_whitespace_and_quotes_still_demotes():
    """T6：query 被空白/引号包起来仍是 echo → low。"""
    chunk = {"id": 1, "source": "faq.md",
             "text": "  用户问：华东区域的年会在哪里办？  \n"}
    conf = confidence_of(ECHO_QUERY, [chunk])
    assert conf.level == "low"
    assert conf.basis == "echo_question"


def test_T7_disable_echo_demotion_falls_back_to_D53():
    """T7：rag_echo_demotion_enabled=false → 退化回 D53（亲和高 = high）。"""
    chunk = {"id": 1, "source": "faq.md",
             "text": "用户问：华东区域的年会在哪里办？"}
    from app.config import get_settings
    # 用新的 object 替换 Settings，绕过 pydantic-frozen
    import app.config as _cfg
    object.__setattr__(get_settings(), "rag_echo_demotion_enabled", False)
    try:
        conf = confidence_of(ECHO_QUERY, [chunk])
        assert conf.level == "high", f"关闭 echo 后应退化回 D53 的 high：{conf}"
        assert conf.basis == "phrase_affinity"
    finally:
        object.__setattr__(get_settings(), "rag_echo_demotion_enabled", True)


def test_T8_tool_drops_chunks_when_echo_demoted(seeded_store):
    """T8：echo 降级后，tool 端必须 chunks=[] + low_confidence=True + note。

    走 knowledge_tool.run 的真实路径，验证结构性保证（不只是 confidence_of 单体）。
    """
    from unittest.mock import patch
    from app.core.tools import knowledge_tool

    echo_chunk = {"id": 1, "source": "faq.md",
                  "text": "用户问：华东区域的年会在哪里办？"}
    # 让 store.search 返回我们的 echo 块做首条
    with patch.object(seeded_store, "search",
                      return_value=[echo_chunk]):
        with patch("app.core.tools.knowledge_tool.get_store",
                   return_value=seeded_store):
            out = knowledge_tool.run({"query": ECHO_QUERY})
    assert out["ok"] is True
    assert out["low_confidence"] is True
    assert out["chunks"] == [], "echo 降级后 chunks 必须清空（结构性保证）"
    assert out["confidence"]["level"] == "low"
    assert out["confidence"]["basis"] == "echo_question"
    assert out["note"]


# --------------------------------------------------------------------------- #
# §三、metrics                                                                #
# --------------------------------------------------------------------------- #
def test_echo_demotion_is_counted(seeded_store):
    """D62：echo 降级要在 /metrics 里单独计一条，与普通低置信分开。"""
    from unittest.mock import patch
    from app.core.tools import knowledge_tool
    from app.infrastructure.observability.metrics import metrics

    echo_chunk = {"id": 1, "source": "faq.md",
                  "text": "用户问：华东区域的年会在哪里办？"}

    def _snap(key):
        return metrics.snapshot()["counters"].get(key, 0.0)

    before_low = _snap("rag_low_confidence_total")
    before_echo = _snap("rag_echo_demotion_total")
    with patch.object(seeded_store, "search",
                      return_value=[echo_chunk]):
        with patch("app.core.tools.knowledge_tool.get_store",
                   return_value=seeded_store):
            knowledge_tool.run({"query": ECHO_QUERY})
    snap = metrics.snapshot()["counters"]
    assert snap.get("rag_low_confidence_total", 0.0) == before_low + 1, \
        "echo 降级仍是低置信——主计数器也得涨"
    assert snap.get("rag_echo_demotion_total", 0.0) == before_echo + 1, \
        "echo 降级要用单独的 rag_echo_demotion_total 计数"


# --------------------------------------------------------------------------- #
# §四、pytest fixtures（沿用 conftest 的 bm25 隔离；本模块额外提供 seeded_store #
#     让 T4 走真实 search，验证 D62 在**真实检索路径**上无误伤。                #
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded_store(tmp_path, monkeypatch):
    """BM25 通道的 store（不加载嵌入模型）。"""
    monkeypatch.setenv("REDIS_URL", "")
    from app.core.tools import knowledge_tool
    monkeypatch.setattr(knowledge_tool, "_embed", lambda _t: None)
    from app.core.tools.knowledge_tool import KnowledgeStore

    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    CORPUS = (
        ("口径-营收.md",
         "营收口径：营收 = 支付成功金额 - 退款金额，不含运费与税费。"
         "计算营收时必须使用支付成功时间，未支付订单不计入。"),
        ("口径-转化率.md",
         "转化率口径：转化率 = 支付成功订单数 / 当日访问 UV（去重访客）。"
         "分母为访问 UV，不是 PV，也不是注册用户数。"),
        ("口径-华东区域.md",
         "华东区域定义：包含江苏、浙江、上海、安徽四省市。华南包含广东、福建、广西、海南。"),
        ("指标-客户数.md",
         "客户数按 customer_id 去重计数；公司内部员工账号与 guest 访客均不计入客户数。"),
        ("业务规则-退款.md",
         "退款在发生退款的月份冲减当月营收，不追溯调整原订单月份；公司内部冲账同样适用。"),
    )
    for source, text in CORPUS:
        store.add(text, source)
    return store
