"""D38：analyst 的**不可用输出被静默当成成功**——E1 溯源维度因此测不出来。

现象（真实基线）
----------------
7 条用例全部 `findings=0`、`numeric_claims=0/0`、`溯源覆盖率=None`。
即 **E1 的溯源维度在真实模型下压根无从度量**——不是失效，是输入就是空的。

根因（查 checkpoint 的 `analysis.raw` 得到，五种形状）
----------------------------------------------------
真实模型返回的东西五花八门，而 `AnalysisResult` 的字段**全有默认值**，
于是**任何** dict 都能过 pydantic 校验、得到一份**全空的**分析，且**全程无声**：

| 形状 | 实测样例 |
|---|---|
| ✅ 正确 | `findings/hypotheses/limitations/metrics` |
| ① 包一层壳 | `{"role":"analyst","content":"<真正的JSON字符串>"}` |
| ② 工具调用 | `{"tool":"sql_query","arguments":{}}` / `{"tool":..,"input":..}` |
| ③ 别的阶段的 schema | analyst 返回了 planner 的 `{"goal","steps","stopping_criteria"}` |
| ④ ToolResult dump | `{"step_id","tool","status","artifacts",...}` |

`run_analyst` 调 `_llm_model` 时**没有传 `ok=`**，所以"能过校验"被当成了"可用"——
这正是本项目在 planner 上早已记过的坑（"能过校验 ≠ 可用"），analyst 漏了。

修法
----
1. `_parse_json` **拆掉包装壳**（①）：内层 payload 常常是**字符串**，原实现只解析外层；
2. 给 analyst 补 **可用性判据** `ok=`：至少有一个实质字段非空（②③④ 由此被拒 → 走
   既有的"回喂原因重试 → 仍不可用则降级并记 error"链路，不再静默）。
"""
from __future__ import annotations

import json

from app.core.agents.data_analyst.nodes import _analysis_usable, _parse_json, run_analyst
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    PlanModel,
    PlanStep,
)

WRAPPED = json.dumps({
    "role": "analyst",
    "content": json.dumps({
        "metrics": [{"name": "revenue"}],
        "findings": [{"finding": "各品类营收如下", "confidence": 0.8}],
        "limitations": [],
        "hypotheses": [],
        "recommendations": [],
    }, ensure_ascii=False),
}, ensure_ascii=False)


def _state() -> AgentState:
    s = AgentState(session_id="ana_usable", user_query="统计各品类营收")
    s.context = ContextModel(objective="统计各品类营收", metrics=["营收"])
    s.plan = PlanModel(goal="g", steps=[
        PlanStep(id="s1", objective="取数", action="查询", tool="sql_query")])
    return s


# --------------------------------------------------------------------------- #
# 一、包装壳必须拆掉（内层是**字符串** JSON）
# --------------------------------------------------------------------------- #
def test_parse_json_unwraps_role_content_shell():
    out = _parse_json(WRAPPED)
    assert out.get("findings"), f"壳没拆开，仍是 {sorted(out)}"


def test_parse_json_unwraps_nested_object():
    out = _parse_json(json.dumps({"content": {"findings": [{"finding": "x"}]}}))
    assert out.get("findings")


def test_parse_json_does_not_touch_a_normal_payload():
    payload = {"findings": [{"finding": "x"}], "metrics": []}
    assert _parse_json(json.dumps(payload)) == payload


def test_parse_json_does_not_unwrap_tool_call_shape():
    """工具调用 JSON **不是**壳——它就是要被拒的内容，别给它找"内层"。"""
    raw = json.dumps({"tool": "sql_query", "arguments": {"sql": "SELECT 1"}})
    out = _parse_json(raw)
    assert out.get("tool") == "sql_query" and "findings" not in out


# --------------------------------------------------------------------------- #
# 二、可用性判据
# --------------------------------------------------------------------------- #
def test_usable_requires_at_least_one_substantive_field():
    assert not _analysis_usable(AnalysisResult())
    assert _analysis_usable(AnalysisResult(findings=[{"finding": "x"}]))
    assert _analysis_usable(AnalysisResult(limitations=["数据缺失"]))


def test_no_data_analysis_with_only_limitations_is_usable():
    """如实报告"没数据"是**合法**结论，不能被当成不可用而反复重试。"""
    assert _analysis_usable(AnalysisResult(limitations=["工具结果中没有实际 SQL 结果"]))


# --------------------------------------------------------------------------- #
# 三、端到端：包壳输出不得导致"全空且无声"
# --------------------------------------------------------------------------- #
def test_run_analyst_accepts_wrapped_payload(monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: WRAPPED)
    state = run_analyst(_state())
    assert state.analysis.findings, "包壳内容被丢了（这正是真实基线的病）"


def test_run_analyst_rejects_tool_call_shape_loudly(monkeypatch):
    """工具调用形状 → 既不能当分析，也不能静默变空：必须回喂重试并留下降级说明。"""
    import app.core.agents.data_analyst.nodes as nodes

    seen: list[str] = []

    def fake_llm(stage, user, json_mode=True):
        seen.append(user)
        return json.dumps({"tool": "sql_query", "arguments": {"sql": "SELECT 1"}})

    monkeypatch.setattr(nodes, "_llm", fake_llm)
    state = run_analyst(_state())

    assert len(seen) >= 2, "不可用输出必须触发回喂重试"
    assert any("上一次输出不可用" in u for u in seen), "重试要带具体原因"
    assert state.error and "analyst" in state.error, (
        "最终仍不可用必须留下降级说明（铁律 3：不许静默）")
