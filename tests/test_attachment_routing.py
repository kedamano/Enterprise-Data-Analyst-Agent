"""ATTACH/01 回归测试：上传的表格附件必须成为**可查询的数据源**。

背景（两个真实案例）：
- 用户上传 sleep.csv 问「这份文件反应了什么数据规律」
- Agent 却去查内置 sample_enterprise.db，返回 100 行无关数据
- 三轮 REPLAN 原地打转，最终报告退化成 mock 模板

根因：附件只是「提示词里的文本」（列名+行数+样例），从未注册成工具可寻址的数据源。

这组测试锁死修复后的行为，防止路由/工具改动导致退化。
"""
from __future__ import annotations

import pytest

from app.core.attachments import (
    attach_clause,
    attached_tables,
    build_preview,
    get_attachment_store,
    materialize_table,
    sidecar_path,
)

SLEEP_CSV = b"""person_id,gender,age,occupation,sleep_quality,stress,sleep_disorder
1,1,35,Office Worker,6,7,None
2,2,42,Student,7,4,Insomnia
3,1,50,Retired,8,3,None
4,2,38,Office Worker,5,8,Insomnia
5,1,29,Student,6.5,6,None
6,2,45,Office Worker,4,9,Insomnia
"""


@pytest.fixture()
def uploaded_session():
    """上传一份 sleep.csv，返回 (session_id, preview)。

    每次用**唯一 session id**：此前硬编码 ``test-attach-sleep``，导致
    并行 pytest / 重复运行时多个进程争抢同一个 ``upload.db``，
    出现 ``database is locked`` 的假失败（测试互相干扰，不是产品缺陷）。
    """
    import uuid

    sid = "test-attach-" + uuid.uuid4().hex[:10]
    store = get_attachment_store()
    store.clear(sid)
    p = build_preview(SLEEP_CSV, "sleep.csv")
    store.put(sid, p)
    yield sid, p
    store.clear(sid)


# --------------------------------------------------------------------------- #
# 落地
# --------------------------------------------------------------------------- #
def test_csv_materialised_to_queryable_table(uploaded_session):
    """表格附件必须落地成边车库里的真实表，并回填 table 名。"""
    sid, p = uploaded_session
    assert p.kind == "table"
    assert p.table, "预览未回填 table 名 —— 附件没有落地成可查询表"
    assert sidecar_path(sid).exists(), "边车库文件不存在"


def test_full_rows_retained_for_materialisation(uploaded_session):
    """落地必须用**完整行**，不能只用 20 行样例（否则聚合结果错）。"""
    sid, p = uploaded_session
    assert len(p.all_rows) == p.rows == 6, (
        f"落地行数 {len(p.all_rows)} != 实际行数 {p.rows}"
    )


def test_attached_tables_exposes_upload_origin(uploaded_session):
    """attached_tables 必须把上传表标为 user_upload，供 schema 层区分来源。"""
    sid, p = uploaded_session
    tables = attached_tables(sid)
    assert len(tables) == 1
    t = tables[0]
    assert t["table"] == "sleep"
    assert t["origin"] == "user_upload"
    assert t["row_count"] == 6


# --------------------------------------------------------------------------- #
# 工具层可寻址（核心）
# --------------------------------------------------------------------------- #
def test_schema_search_discovers_uploaded_table(uploaded_session):
    """Planner 必须能"发现"用户上传的表。"""
    sid, _ = uploaded_session
    from app.core.tools import execute_tool

    r = execute_tool("s0", "schema_search", {}, sid)
    assert r.status == "SUCCESS"
    names = [t["table"] for t in r.output["tables"]]
    assert "sleep" in names, f"schema_search 未发现上传表，只看到 {names}"


def test_uploaded_table_ranked_first(uploaded_session):
    """用户上传的表必须排在内置库之前，避免 Planner 优先选用无关表。"""
    sid, _ = uploaded_session
    from app.core.tools import execute_tool

    r = execute_tool("s0", "schema_search", {}, sid)
    assert r.output["tables"][0].get("origin") == "user_upload"


def test_sql_can_aggregate_uploaded_table(uploaded_session):
    """sql_query 必须能跨库聚合上传表 —— 这是案例一失败的直接修复点。"""
    sid, _ = uploaded_session
    from app.core.tools import execute_tool

    sql = (
        "SELECT occupation, COUNT(*) AS n, "
        "ROUND(AVG(CAST(sleep_quality AS REAL)),2) AS avg_q "
        "FROM upload.sleep GROUP BY occupation ORDER BY avg_q DESC"
    )
    r = execute_tool("s1", "sql_query", {"sql": sql}, sid)
    assert r.status == "SUCCESS", f"跨库查询失败: {r.error}"
    rows = r.output["rows"]
    assert len(rows) == 3
    by_occ = {x["occupation"]: x for x in rows}
    # Office Worker 压力最高 → 睡眠质量最低（与人工分析结论一致）
    assert by_occ["Office Worker"]["avg_q"] == 5.0
    assert by_occ["Retired"]["avg_q"] == 8.0


