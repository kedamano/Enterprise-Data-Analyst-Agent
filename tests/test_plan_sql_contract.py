"""E2-02：计划步骤必须自带 SQL —— 执行器**不得从步骤目标臆造一条查询**。

动因（`eval --mode real` 首次全量基线的解剖，2026-09-15）
-----------------------------------------------------------
planner 出了 8 步目标明确的计划，但**每一步 `input` 都是 `{}`** →
`build_executor_params` 走最终兜底 `SELECT * FROM {table} LIMIT 100` →
`_first_table()` 取 `schema_search.tables[0]` → 那是
`insp.get_table_names()` 的**字典序第一张** = `dim_channel`（3 行维表）。

审计实据：`q_revenue_diag` 的 6 个 SQL 步骤**全部**是
`SELECT * FROM dim_channel LIMIT 100`，返回 3 行、不报错、记 SUCCESS
→ `工具成功率 0.986`。而 `q_region_top` 据此**凭空造出一整张区域营收表**却判 ✅。

本文件钉住三件事：
① 选表必须**看内容**（度量列 / 词面 / 规模），不得盲取 `tables[0]`；
② 选不出来 → **响亮失败**（空 SQL），绝不退回"某张表"；
③ planner 侧：步骤 schema 必须暴露 `input`，且重规划时能看见 schema。
"""
from __future__ import annotations

import json

import pytest

from app.core.agents.data_analyst import nodes
from app.core.agents.data_analyst.nodes import (
    _first_table,
    _pick_table,
    build_executor_params,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    ContextModel,
    PlanModel,
    PlanStep,
    ToolResult,
)
from app.core.tools import execute_tool


# --------------------------------------------------------------------------- #
# 夹具：两个样例库的**真实**表结构（见 scripts/generate_analyst_sample.py）
# --------------------------------------------------------------------------- #
def _t(table, cols, rows):
    return {"table": table, "row_count": rows,
            "columns": [{"name": c, "type": "TEXT"} for c in cols]}


# `dim_channel` 字典序第一 —— 真实基线里占掉 `tables[0]` 的正是它
_REAL_SAMPLE_TABLES = [
    _t("dim_channel", ["channel_id", "channel_name"], 3),
    _t("dim_product", ["product_id", "product_name", "category"], 4),
    _t("dim_region", ["region_id", "region_name", "country"], 5),
    _t("fact_sales", ["sale_id", "sale_date", "region_id", "product_id",
                      "channel_id", "revenue", "orders", "customers"], 3120),
]


def _state(query="对比各区域营收表现，识别增长最快的地区", *,
           metrics=("营收",), dimensions=("region",)) -> AgentState:
    s = AgentState(session_id="psc", user_query=query)
    s.context = ContextModel(objective=query, metrics=list(metrics),
                             dimensions=list(dimensions))
    return s


def _with_schema(state: AgentState, tables) -> AgentState:
    state.tool_results = [ToolResult(
        step_id="s1", tool="schema_search", status="SUCCESS",
        output={"ok": True, "tables": tables})]
    return state


def _step(tool="sql_query", sid="step_2", objective="取各区域营收") -> PlanStep:
    return PlanStep(id=sid, objective=objective, action="执行 SQL 聚合",
                    tool=tool, input=None)


# --------------------------------------------------------------------------- #
# 一、不得盲取 tables[0]（真实基线的根因）
# --------------------------------------------------------------------------- #
def test_does_not_pick_the_alphabetically_first_table():
    st = _with_schema(_state(), _REAL_SAMPLE_TABLES)
    table, cols = _pick_table(st, _step().objective + _step().action)
    assert table != "dim_channel", (
        "`tables[0]` 是字典序第一张（dim_channel，3 行维表）——"
        "真基线里每个 SQL 步骤都被合成到了它上面"
    )
    assert table == "fact_sales", f"应选到含度量列的事实表，实际 {table!r}"
    assert "revenue" in cols


