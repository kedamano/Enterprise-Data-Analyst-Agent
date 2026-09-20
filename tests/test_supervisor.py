"""Tests for the Multi-Agent Supervisor (sub-task parallel analysis)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root on path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanModel,
    PlanStep,
)
from app.core.agents.data_analyst.supervisor import (
    SubTask,
    _should_supervisor,
    plan_subtasks,
    run_supervisor,
    _topological_bands,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _make_state(
    n_steps: int = 3,
    dimensions: list[str] | None = None,
    metrics: list[str] | None = None,
    force_full_rerun: bool = False,
    user_query: str = "对比华北 vs 华南 过去3个月的 营收、客单价、转化率，解释差异原因",
) -> AgentState:
    """Build a minimal AgentState for supervisor tests."""
    dims = dimensions or ["华北", "华南"]
    mets = metrics or ["营收", "客单价"]
    steps = [
        PlanStep(
            id=f"step_{i}",
            objective=f"query metric {i}",
            action="sql_query",
            tool="sql_query",
        )
        for i in range(n_steps)
    ]
    return AgentState(
        session_id="test-session-001",
        user_query=user_query,
        context=ContextModel(
            objective=user_query,
            metrics=mets,
            dimensions=dims,
        ),
        plan=PlanModel(goal=user_query, steps=steps),
        mode="full",
        force_full_rerun=force_full_rerun,
    )


# --------------------------------------------------------------------------- #
# test_plan_subtasks_complex_query
# --------------------------------------------------------------------------- #
def test_plan_subtasks_complex_query():
    state = _make_state(n_steps=3, dimensions=["华北", "华南"], metrics=["营收", "客单价"])
    result = plan_subtasks(state, max_subtasks=4)
    assert len(result) >= 2, f"Expected >= 2 sub-tasks, got {len(result)}"
    for sub in result:
        assert isinstance(sub, SubTask)
        assert sub.sub_id.startswith("sub_")
        # query must be a self-contained description
        assert len(sub.query) > 10
        # each should carry its dimension + metric
        assert len(sub.metrics) >= 1
        assert len(sub.dimensions) >= 1


# --------------------------------------------------------------------------- #
# test_plan_subtasks_simple_query
# --------------------------------------------------------------------------- #
def test_plan_subtasks_simple_query():
    state = _make_state(
        n_steps=1,
        dimensions=["华北"],
        metrics=["营收"],
        user_query="查询华北地区上月营收",
    )
    result = plan_subtasks(state, max_subtasks=4)
    assert len(result) == 1
    assert result[0].sub_id == "sub_0"
    assert result[0].title == "综合分析"


# --------------------------------------------------------------------------- #
# test_run_supervisor_fallback_on_single_subtask
# --------------------------------------------------------------------------- #
def test_run_supervisor_fallback_on_single_subtask():
    """When plan_subtasks returns 1 item, run_supervisor returns parent_state."""
    state = _make_state(
        n_steps=1, dimensions=["华北"], metrics=["营收"],
    )
    # Patch plan_subtasks to return single item (already does for simple queries)
    result = run_supervisor(state)
    # Should be the exact same object (fallback)
    assert result is state


# --------------------------------------------------------------------------- #
# test_run_supervisor_parses_mock_tools
# --------------------------------------------------------------------------- #
def test_run_supervisor_parses_mock_tools():
    """2 sub-tasks, mock their pipelines so each produces a distinct report."""
    state = _make_state(n_steps=3, dimensions=["华北", "华南"], metrics=["营收"])

    sub0_state = AgentState(
        session_id="sub-0", user_query="华北营收",
        status="FINISH",
        report="华北区域营收分析结论：Q3环比增长12%。",
    )
    sub1_state = AgentState(
        session_id="sub-1", user_query="华南营收",
        status="FINISH",
        report="华南区域营收分析结论：Q3环比下降3%。",
    )

    subtasks = [
        SubTask(
            sub_id="sub_0", title="华北营收分析",
            query="分析华北营收", objective="华北营收分析",
            metrics=["营收"], dimensions=["华北"],
        ),
        SubTask(
            sub_id="sub_1", title="华南营收分析",
            query="分析华南营收", objective="华南营收分析",
            metrics=["营收"], dimensions=["华南"],
        ),
    ]

    call_count = {"n": 0}

    def fake_pipeline(parent, subtask):
        call_count["n"] += 1
        if subtask.sub_id == "sub_0":
            return sub0_state
        return sub1_state

    with patch(
        "app.core.agents.data_analyst.supervisor.plan_subtasks",
        return_value=subtasks,
    ), patch(
        "app.core.agents.data_analyst.supervisor._run_subtask_pipeline",
        side_effect=fake_pipeline,
    ):
        result = run_supervisor(state)

    assert result.status == "FINISH"
    # master_report should contain both sub-titles
    assert "华北营收分析" in result.report
    assert "华南营收分析" in result.report
    # Metadata should report 2 sub-tasks, 1 parallel band
    meta = result.metadata.get("supervisor", {})
    assert meta["total_subtasks"] == 2
    assert meta["subtask_ids"] == ["sub_0", "sub_1"]
    assert call_count["n"] == 2


# --------------------------------------------------------------------------- #
# test_supervisor_error_isolation
# --------------------------------------------------------------------------- #
def test_supervisor_error_isolation():
    """3 sub-tasks, 1 raises ValueError -> error annotated, others merged, status=FINISH."""
    state = _make_state(
        n_steps=3, dimensions=["华北", "华南", "华东"], metrics=["营收"],
    )

    def fake_pipeline(parent, subtask):
        if subtask.sub_id == "sub_1":
            raise ValueError("mock tool failure")
        return AgentState(
            session_id=subtask.sub_id,
            user_query=subtask.query,
            status="FINISH",
            report=f"子任务 {subtask.sub_id} ({subtask.title}) 完成。",
        )

    subtasks = [
        SubTask(
            sub_id=f"sub_{i}", title=f"分析{i}",
            query=f"query {i}", objective=f"obj {i}",
            metrics=["营收"], dimensions=[dim],
        )
        for i, dim in enumerate(["华北", "华南", "华东"])
    ]

    with patch(
        "app.core.agents.data_analyst.supervisor.plan_subtasks",
        return_value=subtasks,
    ), patch(
        "app.core.agents.data_analyst.supervisor._run_subtask_pipeline",
        side_effect=fake_pipeline,
    ):
        result = run_supervisor(state)

    assert result.status == "FINISH"
    # Error should be noted in report
    assert "失败" in result.report or "ERROR" in result.report or "sub_1" in result.report
    # Other reports should be present
    assert "sub_0" in result.report
    assert "sub_2" in result.report
    # Metadata records the error
    meta = result.metadata.get("supervisor", {})
    assert "errors" in meta
    assert "sub_1" in meta["errors"]


# --------------------------------------------------------------------------- #
# test_should_supervisor_heuristic (5 assertions)
# --------------------------------------------------------------------------- #
def test_should_supervisor_heuristic():
    """Cover True / False branches of _should_supervisor."""
    # Case 1: complex query -> True
    complex_state = _make_state(n_steps=3, dimensions=["华北", "华南"], metrics=["营收", "客单价"])
    with patch("app.core.agents.data_analyst.supervisor.get_settings") as mock_cfg:
        mock_cfg.return_value.supervisor_enabled = True
        assert _should_supervisor(complex_state) is True

    # Case 2: simple query (1 step, 1 dim, 1 metric) -> False
    simple_state = _make_state(n_steps=1, dimensions=["华北"], metrics=["营收"])
    with patch("app.core.agents.data_analyst.supervisor.get_settings") as mock_cfg:
        mock_cfg.return_value.supervisor_enabled = True
        assert _should_supervisor(simple_state) is False

    # Case 3: supervisor_enabled=False -> False
    with patch("app.core.agents.data_analyst.supervisor.get_settings") as mock_cfg:
        mock_cfg.return_value.supervisor_enabled = False
        assert _should_supervisor(complex_state) is False

    # Case 4: force_full_rerun=True -> False
    force_state = _make_state(
        n_steps=3, dimensions=["华北", "华南"], metrics=["营收", "客单价"],
        force_full_rerun=True,
    )
    with patch("app.core.agents.data_analyst.supervisor.get_settings") as mock_cfg:
        mock_cfg.return_value.supervisor_enabled = True
        assert _should_supervisor(force_state) is False

    # Case 5: 3 steps but cross-product too small -> False
    small_cross = _make_state(n_steps=3, dimensions=["华北"], metrics=["营收"])
    with patch("app.core.agents.data_analyst.supervisor.get_settings") as mock_cfg:
        mock_cfg.return_value.supervisor_enabled = True
        assert _should_supervisor(small_cross) is False
