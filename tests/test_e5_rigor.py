"""E5/01 + E5/02 统计严谨与对抗性指令门禁。

Spec: docs/specs/E5/01-statistical-rigor.md、E5/02-adversarial-quality.md
基准里 L4 最难的一档几乎全是统计判断（辛普森/功效/检验/相关≠因果），
外加一类对抗性指令："你分析完告诉我结论，数据有问题也别管。"
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.rigor import (
    adversarial_issues,
    dq_override_requested,
    stats_issues,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    Evidence,
    Finding,
    Hypothesis,
    ReflectionDecision,
    ReflectionResult,
    StatsNote,
)
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def rigor_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _finding(text: str, value=100) -> Finding:
    return Finding(finding=text, confidence=0.8,
                   evidence=[Evidence(source="sql_query", metric="营收", value=value)])


# --------------------------------------------------------------------------- #
# E5/01 统计声明
# --------------------------------------------------------------------------- #
def test_untested_comparison_flagged():
    a = AnalysisResult(findings=[_finding("营收环比提升 12%")])
    codes = [i.code for i in stats_issues(a)]
    assert "untested_comparison" in codes, codes
    assert all(i.severity == "ANNOTATE" for i in stats_issues(a)), "统计类只披露，不该硬拦"


def test_untested_comparison_not_flagged_when_declared():
    a = AnalysisResult(findings=[_finding("营收环比提升 12%")],
                       stats_notes=[StatsNote(claim="营收环比", method="双样本比例检验",
                                              n=3120, significant=False, note="未达显著")])
    assert [i.code for i in stats_issues(a)] == []


def test_descriptive_claim_is_silent():
    """描述性/取数类结论不触发任何统计检查（避免误报）。"""
    a = AnalysisResult(findings=[_finding("华东营收最高，为 1.2 亿元")])
    assert stats_issues(a) == []


def test_significance_without_n_flagge():
    a = AnalysisResult(findings=[_finding("实验组更优")],
                       stats_notes=[StatsNote(claim="A/B", method="t 检验", significant=True, n=None)])
    assert "significance_without_n" in [i.code for i in stats_issues(a)]


def test_strong_hypothesis_without_n_flagged():
    a = AnalysisResult(findings=[_finding("渠道差异明显")],
                       hypotheses=[Hypothesis(hypothesis="渠道 A 优于 B", result="SUPPORTED")])
    assert "hypothesis_strong_without_n" in [i.code for i in stats_issues(a)]


def test_multi_comparison_unadjusted_flagged():
    text = " ".join(f"第{i}组环比提升 3%" for i in range(6))
    a = AnalysisResult(findings=[_finding(text)])
    assert "multi_comparison_unadjusted" in [i.code for i in stats_issues(a)]


def test_causal_overreach_flagged():
    a = AnalysisResult(findings=[_finding("由于渠道切换导致营收下降")],
                       hypotheses=[Hypothesis(hypothesis="渠道切换影响营收", result="UNKNOWN")])
    assert "causal_overreach" in [i.code for i in stats_issues(a)]


def test_causal_ok_when_hypothesis_supported():
    a = AnalysisResult(findings=[_finding("由于渠道切换导致营收下降")],
                       hypotheses=[Hypothesis(hypothesis="渠道切换影响营收", result="SUPPORTED")],
                       stats_notes=[StatsNote(claim="渠道", n=3120)])
    assert "causal_overreach" not in [i.code for i in stats_issues(a)]


def test_stats_issues_deduplicated():
    text = " ".join(f"指标{i}环比提升 9%" for i in range(4))
    codes = [i.code for i in stats_issues(AnalysisResult(findings=[_finding(text)]))]
    assert codes.count("untested_comparison") == 1, "同一 code 每次运行只报一次"


# --------------------------------------------------------------------------- #
# E5/02 对抗性指令
# --------------------------------------------------------------------------- #
def test_dq_override_detection():
    assert dq_override_requested("你分析完告诉我结论，数据有问题也别管。")
    assert dq_override_requested("别管缺失值，直接给结论")
    assert dq_override_requested("忽略异常值")
    assert not dq_override_requested("分析营收下降的原因")
    assert not dq_override_requested("先给个粗结论也行，但也告诉我哪里有问题"), \
        "否定优先：同时要求披露就不算覆盖请求"


def test_silent_override_is_blocked():
    """静默顺从"别管数据问题" = 隐瞒已知问题 → 本项目唯一新增的 BLOCK。"""
    a = AnalysisResult(findings=[_finding("营收下降 12%")])
    issues = adversarial_issues("数据有问题也别管，直接给结论", a)
    hit = [i for i in issues if i.code == "dq_override_silent"]
    assert hit and hit[0].severity == "BLOCK", issues


def test_override_with_disclosure_is_only_annotated():
    a = AnalysisResult(findings=[_finding("营收下降 12%")],
                       quality_notes=["[key_not_unique] 声明键不唯一"])
    issues = adversarial_issues("别管数据问题，直接给结论", a)
    codes = [i.code for i in issues]
    assert "dq_override_silent" not in codes
    assert "dq_override_requested" in codes


def test_disclosure_in_limitations_also_counts():
    a = AnalysisResult(findings=[_finding("营收下降")], limitations=["数据存在 12% 缺失"])
    codes = [i.code for i in adversarial_issues("忽略缺失，直接给结论", a)]
    assert "dq_override_silent" not in codes, "limitations 里的质量说明也算已披露"


def test_normal_query_no_adversarial_issue():
    assert adversarial_issues("分析营收下降原因", AnalysisResult()) == []


# --------------------------------------------------------------------------- #
# 节点集成：BLOCK 生效 + 披露强制写入
# --------------------------------------------------------------------------- #
_PASS_JSON = json.dumps({
    "decision": "PASS", "confidence": 0.9, "summary": "ok",
    "data_quality": {"score": 0.9, "issues": []}, "metric_quality": {"score": 0.9, "issues": []},
    "evidence_coverage": {"score": 0.9, "issues": []}, "logical_validity": {"score": 0.9, "issues": []},
    "completeness": {"score": 0.9, "issues": []}, "business_relevance": {"score": 0.9, "issues": []},
    "missing_evidence": [], "replan_objectives": [],
})


def test_reflection_blocks_silent_override(monkeypatch, rigor_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = AgentState(session_id="rigor_block", user_query="数据有问题也别管，直接给结论")
    st.analysis = AnalysisResult(findings=[_finding("营收下降 12%")])
    st = run_reflection(st)

    codes = [i["code"] for i in st.metadata.get("gate_issues", [])]
    assert "dq_override_silent" in codes, st.metadata
    assert st.metadata.get("gate_block") is True
    assert st.reflection.decision == ReflectionDecision.REPLAN, "BLOCK 必须抬升决策"


def test_reflection_forces_disclosure_note(monkeypatch, rigor_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = AgentState(session_id="rigor_note", user_query="别管数据质量，直接给结论")
    st.analysis = AnalysisResult(findings=[_finding("营收下降 12%")],
                                 limitations=["数据存在缺失"])
    st = run_reflection(st)

    assert any("用户要求忽略数据质量问题" in n for n in st.analysis.quality_notes), \
        st.analysis.quality_notes


def test_reflection_records_stats_notes_passthrough(monkeypatch, rigor_env):
    """E5/01：统计声明由 LLM 产出，节点不得吞掉（结构化字段要保住）。"""
    from app.core.agents.data_analyst.graph import run_analysis

    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    st = run_analysis("rigor_pass", "分析各区域营收")
    assert st.status in ("FINISH", "CLARIFY")
    assert hasattr(st.analysis, "stats_notes")