def test_synthesized_sql_is_not_the_baseline_wildcard():
    """端到端复现：真基线的 `SELECT * FROM dim_channel LIMIT 100` 不得再出现。"""
    st = _with_schema(_state(), _REAL_SAMPLE_TABLES)
    params = build_executor_params(st, _step())
    sql = (params.get("sql") or "").strip()
    assert sql != "SELECT * FROM dim_channel LIMIT 100", (
        "这正是真实基线里 6 个步骤重复执行的那条查询"
    )
    assert "fact_sales" in sql, f"应落在真实事实表上，实际 {sql!r}"


def test_measure_column_wins_over_name_similarity():
    """有度量列的表优先于"名字沾边"的表。"""
    tables = [
        _t("region_summary", ["region_name", "note"], 20),          # 名字很沾边，但没度量
        _t("fact_orders", ["order_id", "region_id", "revenue"], 900),  # 有度量
    ]
    st = _with_schema(_state(dimensions=("region",)), tables)
    table, _ = _pick_table(st, "统计各区域营收")
    assert table == "fact_orders", f"度量列是硬信号，实际选到 {table!r}"


def test_uploaded_table_still_wins():
    """ATTACH/01 不回归：用户上传了数据时，候选**收缩到上传表**（结构性优先）。"""
    tables = [
        {"table": "sleep", "row_count": 30, "match": "user_upload",
         "columns": [{"name": "occupation", "type": "TEXT"},
                     {"name": "sleep_hours", "type": "REAL"}]},
        _t("fact_sales", ["sale_id", "revenue"], 3120),
    ]
    st = _with_schema(_state(), tables)
    table, _ = _pick_table(st, "分析睡眠时长")
    assert table == "sleep", f"上传数据必须优先，实际 {table!r}"


def test_first_table_delegates_to_pick_table():
    """`_first_table` 保留旧签名，但**不再**等于 `tables[0]`。"""
    st = _with_schema(_state(), _REAL_SAMPLE_TABLES)
    assert _first_table(st)[0] == _pick_table(st, "")[0]
    assert _first_table(st)[0] != "dim_channel"


# --------------------------------------------------------------------------- #
# 二、选不出来就响亮失败（fail-closed）
# --------------------------------------------------------------------------- #
def test_fails_closed_when_no_candidate_is_usable():
    """**多个**候选都没法支撑"分析" → 空 SQL → 该步 FAILED，绝不猜一张表。

    注意这里是**两个**候选：fail-closed 的前提是"有得选、但选不出"
    （D54 的 bug 正是"字典序第一张 vs 真正该查的那张"）。
    只有一个候选时不存在"选错"——见下一条。
    """
    tables = [
        _t("dim_channel", ["channel_id", "channel_name"], 3),
        _t("dim_product", ["product_id", "product_name"], 3),
    ]
    st = _with_schema(_state(query="分析最近半年营收下滑的原因"), tables)
    table, _ = _pick_table(st, "查询各渠道营收")
    assert table == "", f"无可支撑的表时必须返回空，实际 {table!r}"

    params = build_executor_params(st, _step(objective="查询各渠道营收"))
    assert (params.get("sql") or "").strip() == "", (
        "空 SQL 才会让 sql_tool 判 `缺少 sql 参数` 并响亮 FAILED（D38 的约定）"
    )
    res = execute_tool("step_2", "sql_query", params, session_id="psc")
    assert res.status == "FAILED", f"必须失败，实际 {res.status}（假绿来源）"


def test_single_candidate_is_used_even_with_zero_score():
    """**只有一个候选时不 fail-closed**：没有第二个选项，就没有"选错"这回事。

    回归逼出来的（D54 全量回归，`tests/test_export.py` / `test_dlp.py`）：
    夹具建的是单表 `c(id INTEGER, phone TEXT)`（无度量列），
    "导出脱敏值"本就不需要聚合列。一刀切 fail-closed 会让这条链路整条断掉，
    而 D54 要堵的 bug **需要 ≥2 个候选**才复现。
    """
    tables = [_t("c", ["id", "phone"], 1)]
    st = _with_schema(_state(query="看看 c 表"), tables)
    assert _pick_table(st, "看看 c 表")[0] == "c"
    # 但"一个结果都没有"仍然响亮失败（下一条用例），两者不是一回事
    assert _pick_table(_state(), "看看 c 表") == ("", [])


