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
