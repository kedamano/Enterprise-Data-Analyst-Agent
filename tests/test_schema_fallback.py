"""Schema-search keyword fallback — a zero-hit keyword must not leave the
downstream executor (dataset_profile / sql_query) without any table.

Contract:
* English keyword that matches → normal filter (no fallback marker).
* Chinese keyword / any keyword with zero hits (schema metadata is English) →
  fall back to a few candidate tables so profiling/querying still proceeds.
* End-to-end: the executor's deterministic param builder then yields a real
  table name (not an empty param / ``SELECT 1``).
"""
from __future__ import annotations

from app.core.agents.data_analyst.nodes import build_executor_params, _first_table
from app.core.agents.data_analyst.state import AgentState, ContextModel
from app.core.tools import execute_tool


def test_english_keyword_matches_without_fallback():
    res = execute_tool("s", "schema_search", {"keyword": "revenue"}, "sf_schema")
    assert res.status == "SUCCESS", res.error
    assert any("fact_sales" in t.get("table", "") for t in res.output.get("tables", []))
    assert not any("match" in t for t in res.output["tables"]), "命中时不应有 fallback 标记"


def test_zero_hit_keyword_falls_back_to_candidates():
    res = execute_tool("s", "schema_search", {"keyword": "营收"}, "sf_schema2")
    assert res.status == "SUCCESS", res.error
    tables = res.output.get("tables", [])
    assert tables, "零命中关键词也应返回候选表而非空"
    assert tables[0].get("match") == "keyword_fallback"


def test_executor_builds_real_params_after_fallback():
    """复现 demo 缺陷：中文指标 → schema 零命中 → dataset_profile 拿不到 table。"""
    st = AgentState(session_id="sf_demo", user_query="分析最近营收变化的原因")
    st.context = ContextModel(objective="营收变化原因", metrics=["营收"], dimensions=["region"])
    r1 = execute_tool("1", "schema_search", {"keyword": "营收"}, "sf_demo")
    assert r1.status == "SUCCESS" and r1.output.get("tables"), "零命中须回退到候选表"
    st.tool_results = [r1]

    table, _ = _first_table(st)
    assert table, "schema 回退后应能取到真实表名"

    profile_params = build_executor_params(
        st, type("PS", (), {"tool": "dataset_profile", "id": "s2"})())
    assert profile_params.get("table"), f"profile 应带 table，实际 {profile_params}"
    r2 = execute_tool("2", "dataset_profile", profile_params, "sf_demo")
    assert r2.status == "SUCCESS", r2.error

    sql_params = build_executor_params(
        st, type("PS", (), {"tool": "sql_query", "id": "s3"})())
    assert "SELECT 1" not in sql_params.get("sql", ""), "不应退化为 SELECT 1"
    assert table in sql_params["sql"]
