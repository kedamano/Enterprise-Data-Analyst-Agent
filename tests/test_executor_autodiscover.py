"""执行器**自动补发现 schema**：把"合成不出 SQL"从失败变成可用。

动因（真实基线实测，2026-09-13）
--------------------------------
planner **既不给 `input.sql`、也常漏排 `schema_search`** →
`build_executor_params` 里 `_first_table` 解析不出表 → 合成不出真实 SQL →
（改前）退回占位 `SELECT 1` 假绿 /（改后）该步响亮 FAILED → 后面整串 `依赖步骤未完成`。

真实基线读数：**工具成功率只有 0.36，失败的全部是这一类**。

修法选择
--------
- ① 改 `planner.md` 要求每步必给 SQL：**本文件当时不选**——模型未必听，
  故先做确定性的 ②（**2026-09-15 D54 更正**：当时的理由写成"该文件被
  `test_prompt_negative` 钉死"，这是**读错了**——那个文件只断言 misuse guardrails
  存在，加内容是安全的；且真实基线证明 `planner.md` 的步骤 schema **根本没有
  `input` 字段**，模型不是不听、是没被要求过。D54 已补上提示词侧，见
  `docs/specs/E2/02-plan-sql-contract.md`。两条路是**互补**的：提示词提高"写对"的
  概率，执行器选表保证"写不对也不会落到任意表上"）。
- ② **执行器在解析不出表时补一次真实的 `schema_search`**：把"不可用"变成"可用"，
  确定性、可测、不碰提示词。**本文件钉的就是 ②**。

设计约束
--------
- 只在**确实需要表**且**确实拿不到表**时才补（已有 schema / 步骤自带 `input.sql` /
  上传表可解析 → 一律不补）；
- 补的那次是**真实工具调用**，进 `tool_results`（可审计）；
- 补了必须在 `metadata` 里留痕（铁律 3：任何自动行为都要可观测）；
- 并发批次的发现动作在**主线程**做一次，避免多线程写同一个 `tool_results`。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst.nodes import (
    _needs_schema_discovery,
    ensure_schema_discovered,
    run_executor,
    run_executor_all,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanModel,
    PlanStep,
)


def _state(metrics=("营收",)) -> AgentState:
    s = AgentState(session_id="auto_disc", user_query="分析各区域营收表现")
    s.context = ContextModel(objective="分析各区域营收", metrics=list(metrics))
    return s


def _step(tool: str, sid: str = "step_1", **inp) -> PlanStep:
    return PlanStep(id=sid, objective="取数", action="查询", tool=tool,
                    input=(inp or None))


# --------------------------------------------------------------------------- #
# 一、什么时候该补 / 不该补
# --------------------------------------------------------------------------- #
def test_needs_discovery_for_bare_sql_query():
    assert _needs_schema_discovery(_step("sql_query"), _state())


def test_needs_discovery_for_dataset_profile_without_table():
    assert _needs_schema_discovery(_step("dataset_profile"), _state())


def test_no_discovery_when_schema_search_already_done():
    from app.core.agents.data_analyst.state import ToolResult

    s = _state()
    s.tool_results = [ToolResult(step_id="s0", tool="schema_search", status="SUCCESS",
                                 output={"ok": True, "tables": [{"table": "fact_sales",
                                                                 "columns": []}]})]
    assert not _needs_schema_discovery(_step("sql_query"), s)


def test_no_discovery_when_step_carries_its_own_sql():
    """步骤自带 SQL → 不需要表名，补发现是多余的。"""
    assert not _needs_schema_discovery(
        _step("sql_query", sql="SELECT 1 FROM t"), _state())
    assert not _needs_schema_discovery(
        _step("freeform", sql="SELECT 1 FROM t"), _state())


def test_no_discovery_for_tools_that_do_not_need_a_table():
    for tool in ("schema_search", "knowledge_search", "generate_report",
                 "visualization", "python_analysis"):
        assert not _needs_schema_discovery(_step(tool), _state()), tool


# --------------------------------------------------------------------------- #
# 二、补发现：真跑、可审计、留痕
# --------------------------------------------------------------------------- #
def test_ensure_schema_discovered_runs_real_search_and_records():
    s = _state()
    assert ensure_schema_discovered(s, [_step("sql_query")]) is True
    hits = [r for r in s.tool_results if r.tool == "schema_search"]
    assert hits and hits[0].status == "SUCCESS", hits
    assert s.metadata.get("auto_schema_search"), "补发现必须在 metadata 留痕（铁律 3）"


def test_ensure_schema_discovered_is_idempotent():
    s = _state()
    ensure_schema_discovered(s, [_step("sql_query")])
    n = len(s.tool_results)
    assert ensure_schema_discovered(s, [_step("sql_query")]) is False
    assert len(s.tool_results) == n, "已有 schema 时不得重复发现"


def test_ensure_schema_discovered_noop_when_not_needed():
    s = _state()
    assert ensure_schema_discovered(s, [_step("generate_report")]) is False
    assert s.tool_results == []


# --------------------------------------------------------------------------- #
# 三、端到端：不补 → 步骤失败；补了 → 步骤成功且是真实 SQL
# --------------------------------------------------------------------------- #
def _plan_sql_then_report() -> PlanModel:
    return PlanModel(goal="g", steps=[
        PlanStep(id="step_1", objective="取数", action="查营收", tool="sql_query"),
        PlanStep(id="step_2", objective="报告", action="出报告", tool="generate_report",
                 dependencies=["step_1"]),
    ])


def test_sequential_executor_recovers_from_missing_schema_search():
    s = _state()
    s.plan = _plan_sql_then_report()
    run_executor(s)          # step_1

    sql_hits = [r for r in s.tool_results if r.tool == "sql_query"]
    assert sql_hits, s.tool_results
    assert sql_hits[0].status == "SUCCESS", (
        f"补发现后应能合成真实 SQL 并成功，实际 {sql_hits[0].status}: {sql_hits[0].error}")


def test_wave_executor_recovers_from_missing_schema_search():
    s = _state()
    s.plan = _plan_sql_then_report()
    run_executor_all(s)

    sql_hits = [r for r in s.tool_results if r.tool == "sql_query"]
    assert sql_hits and sql_hits[0].status == "SUCCESS", s.tool_results
    # 下游步骤不应再因"依赖未完成"连带失败
    assert not any(r.error == "依赖步骤未完成" for r in s.tool_results), s.tool_results


def test_auto_discovery_does_not_fabricate_data_when_db_unreachable(monkeypatch):
    """库里确实拿不到表时，**不得**假装成功——仍应响亮失败。"""
    monkeypatch.setenv("DATA_DB_URL", "sqlite:///./data/__no_such_db__.db")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        s = _state()
        s.plan = _plan_sql_then_report()
        run_executor(s)
        sql_hits = [r for r in s.tool_results if r.tool == "sql_query"]
        assert sql_hits and sql_hits[0].status == "FAILED", (
            "拿不到数据时绝不能判成功（假绿）")
    finally:
        get_settings.cache_clear()
