"""E2 executor wiring: planner-authored freeform step (input.sql) executes safely."""
from __future__ import annotations

from app.core.agents.data_analyst.nodes import build_executor_params
from app.core.agents.data_analyst.sources import resolve_sql_source
from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanStep,
    ToolResult,
)
from app.core.tools import REGISTRY, execute_tool


def _step(sql: str) -> PlanStep:
    return PlanStep(id="ff1", objective="自定义下钻", action="执行自定义SQL",
                    tool="freeform", input={"sql": sql})


def test_freeform_registered():
    assert "freeform" in REGISTRY


def test_build_params_takes_model_author_sql():
    st = AgentState(session_id="x", user_query="q")
    st.context = ContextModel(objective="o")
    params = build_executor_params(st, _step("SELECT 1"))
    assert params["sql"] == "SELECT 1"


def test_execute_freeform_step_returns_rows_and_is_sql_source():
    res = execute_tool("ff1", "freeform",
                       {"sql": "SELECT r.region_name, SUM(f.revenue) r FROM fact_sales f "
                               "JOIN dim_region r ON f.region_id=r.region_id GROUP BY 1 ORDER BY 2 DESC LIMIT 3"},
                       "e2exe")
    assert res.status == "SUCCESS", res.error
    assert res.output.get("rows"), "自由 SQL 应返回行"
    assert resolve_sql_source("ff1", [res]) is not None, "freeform 步骤应可作溯源源"


def test_execute_freeform_step_rejects_dml():
    res = execute_tool("ff1", "freeform", {"sql": "DELETE FROM fact_sales"}, "e2exe")
    assert res.status == "FAILED"
    assert res.error and "只读" in res.error


def test_step_keeps_negative_input_fail():
    """input 缺失/空 SQL → 守卫返回空错误而非崩溃。"""
    res = execute_tool("ff1", "freeform", {"sql": "   "}, "e2exe")
    assert res.status == "FAILED" and res.error
