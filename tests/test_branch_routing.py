"""TDD suite for deterministic error-branch routing (spec §23 residue).

The REPLAN loop (LLM-driven) already re-plans on insufficient evidence. What
was missing is the *deterministic* branch the spec asks for:

    Invalid Column / Table → Schema Search → Replan/Retry

Contract under test:

* When a sql_query step fails with a schema error ("no such column/table"),
  the Executor automatically runs ``schema_search`` and retries the original
  step ONCE with rebuilt parameters — no full REPLAN round needed.
* The recovery is bounded: a step is recovered at most once (a second schema
  error fails for good and flows into the normal REPLAN path).
* Recovery results are visible in ``tool_results`` (auditable), and the
  retried execution replaces the failure for downstream nodes.
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.nodes import run_executor
from app.core.agents.data_analyst.state import AgentState, PlanModel, PlanStep, ToolResult
from app.core.tools.errors import is_schema_error
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _state_with_sql_step() -> AgentState:
    """构造"**已发现过 schema**"的单步计划。

    预置一条 `schema_search` 成功结果，是为了让本文件专注 §23 的
    "schema 错误 → 补偿 → 重试"分支。否则执行器的**自动补发现**（E2/02：
    计划漏排 schema_search 时自动补一次）会先插一次 `schema_search`，
    把本文件三条精确序列断言全部打乱——那条行为由
    `tests/test_executor_autodiscover.py` 专门覆盖，两边各测各的。
    """
    state = AgentState(session_id="routing", user_query="分析营收")
    state.context.objective = "分析营收"
    state.context.metrics = ["revenue"]
    state.context.dimensions = ["region"]
    state.plan = PlanModel(goal="g", steps=[
        PlanStep(id="s1", objective="查询营收", action="SQL", tool="sql_query",
                 dependencies=[], expected_output="rows", success_criteria="ok"),
    ])
    state.tool_results.append(ToolResult(
        step_id="s0", tool="schema_search", status="SUCCESS",
        output={"tables": [{"table": "fact_sales",
                            "columns": [{"name": "region"}, {"name": "revenue"}]}]}))
    return state


def test_is_schema_error_detector():
    assert is_schema_error("no such column: revenue_usd")
    assert is_schema_error("(sqlite3.OperationalError) no such table: fact_sale")
    assert is_schema_error('unknown column "x" in "field list"')
    assert not is_schema_error("connection reset by peer")
    assert not is_schema_error("只读模式禁止写操作")


def test_schema_error_triggers_auto_schema_search_and_retry(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    calls: list[tuple[str, str]] = []

    def fake_execute_tool(step_id: str, tool: str, params: dict, session_id: str = "") -> ToolResult:
        calls.append((step_id, tool))
        if tool == "sql_query" and not step_id.endswith("__retry"):
            return ToolResult(step_id=step_id, tool=tool, status="FAILED",
                              error='no such column: "revnue"', error_class="NON_RETRYABLE")
        if tool == "schema_search":
            return ToolResult(step_id=step_id, tool=tool, status="SUCCESS", output={
                "tables": [{"table": "fact_sales",
                            "columns": [{"name": "region"}, {"name": "revenue"}]}]})
        return ToolResult(step_id=step_id, tool=tool, status="SUCCESS", output={"rows": [{"revenue": 1}]})

    monkeypatch.setattr(nodes, "execute_tool", fake_execute_tool)

    state = _state_with_sql_step()
    seed_n = len(state.tool_results)      # 跳过预置的 schema_search
    state = run_executor(state)

    # 调用序列：原步骤失败 → 自动 schema_search 补偿 → 带新 schema 重试
    assert [t for _, t in calls] == ["sql_query", "schema_search", "sql_query"], calls
    # 补偿与重试均入 tool_results（可审计）
    tools = [r.tool for r in state.tool_results[seed_n:]]
    assert tools == ["sql_query", "schema_search", "sql_query"]
    # 重试成功后该步骤最终为 SUCCESS，下游节点看到的是修复后的结果
    assert state.tool_results[-1].status == "SUCCESS"
    assert state.tool_results[-1].step_id == "s1__retry"


def test_recovery_is_bounded_to_one_attempt(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    calls: list[str] = []

    def fake_execute_tool(step_id: str, tool: str, params: dict, session_id: str = "") -> ToolResult:
        calls.append(tool)
        # sql_query 永远失败（schema 错误）——补偿 schema_search 成功也无济于事
        if tool == "sql_query":
            return ToolResult(step_id=step_id, tool=tool, status="FAILED",
                              error="no such table: fact_sales", error_class="NON_RETRYABLE")
        return ToolResult(step_id=step_id, tool=tool, status="SUCCESS", output={
            "tables": [{"table": "other", "columns": []}]})

    monkeypatch.setattr(nodes, "execute_tool", fake_execute_tool)

    state = _state_with_sql_step()
    state = run_executor(state)

    # 只允许一次恢复循环：sql → schema_search → sql(重试)，重试再失败就终止
    assert calls == ["sql_query", "schema_search", "sql_query"], calls
    assert state.tool_results[-1].status == "FAILED"


def test_non_schema_error_does_not_trigger_recovery(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    calls: list[str] = []

    def fake_execute_tool(step_id: str, tool: str, params: dict, session_id: str = "") -> ToolResult:
        calls.append(tool)
        return ToolResult(step_id=step_id, tool=tool, status="FAILED",
                          error="connection reset by peer", error_class="RETRYABLE")

    monkeypatch.setattr(nodes, "execute_tool", fake_execute_tool)

    state = _state_with_sql_step()
    state = run_executor(state)

    # 非 schema 错误：不自动补偿，交给错误处理协议/REPLAN
    assert calls == ["sql_query"], calls