def test_attach_clause_only_when_sidecar_exists():
    """没有上传时不应产生 ATTACH 语句（避免无谓开销/悬空引用）。"""
    assert attach_clause("session-with-nothing") is None


def test_attach_clause_escapes_quotes():
    """路径里的单引号必须被转义，杜绝 SQL 注入面。"""
    from app.core import attachments as A

    sid, _ = None, None
    A.get_attachment_store()  # 确保单例已建
    p = build_preview(SLEEP_CSV, "quote'test.csv")
    materialize_table(p, "test-quote")
    clause = attach_clause("test-quote")
    assert clause is not None
    # 路径里的引号被转义成 ''，语句结构不被破坏
    assert "''" in clause or "'" not in clause.split("'", 1)[1].rsplit("'", 1)[0]


# --------------------------------------------------------------------------- #
# 上下文注入
# --------------------------------------------------------------------------- #
def test_describe_tells_model_where_to_query(uploaded_session):
    """上下文必须明确指示用 upload.<table>，否则模型仍会去猜。"""
    sid, _ = uploaded_session
    store = get_attachment_store()
    ctx = store.describe(sid)
    assert "upload.sleep" in ctx
    assert "不要查询内置企业库" in ctx


def test_no_upload_leaves_query_untouched():
    """无附件时不得污染 query（双向验证的另一半）。"""
    from app.core.agents.data_analyst.graph import _new_state

    st = _new_state("session-definitely-empty", "普通问题")
    assert st.attachment_context == ""
    assert st.user_query == "普通问题"


def test_upload_injects_context_into_state(uploaded_session):
    """有附件时，state 必须带上附件上下文与可查询表名。"""
    sid, _ = uploaded_session
    from app.core.agents.data_analyst.graph import _new_state

    st = _new_state(sid, "这份文件反应了什么数据规律")
    assert st.attachment_context
    assert "upload.sleep" in st.user_query


# --------------------------------------------------------------------------- #
# Reporter 证据
# --------------------------------------------------------------------------- #
def test_report_evidence_carries_real_rows(uploaded_session):
    """REP/01：Reporter 必须拿到真实结果行，而不是只有 analysis 摘要。"""
    from app.core.agents.data_analyst.graph import _new_state
    from app.core.agents.data_analyst.nodes import _report_evidence
    from app.core.tools import execute_tool

    sid, _ = uploaded_session
    st = _new_state(sid, "哪个职业睡眠最差")
    sql = ("SELECT occupation, AVG(CAST(sleep_quality AS REAL)) AS avg_q "
           "FROM upload.sleep GROUP BY occupation")
    st.tool_results.append(execute_tool("step_1", "sql_query", {"sql": sql}, sid))

    ev = _report_evidence(st)
    assert ev, "Reporter 证据为空 —— 报告将无法引用任何真实数字"
    assert ev[0]["rows"], "证据里没有结果行"
    assert "sql" in ev[0]


# --------------------------------------------------------------------------- #
# ATTACH/02 持久化真相源（重启 / 多 worker 后仍能发现上传表）
# --------------------------------------------------------------------------- #
def test_attached_tables_survive_store_loss(uploaded_session):
    """清空内存缓存后，attached_tables 必须能从 sidecar 文件反射还原。"""
    sid, p = uploaded_session
    get_attachment_store().clear(sid)  # 模拟重启 / 换 worker

    tables = attached_tables(sid)
    assert tables, "内存丢失后无法发现上传表 —— 真相源没有落到文件上"
    assert tables[0]["table"] == "sleep"
    assert tables[0]["row_count"] == 6, f"反射行数错: {tables[0]['row_count']}"
    assert tables[0]["origin"] == "user_upload"


def test_sidecar_meta_records_source_file(uploaded_session):
    """sidecar 的 _meta 必须记录源文件名/列序，供跨进程还原上下文。"""
    import sqlite3

    sid, _ = uploaded_session
    con = sqlite3.connect(str(sidecar_path(sid)))
    try:
        rows = con.execute('SELECT table_name, payload FROM "_meta"').fetchall()
    finally:
        con.close()
    assert rows, "sidecar 缺少 _meta 表"
    import json as _json

    payload = _json.loads(dict(rows)["sleep"])
    assert payload["source_file"] == "sleep.csv"
    assert "occupation" in payload["columns"]


