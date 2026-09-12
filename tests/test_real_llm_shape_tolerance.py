"""真实 LLM 的松散 JSON 形状容错（planner 阶段）。

背景：换真实模型做端到端时，`planner` 返回
``stopping_criteria: [["Core business question answered"], ...]``
（字符串被多包了一层 list），严格 schema 直接 ValidationError，
把 planner 阶段打挂 → 整跑失败。Mock 永远不会有这种偏差，所以单测必须
**显式构造真实模型的畸形输出**。
"""
from __future__ import annotations

from app.core.agents.data_analyst.state import PlanModel


def test_nested_list_stopping_criteria_flattened():
    raw = {
        "goal": "分析销售",
        "stopping_criteria": [
            ["Core business question answered"],
            ["Sufficient evidence from SQL and image analysis"],
            ["Data quality is acceptable"],
            ["Reflection passes"],
        ],
        "steps": [],
    }
    plan = PlanModel.model_validate(raw)
    assert plan.stopping_criteria == [
        "Core business question answered",
        "Sufficient evidence from SQL and image analysis",
        "Data quality is acceptable",
        "Reflection passes",
    ]


def test_scalar_string_becomes_single_item():
    plan = PlanModel.model_validate({"stopping_criteria": "done"})
    assert plan.stopping_criteria == ["done"]


def test_dict_form_takes_values():
    plan = PlanModel.model_validate({"stopping_criteria": [{"a": "first", "b": "second"}]})
    assert plan.stopping_criteria == ["first", "second"]


def test_mixed_nested_and_scalar():
    plan = PlanModel.model_validate({"risk_points": ["a", ["b", ["c"]], {"d": "e"}]})
    assert plan.risk_points == ["a", "b", "c", "e"]


def test_none_and_missing_become_empty():
    assert PlanModel.model_validate({"stopping_criteria": None}).stopping_criteria == []
    assert PlanModel.model_validate({}).stopping_criteria == []


def test_tool_name_normalized():
    """模型爱写 'SQL Query' / 'image-analyze' → 归一化成工具名。"""
    plan = PlanModel.model_validate({
        "steps": [
            {"id": "s1", "objective": "查", "action": "查", "tool": "SQL Query"},
            {"id": "s2", "objective": "看图", "action": "看图", "tool": "image-analyze"},
        ]
    })
    assert [s.tool for s in plan.steps] == ["sql_query", "image_analyze"]


def test_string_step_becomes_minimal_step():
    plan = PlanModel.model_validate({"steps": ["step_1"]})
    assert len(plan.steps) == 1
    assert plan.steps[0].id == "step_1"
    assert plan.steps[0].tool == "sql_query"


def test_list_valued_objective_joined():
    plan = PlanModel.model_validate({
        "steps": [{"id": "s1", "objective": ["a", "b"], "action": "x", "tool": "sql_query"}]
    })
    assert plan.steps[0].objective == "a；b"


def test_dependencies_coerced_to_str_list():
    plan = PlanModel.model_validate({
        "steps": [{"id": "s2", "objective": "o", "action": "a", "tool": "sql_query",
                   "dependencies": [["s1"], "s0"]}]
    })
    assert plan.steps[0].dependencies == ["s1", "s0", "s0"] or \
        set(plan.steps[0].dependencies) == {"s0", "s1"}


def test_wellformed_plan_untouched():
    """正常结构不能被容错逻辑改坏。"""
    raw = {
        "goal": "g",
        "stopping_criteria": ["a", "b"],
        "steps": [{"id": "s1", "objective": "o", "action": "a", "tool": "sql_query",
                   "dependencies": [], "expected_output": "e", "success_criteria": "c"}],
    }
    plan = PlanModel.model_validate(raw)
    assert plan.goal == "g"
    assert plan.stopping_criteria == ["a", "b"]
    assert plan.steps[0].id == "s1" and plan.steps[0].tool == "sql_query"
