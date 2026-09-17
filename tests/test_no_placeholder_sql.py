"""`SELECT 1` 占位符必须**响亮失败**，不能执行成"成功取到 1 行"。

真实基线里发现的假绿（2026-09-13，`deepseek-v4-flash`）
------------------------------------------------------
`r_caliber_period_mismatch` 与 `r_decompose_before_attribution` 两条 golden
**断言全绿（✅）**，但查 checkpoint 发现它们**每一步 SQL 都是 `SELECT 1`**：

```
step_1 SUCCESS rows=1 | SELECT 1
step_2 SUCCESS rows=1 | SELECT 1
step_4 SUCCESS rows=1 | SELECT 1
```

链路：planner 没排 `schema_search`（也没给 `input.sql`）→ `build_executor_params`
`_first_table` 解析不出表 → 退回占位 `SELECT 1` → **而 `SELECT 1` 是合法只读 SQL，
照常执行、返回 1 行、step 记 SUCCESS** → 分析在**假数据**上进行。
唯一指出问题的是 Reflection（"所有数据查询步骤均返回占位数据（SELECT 1）"），
但断言不依赖数据的 golden（口径 kind、报告文案）照样通过 → **假绿**。

`must_not_appear=("SELECT 1",)` 拦不住：它查的是**报告文本**，而报告里没有这句话。

修法
----
占位符从 `SELECT 1` 改为**空串**——`sql_tool.run` 对空 SQL 直接
`{"ok": False, "error": "缺少 sql 参数"}`，于是该步**响亮 FAILED**：
`tool_success_rate` 反映真相，Reflection 拿到的也是真失败而不是"成功的假数据"。
（`freeform` 分支的注释本就写着"空 → 无效占位，交由只读守卫判空"，但实现给的是 `SELECT 1`，
注释与实现不符。）
"""
from __future__ import annotations

from app.core.agents.data_analyst.nodes import build_executor_params
from app.core.agents.data_analyst.state import AgentState, PlanStep
from app.core.tools import execute_tool


def _state_no_schema() -> AgentState:
    """没有任何 schema_search 结果的会话——planner 漏排了它。"""
    s = AgentState(session_id="ph_sql", user_query="本月营收环比上季度增长 12%，说明增长强劲吗？")
    return s


def _step(tool: str, **inp) -> PlanStep:
    return PlanStep(id="step_1", objective="取数", action="查询", tool=tool,
                    input=(inp or None))


# --------------------------------------------------------------------------- #
# 1. 参数合成：不得产出占位 SQL
# --------------------------------------------------------------------------- #
def test_sql_query_without_table_yields_no_placeholder():
    params = build_executor_params(_state_no_schema(), _step("sql_query"))
    assert (params.get("sql") or "").strip() != "SELECT 1", (
        "`SELECT 1` 会被成功执行、返回 1 行假数据 → 假绿"
    )


def test_freeform_without_input_sql_yields_no_placeholder():
    params = build_executor_params(_state_no_schema(), _step("freeform"))
    assert (params.get("sql") or "").strip() != "SELECT 1"


# --------------------------------------------------------------------------- #
# 2. 端到端：占位步必须**失败**，而不是"成功取到 1 行"
# --------------------------------------------------------------------------- #
def test_placeholder_step_fails_loudly():
    params = build_executor_params(_state_no_schema(), _step("sql_query"))
    res = execute_tool("step_1", "sql_query", params, session_id="ph_sql")
    assert res.status == "FAILED", (
        f"合成不出真实 SQL 时必须失败，实际 status={res.status}（假绿来源）"
    )
    assert res.error, "失败要给出可读原因"


# --------------------------------------------------------------------------- #
# 3. 不破坏正常路径：有 schema_search 时照样合成真实 SQL
# --------------------------------------------------------------------------- #
def test_synthesis_still_works_with_schema():
    from app.core.agents.data_analyst.state import ToolResult

    s = _state_no_schema()
    s.tool_results = [ToolResult(
        step_id="s1", tool="schema_search", status="SUCCESS",
        output={"ok": True, "tables": [{
            "table": "fact_orders",
            "columns": [{"name": "region_id", "type": "INTEGER"},
                        {"name": "gmv", "type": "REAL"}],
            "row_count": 22767}]})]
    params = build_executor_params(s, _step("sql_query"))
    sql = params.get("sql") or ""
    assert "fact_orders" in sql, f"应落到真实表上，实际 {sql!r}"
    assert sql.strip() != "SELECT 1"
