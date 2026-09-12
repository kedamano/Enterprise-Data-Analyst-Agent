"""E4/03 口径可比性：报告里最致命也最难自查的错误是"口径不可比"。

Spec: docs/specs/E4/03-caliber-comparability.md
基准（docs/测试用例.md）的 Rubric 权重里，口径/分母/结构效应占比最大——
"Q1 营收 1.2 亿" 与 "Q2 营收 1.5 亿" 若口径不同，算出来的环比重就是假的。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.caliber import (
    apply_caliber,
    caliber_check,
    caliber_notes,
    parse_period_days,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    Evidence,
    Finding,
    ReflectionDecision,
    ReflectionResult,
    TimeRange,
    Comparison,
)
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _ctx(tr=None, cmp_period=None, metrics=None) -> ContextModel:
    return ContextModel(metrics=metrics or ["营收"],
                        time_range=TimeRange(start=tr) if tr else TimeRange(),
                        comparison=Comparison(type="同比" if cmp_period else None,
                                              period=cmp_period))


def _finding(text: str, metric="营收", value=1) -> Finding:
    return Finding(finding=text, confidence=0.8,
                   evidence=[Evidence(source="sql_query", metric=metric, value=value)])


# --------------------------------------------------------------------------- #
# 期间解析
# --------------------------------------------------------------------------- #
def test_parse_period_days_matrix():
    assert parse_period_days("近30天") == 30
    assert parse_period_days("近 2 周") == 14
    assert parse_period_days("近3个月") == 90
    assert parse_period_days("本季度") == 90
    assert parse_period_days("上个月") == 30
    assert parse_period_days("去年同期") == 365
    assert parse_period_days("2024年3月") == 31
    assert parse_period_days("随便写的") is None, "解析不出必须给 None（宁缺勿滥）"


# --------------------------------------------------------------------------- #
# ① 期间长度不等
# --------------------------------------------------------------------------- #
def test_period_mismatch_detected():
    check = caliber_check(AnalysisResult(findings=[_finding("营收环比增长 25%")]),
                          _ctx(tr="近30天", cmp_period="本季度"))
    kinds = [i.kind for i in check.issues]
    assert "period_mismatch" in kinds, check
    assert check.comparable is False


def test_same_length_periods_are_comparable():
    check = caliber_check(AnalysisResult(findings=[_finding("营收环比增长 5%")]),
                          _ctx(tr="近30天", cmp_period="上个月"))
    assert [i.kind for i in check.issues] == [], check


def test_unparseable_periods_do_not_raise_false_alarm():
    check = caliber_check(AnalysisResult(findings=[_finding("营收变化")]),
                          _ctx(tr="某些时候", cmp_period="另一些时候"))
    assert check.issues == [], "解析不出期间就不判（宁缺勿滥）"


# --------------------------------------------------------------------------- #
# ② 分母缺失
# --------------------------------------------------------------------------- #
def test_ratio_without_denominator_flagged():
    check = caliber_check(AnalysisResult(findings=[_finding("转化率 6%，环比提升 7%")]),
                          _ctx(tr="近30天", cmp_period="上个月"))
    assert [i.kind for i in check.issues] == ["denominator_missing"]


def test_ratio_with_denominator_passes():
    check = caliber_check(
        AnalysisResult(findings=[_finding("转化率 6%（分母为当日访问 UV，分子为支付成功订单）")]),
        _ctx(tr="近30天", cmp_period="上个月"))
    assert check.issues == []


def test_plain_metric_does_not_trigger_denominator_rule():
    check = caliber_check(AnalysisResult(findings=[_finding("华东营收 1.2 亿元")]),
                          _ctx(tr="近30天", cmp_period="上个月"))
    assert check.issues == []


# --------------------------------------------------------------------------- #
# ③ 迭代口径漂移（E3 联动）
# --------------------------------------------------------------------------- #
def test_iteration_drift_on_cross_caliber_comparison():
    check = caliber_check(AnalysisResult(findings=[_finding("改为 3 月后，营收环比下降 12%")]),
                          _ctx(tr="2024年3月", cmp_period="2024年3月"),
                          iteration={"kind": "date_change"})
    kinds = [i.kind for i in check.issues]
    assert "iteration_drift" in kinds, check


def test_iteration_without_comparison_is_fine():
    check = caliber_check(AnalysisResult(findings=[_finding("3 月营收 500 万")]),
                          _ctx(tr="2024年3月", cmp_period="2024年3月"),
                          iteration={"kind": "date_change"})
    assert [i.kind for i in check.issues] == []


def test_iteration_drift_only_when_caliber_changed():
    """粒度/时间切片之外的增量（如筛选）不算口径漂移。"""
    check = caliber_check(AnalysisResult(findings=[_finding("只看 region 1，营收环比下降 12%")]),
                          _ctx(tr="2024年3月", cmp_period="2024年3月"),
                          iteration={"kind": "filter"})
    assert [i.kind for i in check.issues] == []


# --------------------------------------------------------------------------- #
# 决策施加：只收紧，且 drift 是"结论错误"级
# --------------------------------------------------------------------------- #
def test_apply_caliber_tightens_only_on_iteration_drift():
    drift = caliber_check(AnalysisResult(findings=[_finding("环比下降 12%")]),
                          _ctx(tr="2024年3月", cmp_period="2024年3月"),
                          iteration={"kind": "granularity"})
    period = caliber_check(AnalysisResult(findings=[_finding("环比增长 25%")]),
                           _ctx(tr="近30天", cmp_period="本季度"))

    assert apply_caliber(drift, ReflectionResult(decision=ReflectionDecision.PASS)) \
        == ReflectionDecision.REPLAN
    assert apply_caliber(period, ReflectionResult(decision=ReflectionDecision.PASS)) \
        == ReflectionDecision.PASS, "期间不等只需披露，不该回退"
    assert apply_caliber(period, ReflectionResult(decision=ReflectionDecision.FAIL)) \
        == ReflectionDecision.FAIL


def test_caliber_notes_are_readable():
    check = caliber_check(AnalysisResult(findings=[_finding("转化率 6%")]),
                          _ctx(tr="近30天", cmp_period="上个月"))
    notes = caliber_notes(check)
    assert notes and "分母" in notes[0]


# --------------------------------------------------------------------------- #
# 节点集成：Reflection 写入结构化结果 + 决策收紧
# --------------------------------------------------------------------------- #
_PASS_JSON = json.dumps({
    "decision": "PASS", "confidence": 0.9, "summary": "ok",
    "data_quality": {"score": 0.9, "issues": []}, "metric_quality": {"score": 0.9, "issues": []},
    "evidence_coverage": {"score": 0.9, "issues": []}, "logical_validity": {"score": 0.9, "issues": []},
    "completeness": {"score": 0.9, "issues": []}, "business_relevance": {"score": 0.9, "issues": []},
    "missing_evidence": [], "replan_objectives": [],
})


def test_reflection_records_structured_caliber(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = AgentState(session_id="caliber_node", user_query="转化率怎么样")
    st.context = _ctx(tr="近30天", cmp_period="上个月")
    st.analysis = AnalysisResult(findings=[_finding("转化率 6%，环比提升")])
    st.iteration = {"kind": "date_change"}
    st = run_reflection(st)

    cc = st.reflection.caliber_comparability
    assert cc.issues and cc.comparable is False, cc
    assert st.reflection.decision == ReflectionDecision.REPLAN, "口径漂移必须回退"
    assert any("口径" in n for n in st.analysis.quality_notes), st.analysis.quality_notes


def test_reflection_keeps_pass_when_caliber_clean(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = AgentState(session_id="caliber_clean", user_query="华东营收")
    st.context = _ctx(tr="近30天", cmp_period="上个月")
    st.analysis = AnalysisResult(findings=[_finding("华东营收 1.2 亿元")])
    st = run_reflection(st)

    assert st.reflection.decision == ReflectionDecision.PASS
    assert st.reflection.caliber_comparability.comparable is True
    assert st.reflection.caliber_comparability.issues == []


def test_report_renders_caliber_section():
    from app.core.tools.report_tool import run as report_run

    check = caliber_check(AnalysisResult(findings=[_finding("转化率 6%，环比提升")]),
                          _ctx(tr="近30天", cmp_period="本季度"))
    refl = ReflectionResult(decision=ReflectionDecision.PASS, confidence=0.9)
    refl.caliber_comparability = check

    out = report_run({"analysis": AnalysisResult(findings=[_finding("营收 1 亿")]).model_dump(),
                      "reflection": refl.model_dump(), "objective": "营收分析"})
    assert "口径说明" in out["report"], out["report"][-500:]
    assert "period_mismatch" in out["report"]

    clean = report_run({"analysis": AnalysisResult().model_dump(),
                        "reflection": ReflectionResult().model_dump(), "objective": "x"})
    assert "口径说明" not in clean["report"], "没有口径问题就不出现该段"
