"""Self-Consistency / 多次采样投票 -- check_consistency + sample_analyst + graph 集成。

6 cases：
  1. 3 份完全一致 → consensus_ratio=1.0, low_consistency=False。
  2. 3 份完全不同 → consensus_ratio=0.0, low_consistency=True。
  3. 3 份 2/3 一致 → 0 < ratio < 1。
  4. self_consistency_enabled=False → run_analyst 仅被调用 1 次。
  5. use_mock_llm 模式 → 跳过采样（run_analyst 仅被调用 1 次）。
  6. 低一致性 → metadata["self_consistency"]["low_consistency"] == True。
"""
from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    Finding,
    PlanModel,
    PlanStep,
    ReflectionDimension,
    ReflectionResult,
)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _make_refl(decision: str = "REPLAN", objectives: list[str] | None = None) -> ReflectionResult:
    d = lambda: ReflectionDimension(score=0.5, issues=[])
    return ReflectionResult(
        decision=decision, confidence=0.5,
        data_quality=d(), metric_quality=d(), evidence_coverage=d(),
        logical_validity=d(), completeness=d(), business_relevance=d(),
        missing_evidence=[], replan_objectives=objectives or [], summary="x",
    )


def _make_state_with_findings(metric_texts: list[str]) -> AgentState:
    """构造一个 state，.analysis.findings 用 metric_texts 填充。"""
    state = AgentState(session_id="sc", user_query="test")
    state.analysis = AnalysisResult(
        findings=[Finding(finding=t) for t in metric_texts],
    )
    return state


# --------------------------------------------------------------------------- #
# Case 1-3：纯 check_consistency
# --------------------------------------------------------------------------- #


def test_consistency_three_identical_reports():
    """3 份完全一致 → consensus_ratio=1.0。"""
    reports = [
        "营收 1234.5 万元，利润 998 万元，增长率 12.5%。",
        "营收 1234.5 万元，利润 998 万元，增长率 12.5%。",
        "营收 1234.5 万元，利润 998 万元，增长率 12.5%。",
    ]
    from app.core.agents.data_analyst.self_consistency import check_consistency
    result = check_consistency(reports, consensus_threshold=0.6)
    assert result.consensus_ratio == 1.0
    assert len(result.consensus_metrics) > 0
    assert len(result.disagreement_metrics) == 0


def test_consistency_three_different_reports():
    """3 份完全不同 → consensus_ratio=0.0, low_consistency。"""
    reports = [
        "营收 100 万元，客户 50 个。",
        "订单 999 笔，退货率 0.5%。",
        "库存 3000 件，周转率 8.0 次。",
    ]
    from app.core.agents.data_analyst.self_consistency import check_consistency
    result = check_consistency(reports, consensus_threshold=0.6)
    assert result.consensus_ratio == 0.0
    assert result.consensus_ratio < 0.5  # low_consistency


def test_consistency_two_of_three_match():
    """3 份 2/3 一致 → 0 < ratio < 1。"""
    reports = [
        "营收 500 万元，利润 200 万元。",
        "营收 500 万元，利润 999 万元。",  # 营收一致，利润不同
        "营收 500 万元，成本 50 万元。",   # 营收一致，新增成本
    ]
    from app.core.agents.data_analyst.self_consistency import check_consistency
    result = check_consistency(reports, consensus_threshold=0.6)
    assert 0.0 < result.consensus_ratio < 1.0
    # 共识指标里应该有「营收 500」
    consensus_names = [m.metric_name for m in result.consensus_metrics]
    assert any("营收" in n for n in consensus_names)


# --------------------------------------------------------------------------- #
# Case 4-6：graph 集成
# --------------------------------------------------------------------------- #


def _graph_mocks(monkeypatch, *, call_counters: dict):
    """为 _drive_sync 注入最小 mock 使其走到 analyst 阶段。"""
    import app.core.agents.data_analyst.graph as g

    def _ctx(s):
        return s

    def _resolve_route(s):
        return s

    def _try_iteration(s):
        return None

    def _should_supervisor_proxy(s):
        return False

    def _check_budget(s, estimated=5000):
        pass

    def _plan(s):
        s.status = "PLAN"
        s.plan = PlanModel(goal="g", steps=[
            PlanStep(id="s1", objective="o", action="a", tool="sql_query",
                     dependencies=[], expected_output="", success_criteria="")])
        s.current_step_index = 0
        return s

    def _exec(s):
        s.current_step_index += 1
        return s

    def _ana(s):
        call_counters["graph_analyst"] += 1
        s.status = "ANALYZE"
        return s

    def _refl(s):
        call_counters["reflection"] += 1
        s.replan_count += 1
        # decision 字段只接受 PASS/REPLAN/FAIL；实际路由看 state.status
        s.reflection = _make_refl(decision="PASS")
        s.status = "REPORT"
        return s

    def _rep(s):
        s.status = "FINISH"
        return s

    monkeypatch.setattr(g, "run_context", _ctx)
    monkeypatch.setattr(g, "_resolve_route", _resolve_route)
    monkeypatch.setattr(g, "_try_iteration", _try_iteration)
    monkeypatch.setattr(g, "_should_supervisor_proxy", _should_supervisor_proxy)
    monkeypatch.setattr(g, "_check_budget", _check_budget)
    monkeypatch.setattr(g, "run_planner", _plan)
    monkeypatch.setattr(g, "run_executor", _exec)
    monkeypatch.setattr(g, "run_executor_all", _exec)
    monkeypatch.setattr(g, "run_analyst", _ana)
    monkeypatch.setattr(g, "run_reflection", _refl)
    monkeypatch.setattr(g, "run_reporter", _rep)
    # 关闭上下文压缩分支（_compress_context 是 _drive_sync 的局部 import，无需 mock）
    settings = get_settings()
    monkeypatch.setattr(settings, "context_compression_enabled", False, raising=False)


