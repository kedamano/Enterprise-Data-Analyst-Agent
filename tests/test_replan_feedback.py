"""TDD suite for the REPLAN feedback loop (error re-injection, DeepAnalyze-style).

Contract under test:

* When the agent re-plans (``replan_count > 0`` with a Reflection result),
  ``run_planner`` must feed the *why* back to the LLM:
  - ``reflection.replan_objectives`` / ``missing_evidence``
  - the errors of failed tool executions (tool, error, error_class)
  so the new plan addresses the gap instead of repeating it.
* On the first plan (no reflection yet) the payload carries no feedback —
  identical behaviour to before.
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.nodes import run_planner
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ReflectionResult,
    ToolResult,
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


def _state_with_reflection_feedback() -> AgentState:
    state = AgentState(session_id="replan_fb", user_query="分析华北营收下滑原因")
    state.context.objective = "分析华北营收下滑原因"
    state.context.metrics = ["revenue"]
    state.tool_results = [
        ToolResult(step_id="s1", tool="sql_query", status="FAILED",
                   error="no such column: revenue_usd", error_class="NON_RETRYABLE"),
        ToolResult(step_id="s2", tool="schema_search", status="SUCCESS", output={}),
    ]
    state.analysis = AnalysisResult()
    state.reflection = ReflectionResult.model_validate({
        "decision": "REPLAN", "confidence": 0.5,
        "missing_evidence": ["分渠道的营收对比数据"],
        "replan_objectives": ["改用正确的营收字段重新查询"],
        "summary": "证据不足",
    })
    state.replan_count = 1
    return state


def test_replan_feedback_reaches_planner(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    captured: dict[str, str] = {}

    def spy_llm(stage: str, user: str, json_mode: bool = True) -> str:
        captured["stage"] = stage
        captured["user"] = user
        return json.dumps({
            "goal": "重新规划", "steps": [
                {"id": "step_1", "objective": "查 schema", "action": "检索",
                 "tool": "schema_search", "dependencies": [],
                 "expected_output": "字段", "success_criteria": "ok"},
            ],
        })

    monkeypatch.setattr(nodes, "_llm", spy_llm)
    state = _state_with_reflection_feedback()
    state = run_planner(state)

    assert captured["stage"] == "planner"
    payload_text = captured["user"]
    # 反思结论必须回注
    assert "改用正确的营收字段重新查询" in payload_text, "replan_objectives 应回注给 Planner"
    assert "分渠道的营收对比数据" in payload_text, "missing_evidence 应回注给 Planner"
    # 失败工具的错误必须回注（供 Planner 换表/换字段/换工具）
    assert "no such column: revenue_usd" in payload_text, "失败工具错误应回注给 Planner"
    assert "sql_query" in payload_text


def test_first_plan_carries_no_feedback(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    captured: dict[str, str] = {}

    def spy_llm(stage: str, user: str, json_mode: bool = True) -> str:
        captured["user"] = user
        return json.dumps({
            "goal": "初版计划", "steps": [
                {"id": "step_1", "objective": "查 schema", "action": "检索",
                 "tool": "schema_search", "dependencies": [],
                 "expected_output": "字段", "success_criteria": "ok"},
            ],
        })

    monkeypatch.setattr(nodes, "_llm", spy_llm)
    state = AgentState(session_id="replan_first", user_query="分析营收")
    state.context.objective = "分析营收"
    state = run_planner(state)

    assert "replan" not in captured["user"].lower(), "首轮计划不应携带 replan 反馈块"
    assert "previous_attempt" not in captured["user"].lower()


def test_feedback_only_includes_failed_tools(monkeypatch, mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    captured: dict[str, str] = {}

    def spy_llm(stage: str, user: str, json_mode: bool = True) -> str:
        captured["user"] = user
        return json.dumps({
            "goal": "g", "steps": [
                {"id": "step_1", "objective": "o", "action": "a",
                 "tool": "schema_search", "dependencies": [],
                 "expected_output": "e", "success_criteria": "s"},
            ],
        })

    monkeypatch.setattr(nodes, "_llm", spy_llm)
    state = _state_with_reflection_feedback()
    state = run_planner(state)

    # 成功的 schema_search 不应被当作"失败教训"回注
    import re
    feedback = re.search(r"previous_attempt.*?(?=$)", captured["user"], re.DOTALL)
    assert feedback is not None, "应存在 previous_attempt 反馈块"
    assert "SUCCESS" not in feedback.group(0), "反馈块只应包含失败的工具"
