"""No-progress (identical-REPLAN) loop detection in the graph driver."""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst.graph import _replan_key
from app.core.agents.data_analyst.state import (
    PlanModel,
    AgentState,
    PlanStep,
    ReflectionDimension,
    ReflectionResult,
)


def _refl(objectives: list[str]) -> ReflectionResult:
    d = lambda: ReflectionDimension(score=0.5, issues=[])
    return ReflectionResult(
        decision="REPLAN", confidence=0.5,
        data_quality=d(), metric_quality=d(), evidence_coverage=d(),
        logical_validity=d(), completeness=d(), business_relevance=d(),
        missing_evidence=[], replan_objectives=objectives, summary="x",
    )


def _state_with_plan_tools(tools: list[str]) -> AgentState:
    s = AgentState(session_id="np", user_query="q")
    s.plan = PlanModel(goal="g", steps=[
        PlanStep(id=f"s{i}", objective="o", action="a", tool=t,
                 dependencies=[], expected_output="e", success_criteria="c")
        for i, t in enumerate(tools)
    ])
    return s


def test_replan_key_stable_for_same_plan_and_objectives():
    a = _state_with_plan_tools(["sql_query", "schema_search"])
    a.reflection = _refl(["缺营收数据"])
    b = _state_with_plan_tools(["sql_query", "schema_search"])
    b.reflection = _refl(["缺营收数据"])
    assert _replan_key(a) == _replan_key(b)

    c = _state_with_plan_tools(["sql_query", "schema_search"])
    c.reflection = _refl(["换维度下钻"])  # 目标变了
    assert _replan_key(a) != _replan_key(c)


def test_identical_replan_loop_is_cut_short(monkeypatch):
    """连续同计划+同目标 REPLAN：不再空转到 max 步数，提前 FAIL 并给无进展信息。"""
    import app.core.agents.data_analyst.graph as g

    def _ctx(s):
        return s

    def _plan(s):
        s.status = "PLAN"
        s.plan = PlanModel(goal="g", steps=[
            PlanStep(id="s1", objective="o", action="a", tool="sql_query",
                     dependencies=[], expected_output="e", success_criteria="c")])
        s.current_step_index = 0
        return s

    def _exec(s):
        s.current_step_index += 1  # 让步骤循环前进一次即结束
        return s

    def _ana(s):
        s.status = "ANALYZE"
        return s

    calls = {"n": 0}

    def _make_refl():
        d = lambda: ReflectionDimension(score=0.5, issues=[])
        return ReflectionResult(
            decision="REPLAN", confidence=0.5, data_quality=d(),
            metric_quality=d(), evidence_coverage=d(), logical_validity=d(),
            completeness=d(), business_relevance=d(), missing_evidence=[],
            replan_objectives=["缺营收数据"], summary="x")

    def _refl(s):
        calls["n"] += 1
        s.replan_count += 1
        s.reflection = _make_refl()
        s.status = "REPLAN"
        return s

    def _rep(s):
        return s

    monkeypatch.setattr(g, "run_context", _ctx)
    monkeypatch.setattr(g, "run_planner", _plan)
    monkeypatch.setattr(g, "run_executor", _exec)
    monkeypatch.setattr(g, "run_executor_all", _exec)
    monkeypatch.setattr(g, "run_analyst", _ana)
    monkeypatch.setattr(g, "run_reflection", _refl)
    monkeypatch.setattr(g, "run_reporter", _rep)

    state = g.run_analysis("np_stall", "分析营收下滑")
    assert "无进展" in (state.error or ""), state.error
    assert calls["n"] <= _REPLAN_MAX_STALL_LIMIT()


def _REPLAN_MAX_STALL_LIMIT():
    from app.core.agents.data_analyst.graph import _REPLAN_MAX_STALL
    return _REPLAN_MAX_STALL + 1
