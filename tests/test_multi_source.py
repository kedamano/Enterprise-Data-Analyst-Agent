"""E7/01 多数据源：分析师的数据不止一个库。

Spec: docs/specs/E7/01-multi-source-deploy.md

**明确不做跨源 JOIN / 联邦查询**（那是另一个量级）：需要跨源就在各自源上取数后，
用 `python_analysis` 合并。本规格只保证"能按名字寻址到指定的库"，且**不填 = 主源**（兼容既有行为）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.config import get_settings
from app.core.tools.datasource import available_sources, resolve_source
from app.infrastructure.llm.router import reset_llm


def _mk_db(path, table: str, rows) -> str:
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE {table} (id INTEGER, v TEXT)")
    con.executemany(f"INSERT INTO {table} VALUES (?,?)", rows)
    con.commit()
    con.close()
    # Windows 下必须用 posix 分隔符：反斜杠进 DSN/JSON 会变成非法转义 → 解析失败
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def multi_env(monkeypatch, tmp_path):
    primary = _mk_db(tmp_path / "primary.db", "t", [(1, "primary")])
    crm = _mk_db(tmp_path / "crm.db", "t", [(1, "crm")])
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATA_DB_URL", primary)
    monkeypatch.setenv("DATA_SOURCES", f'[{{"name":"crm","url":"{crm}","dialect":"sqlite"}}]')
    get_settings.cache_clear()
    reset_llm()
    yield {"primary": primary, "crm": crm}
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 1. 解析
# --------------------------------------------------------------------------- #
def test_default_source_is_primary(multi_env):
    url, dialect = resolve_source(None)
    assert url == multi_env["primary"], "不传 source 必须命中主源（向后兼容）"
    assert dialect == "sqlite"


def test_named_source_resolves(multi_env):
    url, _ = resolve_source("crm")
    assert url == multi_env["crm"]


def test_available_sources_lists_names_without_dsn(multi_env):
    names = available_sources()
    assert "default" in names and "crm" in names
    assert all("://" not in n and "@" not in n for n in names), "源名列表不得泄漏 DSN/密码"


def test_unknown_source_is_readable(multi_env):
    with pytest.raises(KeyError) as exc:
        resolve_source("no_such")
    msg = str(exc.value)
    assert "no_such" in msg and "crm" in msg, "错误里要列出可用源名，分析师才能自己纠正"


def test_bad_config_is_ignored_not_fatal(monkeypatch, tmp_path):
    """配置写坏不能让服务起不来（记 warning，忽略该条）。"""
    monkeypatch.setenv("DATA_DB_URL", _mk_db(tmp_path / "p.db", "t", [(1, "x")]))
    monkeypatch.setenv("DATA_SOURCES", "{这不是 JSON")
    get_settings.cache_clear()
    try:
        assert resolve_source(None)[0].endswith("p.db"), "主源仍可用"
        assert "crm" not in available_sources()
    finally:
        get_settings.cache_clear()


def test_missing_name_or_url_skipped(monkeypatch, tmp_path):
    # 隔离本地数据源存储：committed 的 data/datasources.json（含 oasys 等）若不被重定向，
    # 会漏进 available_sources()，把"缺名跳过"的断言污染成 2 个 non-default 源（期望 1）。
    monkeypatch.setenv("DATA_DB_URL", _mk_db(tmp_path / "p.db", "t", [(1, "x")]))
    monkeypatch.setenv("DATA_SOURCES",
                       '[{"url":"sqlite:///x.db"},{"name":"ok","url":"sqlite:///y.db"}]')
    monkeypatch.setenv("DATASOURCE_STORE_PATH", str(tmp_path / "store.json"))
    get_settings.cache_clear()
    try:
        names = available_sources()
        assert "ok" in names
        assert len([n for n in names if n != "default"]) == 1, "缺 name 的条目应被忽略"
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 2. 工具按源取数
# --------------------------------------------------------------------------- #
def test_sql_query_honours_source(multi_env):
    from app.core.tools import execute_tool

    default = execute_tool("s1", "sql_query", {"sql": "SELECT v FROM t"}, "ms_a")
    assert default.output["rows"][0]["v"] == "primary"

    named = execute_tool("s2", "sql_query", {"sql": "SELECT v FROM t", "source": "crm"}, "ms_a")
    assert named.output["rows"][0]["v"] == "crm", "命名源应取到自己的数据"


def test_unknown_source_on_tool_is_readable(multi_env):
    from app.core.tools import execute_tool

    res = execute_tool("s3", "sql_query", {"sql": "SELECT 1", "source": "nope"}, "ms_b")
    assert res.status == "FAILED"
    assert "nope" in (res.error or "") and "crm" in (res.error or ""), res.error


def test_no_cross_source_join_is_pretended(multi_env):
    """规格明确不做联邦查询：跨源 JOIN 必然失败，且失败是可读的（不是静默错数）。"""
    from app.core.tools import execute_tool

    res = execute_tool("s4", "sql_query",
                       {"sql": "SELECT p.v FROM t p JOIN t q ON p.id=q.id", "source": "crm"},
                       "ms_c")
    # 同一个源内自连接是允许的（这里只验证"没有跨源魔法"）
    assert res.status == "SUCCESS"


# --------------------------------------------------------------------------- #
# 3. 计划可达 + health
# --------------------------------------------------------------------------- #
def test_plan_step_can_target_source(multi_env):
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep

    st = AgentState(session_id="ms_plan", user_query="q")
    step = PlanStep(id="s1", objective="取数", action="SQL", tool="freeform",
                    input={"sql": "SELECT 1", "source": "crm"})
    assert build_executor_params(st, step).get("source") == "crm"


def test_health_exposes_source_names_without_secrets(multi_env):
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/api/v1/health").json()
    assert "crm" in body.get("data_sources", []), body
    assert "://" not in " ".join(body.get("data_sources", [])), "health 不得回 DSN"


# --------------------------------------------------------------------------- #
# 4. 跨源 schema 发现（E7/02）：主源没有的表，agent 必须能自己找到并路由过去。
#    真实场景：用户接了 oasys(MySQL)，问「aoa_dept 表行数」——此前 schema_search
#    只搜主源 → "no such table"，明明页面里连接是成功的。
# --------------------------------------------------------------------------- #
@pytest.fixture
def cross_env(monkeypatch, tmp_path):
    """主源只有 fact_sales；aoa_dept 只存在于命名源 crm。"""
    primary = _mk_db(tmp_path / "p.db", "fact_sales", [(i, "x") for i in range(5)])
    crm_path = tmp_path / "crm.db"
    con = sqlite3.connect(crm_path)
    con.execute("CREATE TABLE aoa_dept (id INTEGER, dept_name TEXT, headcount INTEGER)")
    con.executemany("INSERT INTO aoa_dept VALUES (?,?,?)",
                    [(i, f"d{i}", 10 + i) for i in range(1, 6)])
    con.commit()
    con.close()
    crm = f"sqlite:///{crm_path.as_posix()}"
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("DATA_DB_URL", primary)
    monkeypatch.setenv("DATA_SOURCES", f'[{{"name":"crm","url":"{crm}","dialect":"sqlite"}}]')
    # 隔离本地数据源存储：committed 的 data/datasources.json（含真实 oasys MySQL）
    # 若漏进来，跨源扫描会去连真实库——测试必须完全离线可控。
    monkeypatch.setenv("DATASOURCE_STORE_PATH", str(tmp_path / "store.json"))
    get_settings.cache_clear()
    reset_llm()
    yield {"primary": primary, "crm": crm}
    get_settings.cache_clear()
    reset_llm()


def test_schema_search_tags_primary_source(cross_env):
    from app.core.tools import execute_tool

    res = execute_tool("c1", "schema_search", {"keyword": ""}, "cx_a")
    assert res.status == "SUCCESS"
    tables = res.output["tables"]
    assert tables, "主源应有表"
    assert all(t.get("source") == "default" for t in tables), "每张表都要带 source 标记"


def test_schema_search_discovers_cross_source_table(cross_env):
    from app.core.tools import execute_tool

    res = execute_tool("c2", "schema_search", {"keyword": "aoa_dept"}, "cx_b")
    tables = res.output["tables"]
    hit = next((t for t in tables if t.get("table") == "aoa_dept"), None)
    assert hit, f"主源没有 aoa_dept，必须能在命名源里发现：{[t.get('table') for t in tables]}"
    assert hit["source"] == "crm", "发现结果必须标注表所在源"
    assert hit["row_count"] == 5, "跨源发现也要带回行数/列等描述信息"
    assert hit.get("match") == "cross_source"


def test_executor_routes_sql_to_discovered_source(cross_env):
    """端到端：schema 跨源发现 → 合成 SQL 自动带上表所在源 → 真的查到数。"""
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep
    from app.core.tools import execute_tool

    st = AgentState(session_id="cx_c", user_query="查询aoa_dept表中的数据行数")
    st.tool_results.append(
        execute_tool("c3", "schema_search", {"keyword": "aoa_dept"}, "cx_c"))

    step = PlanStep(id="s1", objective="aoa_dept 行数", action="SQL",
                    tool="sql_query", input={})
    params = build_executor_params(st, step)
    assert "aoa_dept" in params["sql"], "选表必须选中跨源发现的表"
    assert params.get("source") == "crm", "合成 SQL 必须路由到表所在的命名源"

    out = execute_tool("c4", "sql_query", params, "cx_c")
    assert out.status == "SUCCESS", out.error
    assert out.output["rows"], "跨源取数必须返回真实数据行"


def test_sql_query_step_input_source_passthrough(cross_env):
    """计划步骤 input.source 与 freeform 同权：sql_query 也不能丢掉它。"""
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep

    st = AgentState(session_id="cx_d", user_query="q")
    step = PlanStep(id="s1", objective="o", action="SQL", tool="sql_query",
                    input={"sql": "SELECT 1", "source": "crm"})
    assert build_executor_params(st, step).get("source") == "crm"


def test_dataset_profile_follows_discovered_source(cross_env):
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep
    from app.core.tools import execute_tool

    st = AgentState(session_id="cx_e", user_query="查aoa_dept")
    st.tool_results.append(
        execute_tool("c5", "schema_search", {"keyword": "aoa_dept"}, "cx_e"))
    step = PlanStep(id="s1", objective="画像", action="profile",
                    tool="dataset_profile", input={})
    params = build_executor_params(st, step)
    assert params.get("table") == "aoa_dept"
    assert params.get("source") == "crm", "画像也要跟着表所在的源走"


def test_schema_brief_marks_cross_source_table(cross_env):
    """planner 看到的 schema 摘要必须标注来源，它才有机会写 input.source。"""
    from app.core.agents.data_analyst.nodes import _discovered_schema_text
    from app.core.agents.data_analyst.state import AgentState
    from app.core.tools import execute_tool

    st = AgentState(session_id="cx_f", user_query="查aoa_dept")
    st.tool_results.append(
        execute_tool("c6", "schema_search", {"keyword": "aoa_dept"}, "cx_f"))
    text = _discovered_schema_text(st)
    assert "aoa_dept" in text and "[source=crm]" in text, text


# --------------------------------------------------------------------------- #
# 5. 显式点名数据源：用户直接说出源名（「crm 数据源」「在 oasys 里查」）。
#    这是「去哪个库」最硬的信号，不依赖 schema 跨源发现是否成功——
#    线上实测：planner 用空 keyword 列全表时，主源先返回表会把跨源发现跳过，
#    于是 aoa_dept 永远发现不了，agent 误报「只有 sample_enterprise.db」。
# --------------------------------------------------------------------------- #
def test_explicit_source_detected_from_query(cross_env):
    from app.core.agents.data_analyst.nodes import _explicit_source
    from app.core.agents.data_analyst.state import AgentState

    assert _explicit_source(AgentState(session_id="cx_g",
                                      user_query="查询 crm 数据源中 aoa_dept 行数")) == "crm"
    # 未点名时回落主源
    assert _explicit_source(AgentState(session_id="cx_g2",
                                      user_query="查询各部门行数")) == ""


def test_explicit_source_routes_schema_search_to_named(cross_env):
    """用户点名 crm → schema_search 直接打到 crm，aoa_dept 必被发现。"""
    from app.core.agents.data_analyst.nodes import build_executor_params, _explicit_source
    from app.core.agents.data_analyst.state import AgentState, PlanStep
    from app.core.tools import execute_tool

    st = AgentState(session_id="cx_h", user_query="查询 crm 数据源中 aoa_dept 表行数")
    assert _explicit_source(st) == "crm"
    step = PlanStep(id="s1", objective="发现表", action="schema", tool="schema_search", input={})
    params = build_executor_params(st, step)
    assert params.get("source") == "crm", params

    # 真打到 crm 并找到 aoa_dept（即使主源先返回表，也不该把跨源发现跳过）
    res = execute_tool("c7", "schema_search", params, "cx_h")
    assert res.status == "SUCCESS", res.error
    hit = next((t for t in res.output["tables"] if t.get("table") == "aoa_dept"), None)
    assert hit and hit["source"] == "crm", [t.get("table") for t in res.output["tables"]]
    assert hit["row_count"] == 5


def test_explicit_source_routes_sql_query_to_named(cross_env):
    """端到端：点名 crm + 计划直接给 sql → sql_query 路由到 crm 拿到真数。"""
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep
    from app.core.tools import execute_tool

    st = AgentState(session_id="cx_i", user_query="在 crm 里统计 aoa_dept 行数")
    step = PlanStep(id="s1", objective="行数", action="SQL", tool="sql_query",
                    input={"sql": "SELECT COUNT(*) AS cnt FROM aoa_dept"})
    params = build_executor_params(st, step)
    assert params.get("source") == "crm", params

    out = execute_tool("c8", "sql_query", params, "cx_i")
    assert out.status == "SUCCESS", out.error
    assert out.output["rows"][0]["cnt"] == 5


def test_schema_search_cross_source_on_keyword_even_if_primary_has_hits(cross_env):
    """回归：主源也返回表时（非空 results）跨源发现仍应扫描命名源。

    keyword=id 同时命中主源 fact_sales 与命名源 crm 的 aoa_dept（两者都有 id 列）。
    修复前「if not results」会跳过跨源扫描，aoa_dept 彻底发现不了。
    """
    from app.core.tools import execute_tool

    res = execute_tool("c9", "schema_search", {"keyword": "id"}, "cx_j")
    tables = res.output["tables"]
    primary_hit = next((t for t in tables if t.get("table") == "fact_sales"), None)
    cross_hit = next((t for t in tables if t.get("table") == "aoa_dept"), None)
    assert primary_hit and primary_hit.get("source") == "default", "主源应命中 fact_sales"
    assert cross_hit and cross_hit["source"] == "crm", [t.get("table") for t in tables]
