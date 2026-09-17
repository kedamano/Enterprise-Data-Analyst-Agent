"""E2-03：SQL 预检 —— 让"写错了"在**执行前**变成一句人话。

动因（D54 修完后的真实基线 `eval-real-20260915-d54.md`）
---------------------------------------------------------
`planner` 真的开始写 SQL 了（35/35 步自带 `input.sql`），于是第一次看到
自由写码的真实水平：**审计里 20 次 `sql_query` 只有 3 次 SUCCESS**。两类失败：

- **方言错**：`no such function: DATE_TRUNC`、`near "'1 month'": syntax error`、
  `near "3": syntax error`（`DATE_FORMAT(x, '%Y-%m')`）——模型在写 Postgres/MySQL，
  引擎却是 SQLite；
- **编造 schema**：`f.order_id` / `fs.customer_id` / `order_date` / `s.visitor_uv` /
  表 `fact_traffic`——**而 `fact_sales` 的真实列就在上一轮 `schema_search` 结果里躺着**。

`planner.md` 里"use exactly those table and column names. Never invent"**早就写着**，
而模型在 `context.assumptions` 里也**自己写出了真实列语义**——它看过真 schema，
生成 SQL 时仍然编。→ **提示词约束不住，要在执行器上做确定性的事。**

本文件钉住：方言预检（按引擎）+ schema 预检（防假红）+ 失败信息可操作 + 不空转重试。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst import nodes
from app.core.agents.data_analyst.sql_precheck import (
    dialect_hints,
    format_error,
    known_schema,
    unknown_references,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanModel,
    PlanStep,
    ToolResult,
)


def _t(table, cols, rows=100):
    return {"table": table, "row_count": rows,
            "columns": [{"name": c, "type": "TEXT"} for c in cols]}


_FACT = _t("fact_sales",
           ["sale_id", "sale_date", "region_id", "product_id",
            "channel_id", "revenue", "orders", "customers"], 3120)


def _state_with_schema(*tables) -> AgentState:
    s = AgentState(session_id="sp", user_query="各区域营收")
    s.context = ContextModel(objective="各区域营收", metrics=["营收"])
    s.tool_results = [ToolResult(step_id="s1", tool="schema_search", status="SUCCESS",
                                 output={"ok": True, "tables": list(tables or (_FACT,))})]
    return s


# --------------------------------------------------------------------------- #
# 一、方言预检：只在 SQLite 上说 SQLite 的话
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql,frag", [
    ("SELECT DATE_TRUNC('month', sale_date) FROM fact_sales", "strftime"),
    ("SELECT DATE_FORMAT(sale_date, '%Y-%m') FROM fact_sales", "strftime"),
    ("SELECT * FROM fact_sales WHERE sale_date >= CURRENT_DATE - INTERVAL '3 months'",
     "date("),
    ("SELECT sale_id::TEXT FROM fact_sales", "CAST"),
    ("SELECT `revenue` FROM fact_sales", '"'),
])
def test_postgres_mysql_constructs_get_a_sqlite_hint(sql, frag):
    hints = dialect_hints(sql, engine="sqlite")
    assert hints, f"应识别出非 SQLite 构造: {sql}"
    assert any(frag in h for h in hints), (frag, hints)


def test_the_same_sql_is_fine_on_postgres():
    """E7 多源：同一个 `input.source` 可能指向真 PG 库——那时 `DATE_TRUNC` 是**原生**写法。

    预检若不分引擎，就会**拦掉一条本来正确的 SQL**（假红）。
    """
    sql = "SELECT DATE_TRUNC('month', sale_date) FROM fact_sales"
    assert dialect_hints(sql, engine="postgres") == []
    assert dialect_hints(sql, engine="mysql") == []


def test_plain_sqlite_is_not_flagged():
    sql = ("SELECT region_id, SUM(revenue) AS rev FROM fact_sales "
           "GROUP BY region_id ORDER BY rev DESC LIMIT 20")
    assert dialect_hints(sql, engine="sqlite") == []


def test_interval_hint_names_the_replacement():
    """`- INTERVAL '3 months'` 是真实基线里报 `near \"'3 months'\"` 的那条。"""
    hints = dialect_hints(
        "SELECT * FROM fact_sales WHERE sale_date >= date('now') - INTERVAL '3 months'",
        engine="sqlite")
    assert hints and any("date(" in h for h in hints), hints


# --------------------------------------------------------------------------- #
# 二、schema 预检：未知标识符（**防假红是重点**）
# --------------------------------------------------------------------------- #
def test_unknown_qualified_column_is_named():
    schema = {"fact_sales": ["sale_id", "sale_date", "revenue", "orders"]}
    sql = "SELECT r.region_name, SUM(f.order_id) FROM fact_sales f GROUP BY 1"
    tables, cols = unknown_references(sql, schema)
    assert cols == ["order_id"], cols
    assert tables == []


def test_unknown_table_is_named():
    schema = {"fact_sales": ["sale_id", "revenue"]}
    sql = "WITH v AS (SELECT channel_id FROM fact_traffic) SELECT * FROM v"
    tables, _ = unknown_references(sql, schema)
    assert "fact_traffic" in tables, tables


def test_as_alias_is_resolved_too():
    schema = {"fact_sales": ["sale_id", "revenue"]}
    sql = "SELECT s.order_date FROM fact_sales AS s"
    _, cols = unknown_references(sql, schema)
    assert cols == ["order_date"], cols


def test_bare_columns_are_never_judged():
    """**这条是防假红的核心**：裸列名无法与别名/函数名/字符串区分 → 一律不判。"""
    schema = {"fact_sales": ["sale_id", "revenue"]}
    sql = "SELECT order_id, customer_id, DATE_TRUNC('month', sale_date) FROM fact_sales"
    _, cols = unknown_references(sql, schema)
    assert cols == [], f"裸列名不得被判未知（会拦掉正确 SQL），实际 {cols}"


def test_known_columns_are_not_flagged():
    schema = {"fact_sales": ["sale_id", "revenue", "orders"]}
    sql = "SELECT f.revenue, f.orders FROM fact_sales f"
    tables, cols = unknown_references(sql, schema)
    assert (tables, cols) == ([], [])


def test_cte_names_are_not_unknown_tables():
    schema = {"fact_sales": ["sale_id", "revenue"]}
    sql = ("WITH monthly AS (SELECT sale_id, revenue FROM fact_sales) "
           "SELECT * FROM monthly")
    tables, _ = unknown_references(sql, schema)
    assert tables == [], f"CTE 名不是未知表，实际 {tables}"


def test_upload_prefixed_table_is_judged_against_uploads():
    """边车库表以 `upload.` 前缀出现（`_qualify_upload` 的产物）。"""
    schema = {"upload.sleep": ["occupation", "sleep_hours"]}
    sql = "SELECT s.sleep_minutes FROM upload.sleep s"
    _, cols = unknown_references(sql, schema)
    assert cols == ["sleep_minutes"], cols


def test_empty_schema_never_judges():
    """还没发现过 schema → **不得**因为"不知道"而报未知。"""
    sql = "SELECT f.order_id FROM fact_sales f"
    assert unknown_references(sql, {}) == ([], [])
    assert unknown_references("", {"fact_sales": ["a"]}) == ([], [])


def test_known_schema_reads_the_last_schema_search():
    st = _state_with_schema()
    schema = known_schema(st)
    assert "fact_sales" in schema
    assert "revenue" in schema["fact_sales"]


def test_known_schema_is_empty_without_a_result():
    assert known_schema(AgentState(session_id="x", user_query="q")) == {}


# --------------------------------------------------------------------------- #
# 三、错误消息：一句话给全"它缺的三件事"
# --------------------------------------------------------------------------- #
def test_error_message_carries_columns_engine_and_hint():
    msg = format_error(
        "SELECT f.order_id FROM fact_sales f",
        {"fact_sales": ["sale_id", "sale_date", "revenue"]},
        message="no such column: f.order_id",
        engine="sqlite")
    assert "order_id" in msg
    assert "sale_id" in msg, "必须给出真实列清单"
    assert "SQLite" in msg, "必须说明引擎"
    assert "strftime" in msg, "必须给出方言提示"


def test_error_message_keeps_the_original():
    """原始报错不得被替换掉——它是事实，提示是补充。"""
    msg = format_error("SELECT 1", {}, message="near \"3\": syntax error", engine="sqlite")
    assert 'near "3"' in msg


def test_error_message_never_raises_on_junk():
    assert isinstance(format_error(None, {}, message=None, engine="sqlite"), str)
    assert isinstance(format_error("x", {}, message="", engine="sqlite"), str)


# --------------------------------------------------------------------------- #
# 四、接线：执行前拦方言；schema 只在失败后补消息；不空转重试
# --------------------------------------------------------------------------- #
def _sql_step(sid="step_2", sql="SELECT 1") -> PlanStep:
    return PlanStep(id=sid, objective="取数", action="执行 SQL",
                    tool="sql_query", input={"sql": sql})


def test_dialect_error_is_caught_before_execution(monkeypatch):
    """明知 SQLite 不认 → 不执行（省一次 DB 往返，且错误更可读）。"""
    st = _state_with_schema()
    seen: list[str] = []
    real = nodes.execute_tool

    def _spy(step_id, tool, params, session_id, *a, **k):
        seen.append(tool)
        return real(step_id, tool, params, session_id, *a, **k)

    monkeypatch.setattr(nodes, "execute_tool", _spy)
    st.plan = PlanModel(goal="g", steps=[_sql_step(
        sql="SELECT DATE_TRUNC('month', sale_date) FROM fact_sales")])
    nodes.run_executor(st)
    assert "sql_query" not in seen, "方言错的 SQL 不该被送进数据库"
    last = st.tool_results[-1]
    assert last.status == "FAILED" and "strftime" in (last.error or ""), last.error


def test_schema_check_does_not_block_execution(monkeypatch):
    """未知列**不拦**——表/列提取有误判风险，拦掉正确 SQL 比多跑一次贵得多。"""
    st = _state_with_schema()
    seen: list[str] = []
    monkeypatch.setattr(nodes, "execute_tool",
                        lambda *a, **k: (seen.append(a[1]) or ToolResult(
                            step_id=a[0], tool=a[1], status="SUCCESS", output={})))
    st.plan = PlanModel(goal="g", steps=[_sql_step(
        sql="SELECT f.order_id FROM fact_sales f")])
    nodes.run_executor(st)
    assert "sql_query" in seen, "schema 预检不得阻塞执行"


def test_failure_message_carries_the_real_columns(monkeypatch):
    """失败后把真实列清单补进 error —— 这是 REPLAN 唯一能拿到的上下文。"""
    st = _state_with_schema()
    monkeypatch.setattr(nodes, "execute_tool", lambda *a, **k: ToolResult(
        step_id=a[0], tool=a[1], status="FAILED",
        error="(sqlite3.OperationalError) no such column: f.order_id"))
    st.plan = PlanModel(goal="g", steps=[_sql_step(
        sql="SELECT f.order_id FROM fact_sales f")])
    nodes.run_executor(st)
    err = next(r.error for r in st.tool_results if r.status == "FAILED")
    assert "sale_date" in err, f"应补上 fact_sales 的真实列，实际 {err}"


def test_no_verbatim_retry_for_model_authored_sql(monkeypatch):
    """`__retry` 逐字重跑同一条 SQL = 空转（真实基线里必然再失败一次）。

    但 `schema_search` 恢复**要保留**——它的结果喂给 planner 的 `discovered_schema`。
    """
    st = _state_with_schema()
    calls: list[str] = []

    def _fake(step_id, tool, params, session_id, *a, **k):
        calls.append(step_id)
        if tool == "schema_search":
            return ToolResult(step_id=step_id, tool=tool, status="SUCCESS",
                              output={"ok": True, "tables": [_FACT]})
        return ToolResult(step_id=step_id, tool=tool, status="FAILED",
                          error="(sqlite3.OperationalError) no such column: f.order_id")

    monkeypatch.setattr(nodes, "execute_tool", _fake)
    st.plan = PlanModel(goal="g", steps=[_sql_step(
        sql="SELECT f.order_id FROM fact_sales f")])
    nodes.run_executor(st)
    assert not [c for c in calls if c.endswith("__retry")], f"不得逐字重跑: {calls}"
    assert any("schema_search" in c or "recovery" in c for c in calls), calls


def test_synthesized_path_still_retries(monkeypatch):
    """无 `input.sql` 的步骤仍走原重试路径（那里 `_first_table` 会因新 schema 重选表）。"""
    st = _state_with_schema()
    calls: list[str] = []

    def _fake(step_id, tool, params, session_id, *a, **k):
        calls.append(step_id)
        if tool == "schema_search":
            return ToolResult(step_id=step_id, tool=tool, status="SUCCESS",
                              output={"ok": True, "tables": [_FACT]})
        return ToolResult(step_id=step_id, tool=tool, status="FAILED",
                          error="no such column: revenue")

    monkeypatch.setattr(nodes, "execute_tool", _fake)
    step = PlanStep(id="step_3", objective="取数", action="执行 SQL",
                    tool="sql_query", input=None)
    st.plan = PlanModel(goal="g", steps=[step])
    nodes.run_executor(st)
    assert [c for c in calls if c.endswith("__retry")], f"合成路径应保留重试: {calls}"