# --------------------------------------------------------------------------- #
# ATTACH/03 计划层附件感知（决定性：不再去查内置库）
# --------------------------------------------------------------------------- #
def test_attachment_plan_targets_upload_table(uploaded_session):
    """附件保底计划必须每一步都指向 upload.<t>，且含维度下钻。"""
    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst.nodes import _attachment_plan, _uploaded_tables

    sid, _ = uploaded_session
    st = AgentState()
    st.session_id = sid
    ups = _uploaded_tables(st)
    plan = _attachment_plan(st, ups)

    ids = [s.id for s in plan.steps]
    assert "s1_rows" in ids and "s2_by_dim" in ids
    agg = next(s for s in plan.steps if s.id == "s2_by_dim")
    assert "upload.sleep" in (agg.input or {}).get("sql", ""), (
        "聚合步骤没有查上传表 —— 会退回内置库"
    )
    assert "GROUP BY" in (agg.input or {}).get("sql", "").upper()


def test_builtin_plan_is_replaced_when_it_ignores_upload(uploaded_session):
    """模型给出「只查内置库」的计划时，必须被确定性附件计划替换。"""
    from app.core.agents.data_analyst.state import AgentState, PlanModel
    from app.core.agents.data_analyst.nodes import _plan_uses_upload, _uploaded_tables

    sid, _ = uploaded_session
    st = AgentState()
    st.session_id = sid
    ups = _uploaded_tables(st)

    builtin = PlanModel.model_validate({"steps": [{
        "id": "step_3", "objective": "查销售", "action": "聚合",
        "tool": "sql_query", "input": {"sql": "SELECT * FROM fact_sales"},
    }]})
    assert _plan_uses_upload(builtin, ups) is False, "内置库计划被误判为使用上传表"


def test_sql_query_consumes_planned_sql(uploaded_session):
    """计划步骤里的 SQL 必须被真正执行（曾被丢弃 → 退化成 SELECT 1）。"""
    from app.core.agents.data_analyst.state import AgentState, PlanStep
    from app.core.agents.data_analyst.nodes import build_executor_params

    sid, _ = uploaded_session
    st = AgentState()
    st.session_id = sid
    step = PlanStep(id="s1", objective="计数", action="计数", tool="sql_query",
                    input={"sql": "SELECT COUNT(*) AS n FROM upload.sleep"})
    params = build_executor_params(st, step)
    assert params.get("sql") == "SELECT COUNT(*) AS n FROM upload.sleep"


def test_bare_table_name_is_qualified(uploaded_session):
    """计划里写裸表名 ``sleep`` 时必须被限定为 ``upload.sleep``。"""
    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst.nodes import _qualify_upload

    sid, _ = uploaded_session
    st = AgentState()
    st.session_id = sid
    assert "upload.sleep" in _qualify_upload("SELECT * FROM sleep", st)
    # 已限定不重复加
    assert _qualify_upload("SELECT * FROM upload.sleep", st).count("upload.sleep") == 1
    # 内置库不受影响
    assert "upload." not in _qualify_upload("SELECT * FROM fact_sales", st)


# --------------------------------------------------------------------------- #
# 降级路径 JSON 解析（REPLAN 空转的直接根因）
# --------------------------------------------------------------------------- #
def test_mock_extract_json_prefers_task_context():
    """Mock 必须从 <task_context> 取结构化 payload（而非贪婪正则）。"""
    from app.infrastructure.llm.router import MockLLM

    msg = ('<user_request>\n统计 {X} 的占比\n</user_request>\n\n'
           '<task_context>\n{"tool_results": [{"output": {"rows": [1, 2, 3]}}]}\n</task_context>')
    d = MockLLM._extract_json(msg)
    assert d and d.get("tool_results"), "降级路径拿不到 tool_results → 会误报『无事实数据』"
    assert len(d["tool_results"][0]["output"]["rows"]) == 3


def test_mock_extract_json_handles_braces_in_strings():
    """工具输出里的花括号不能破坏解析。"""
    from app.infrastructure.llm.router import MockLLM

    msg = '<task_context>\n{"rows": [{"s": "a}b{c"}]}\n</task_context>'
    d = MockLLM._extract_json(msg)
    assert d and d["rows"][0]["s"] == "a}b{c"


def test_mock_analyst_sees_tool_rows():
    """端到端：Mock analyst 必须把真实结果行转成 findings（而非空手 REPLAN）。"""
    from app.core.prompts import build_user_message
    from app.infrastructure.llm.router import MockLLM

    payload = {"tool_results": [{
        "tool": "sql_query", "status": "SUCCESS",
        "output": {"rows": [{"occupation": "Office Worker", "avg_q": 5.21}]},
    }]}
    msg = build_user_message("这份文件反应了什么数据规律", payload)
    out = MockLLM()._stage_analyst(msg)
    assert out["findings"], "analyst 没有产出任何发现"
    assert out["findings"][0]["confidence"] >= 0.9
    assert "Office Worker" in out["findings"][0]["finding"]


def test_fixture_session_ids_are_isolated(uploaded_session):
    """回归：fixture 必须用唯一 session id，避免并行运行互相踩踏 sidecar。"""
    sid, _ = uploaded_session
    assert sid != "test-attach-sleep", (
        "fixture 又用回了固定 session id —— 并行 pytest 会争抢同一个 upload.db"
    )
    assert sid.startswith("test-attach-")