def test_no_schema_still_fails_closed():
    """一个 schema_search 结果都没有 → 仍然空 SQL（既有行为，不许回归）。"""
    st = _state()
    assert _pick_table(st, "查询各渠道营收") == ("", [])
    assert (build_executor_params(st, _step()).get("sql") or "").strip() == ""


def test_dimension_table_is_still_used_when_it_is_the_only_option():
    """维表惩罚是**相对**的：没有别的候选可挑时，真实的维表数据也胜过硬猜一张表。"""
    tables = [_t("dim_product", ["product_id", "product_name", "category"], 4)]
    st = _with_schema(_state(metrics=("品类",), dimensions=("product",)), tables)
    table, _ = _pick_table(st, "按产品 category 统计")
    assert table == "dim_product", (
        "命名只作相对信号；否则会把名为 dim_x 的可用表一并压掉，退化成失败"
    )


# --------------------------------------------------------------------------- #
# 三、合成 SQL 要贴合分析意图（维度列匹配放宽）
# --------------------------------------------------------------------------- #
def test_relaxed_dimension_match_uses_the_real_column():
    st = _with_schema(_state(), _REAL_SAMPLE_TABLES)
    sql = build_executor_params(st, _step()).get("sql", "")
    assert "SELECT *" not in sql.upper(), (
        "`dimensions=['region']` 与列 `region_id` 精确不等 → 旧实现退化成了 SELECT *"
    )
    assert "GROUP BY" in sql.upper()
    assert "region_id" in sql


def test_exact_dimension_match_still_preferred():
    """既有行为不回归：列名与维度名**精确相等**时仍走原路径。"""
    tables = [_t("fact_sales", ["region", "revenue"], 3120)]
    st = _with_schema(_state(dimensions=("region",)), tables)
    sql = build_executor_params(st, _step()).get("sql", "")
    assert '"region"' in sql and "GROUP BY" in sql.upper()


# --------------------------------------------------------------------------- #
# 四、planner 侧：步骤 schema 暴露 `input`，重规划时看得见 schema
# --------------------------------------------------------------------------- #
def test_planner_prompt_exposes_input_sql():
    from app.core.prompts import load_prompt

    text = load_prompt("planner")
    assert '"input"' in text, (
        "Plan Step 的 JSON schema 里根本没有 input 字段——模型从没被要求过给 SQL。"
        "这正是真基线里「每步 input={}」的来源。"
    )
    assert "input.sql" in text
    assert "discovered_schema" in text, "有 schema 时必须照它写，不得臆造表名/列名"


def test_planner_context_carries_discovered_schema(monkeypatch):
    """重规划时 planner 第一次看得见 schema（否则它想写 SQL 也无从写起）。"""
    captured: dict = {}

    def _fake_llm(model_cls, stage, user, **kw):
        captured["user"] = user
        return PlanModel(goal="g", steps=[PlanStep(
            id="step_1", objective="o", action="a", tool="schema_search")]), None

    monkeypatch.setattr(nodes, "_llm_model", _fake_llm)

    st = _with_schema(_state(), _REAL_SAMPLE_TABLES)
    nodes.run_planner(st)

    assert "discovered_schema" in captured["user"], (
        "planner 的 task_context 里没有 schema —— 它不可能写出 input.sql"
    )
    payload = captured["user"]
    assert "fact_sales" in payload and "revenue" in payload
    # 脱敏纪律：摘要只带表名/列名/行数（结构），**绝不夹带数据行**（取值）
    body = payload.split("<task_context>", 1)[1]
    assert "row_sample" not in body and '"values"' not in body
    assert len(body) < 6000, "schema 摘要必须有界，不能把整个 schema 灌进 prompt"


def test_planner_context_omits_schema_key_when_absent(monkeypatch):
    """没有 schema_search 结果时不注入空壳键（模型会照着它编表名）。"""
    captured: dict = {}

    def _fake_llm(model_cls, stage, user, **kw):
        captured["user"] = user
        return PlanModel(goal="g", steps=[PlanStep(
            id="step_1", objective="o", action="a", tool="schema_search")]), None

    monkeypatch.setattr(nodes, "_llm_model", _fake_llm)
    nodes.run_planner(_state())
    assert "discovered_schema" not in captured["user"]