def _patch_nodes_analyst(call_counters: dict):
    """返回一个上下文管理器，把 nodes.run_analyst 替换为按调用序号返回不同 analysis 的 mock。"""
    import app.core.agents.data_analyst.self_consistency as sc
    import app.core.agents.data_analyst.nodes as nodes_module

    lock = threading.Lock()
    counter = [0]

    def _mock_run_analyst(state):
        with lock:
            counter[0] += 1
            idx = counter[0]
        call_counters["nodes_analyst"] += 1
        state.status = "ANALYZE"
        # 返回带 analysis 的 state：前两次用相同 metrics，第三次用不同
        if idx == 1:
            state.analysis = AnalysisResult(findings=[
                Finding(finding="营收 500 万元"),
                Finding(finding="利润 200 万元"),
            ])
        elif idx == 2:
            state.analysis = AnalysisResult(findings=[
                Finding(finding="营收 500 万元"),
                Finding(finding="利润 200 万元"),
            ])
        else:
            state.analysis = AnalysisResult(findings=[
                Finding(finding="营收 500 万元"),
                Finding(finding="利润 999 万元"),
            ])
        return state

    return patch.object(nodes_module, "run_analyst", _mock_run_analyst)


def test_self_consistency_disabled_calls_analyst_once(monkeypatch):
    """self_consistency_enabled=False → run_analyst 仅被调用 1 次。"""
    import app.core.agents.data_analyst.graph as g

    counters = {"graph_analyst": 0, "nodes_analyst": 0, "reflection": 0}
    settings = get_settings()
    monkeypatch.setattr(settings, "self_consistency_enabled", False, raising=False)
    _graph_mocks(monkeypatch, call_counters=counters)

    state = g._drive_sync(AgentState(session_id="sc_off", user_query="q"))
    assert counters["graph_analyst"] == 1
    assert state.status in ("FINISH", "REPORT")


def test_mock_llm_mode_skips_sampling(monkeypatch):
    """use_mock_llm 模式 → 跳过采样（run_analyst 仅被调用 1 次）。"""
    import app.core.agents.data_analyst.graph as g

    counters = {"graph_analyst": 0, "nodes_analyst": 0, "reflection": 0}
    settings = get_settings()
    # 同时打开 self_consistency 但设 use_mock_llm = True
    monkeypatch.setattr(settings, "self_consistency_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mock_llm", True, raising=False)
    assert settings.use_mock_llm is True
    _graph_mocks(monkeypatch, call_counters=counters)

    state = g._drive_sync(AgentState(session_id="sc_mock", user_query="q"))
    assert counters["graph_analyst"] == 1
    assert state.status in ("FINISH", "REPORT")


def test_low_consistency_flag_in_metadata(monkeypatch):
    """低一致性 → metadata['self_consistency']['low_consistency'] == True。"""
    import app.core.agents.data_analyst.graph as g
    from app.core.agents.data_analyst.self_consistency import (
        ConsistencyResult,
        SampleBundle,
        ConsistencyMetric,
    )

    counters = {"graph_analyst": 0, "nodes_analyst": 0, "reflection": 0}
    settings = get_settings()
    monkeypatch.setattr(settings, "self_consistency_enabled", True, raising=False)
    monkeypatch.setattr(settings, "mock_llm", False, raising=False)
    assert settings.use_mock_llm is False
    _graph_mocks(monkeypatch, call_counters=counters)

    # 构造低一致性 SampleBundle
    low_consensus = ConsistencyResult(
        consensus_metrics=[],
        disagreement_metrics=[ConsistencyMetric(metric_name="营收", value="500", count=1)],
        consensus_ratio=0.0,
        all_reports=["a", "b", "c"],
    )
    bundle = SampleBundle(
        primary_report="report text",
        all_reports=["a", "b", "c"],
        consensus=low_consensus,
    )

    async def _fake_sample(state, n=3):
        return bundle

    monkeypatch.setattr(g, "sample_analyst", _fake_sample)

    state = g._drive_sync(AgentState(session_id="sc_low", user_query="q"))
    sc_meta = state.metadata.get("self_consistency", {})
    assert sc_meta.get("low_consistency") is True
    assert sc_meta.get("consensus_ratio") == 0.0
    assert state.status in ("FINISH", "REPORT")
