"""真实 RDBMS 集成测试（SQLite 后端）。

为什么是 SQLite：本机 PG / MySQL / Milvus 服务不全（PG、Milvus 无服务；
MySQL 服务在线但本会话无凭据）。SQLite 是真实的关系型引擎，且 app 的
``DATA_SOURCES`` + SQLAlchemy 跨方言 + 只读守卫 + 标识符白名单整条链路都支持它，
因此本测试在「真实数据库引擎」上跑通整条数据层，验证的不是 mock。

覆盖：
- 跨方言解析（sqlite / postgres / mysql）
- 命名源解析（默认源 + DATA_SOURCES 多源）
- 真实查询返回行
- 只读守卫拦截 DROP / DELETE / INSERT
- 多语句拦截
- 用户级数据权限：表级白名单、列级黑名单、行级过滤
- 行数上限自动追加 LIMIT
"""
from __future__ import annotations

import os
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.config import get_settings
from app.core.security import auth
from app.core.security.data_guard import apply_row_filters, guard_sql
from app.core.tools import sql_tool
from app.core.tools.datasource import _guess_dialect, resolve_source, sources


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """建一个真实 SQLite 库，注入为默认数据源。"""
    db = tmp_path / "da_live.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE fact_sales (region TEXT, amount REAL, secret_col TEXT)"
    )
    conn.execute(
        "INSERT INTO fact_sales VALUES ('华东', 1200.0, 's1'), "
        "('华北', 680.0, 's2'), ('华南', 540.0, 's3')"
    )
    conn.commit()
    conn.close()
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATA_DB_URL", url)
    # DATA_SOURCES 里再挂一个命名源，验证多源解析（用 json.dumps 正确转义 Windows 路径）
    monkeypatch.setenv(
        "DATA_SOURCES",
        json.dumps([{"name": "secondary", "url": url}]),
    )
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 跨方言 + 命名源解析（纯函数，无需 DB）
# --------------------------------------------------------------------------- #
def test_guess_dialect():
    assert _guess_dialect("sqlite:///x.db") == "sqlite"
    assert _guess_dialect("postgresql://u:p@h/db") == "postgresql"
    assert _guess_dialect("postgres://u:p@h/db") == "postgresql"
    assert _guess_dialect("mysql+pymysql://u:p@h/db") == "mysql"


def test_resolve_default_source(sqlite_db):
    url, dialect = resolve_source(None)
    assert url == sqlite_db
    assert dialect == "sqlite"


def test_resolve_named_source(sqlite_db):
    url, dialect = resolve_source("secondary")
    assert url == sqlite_db
    assert dialect == "sqlite"


def test_resolve_unknown_source_raises(sqlite_db):
    with pytest.raises(KeyError):
        resolve_source("no_such_source")


def test_sources_lists_all(sqlite_db):
    names = list(sources().keys())
    assert "default" in names
    assert "secondary" in names


# --------------------------------------------------------------------------- #
# 真实查询 + 守卫（跑在真实 SQLite 引擎上）
# --------------------------------------------------------------------------- #
def test_real_query_returns_rows(sqlite_db):
    res = sql_tool.run({"sql": "SELECT region, amount FROM fact_sales ORDER BY amount DESC"})
    assert res["ok"] is True, res.get("error")
    assert res["row_count"] == 3
    assert {r["region"] for r in res["rows"]} == {"华东", "华北", "华南"}


def test_readonly_blocks_drop(sqlite_db):
    res = sql_tool.run({"sql": "DROP TABLE fact_sales"})
    assert res["ok"] is False
    assert "只读" in res["error"]


def test_readonly_blocks_insert(sqlite_db):
    res = sql_tool.run({"sql": "INSERT INTO fact_sales VALUES ('x', 1.0, 'y')"})
    assert res["ok"] is False
    assert "只读" in res["error"]


def test_readonly_blocks_delete(sqlite_db):
    res = sql_tool.run({"sql": "DELETE FROM fact_sales WHERE region='华东'"})
    assert res["ok"] is False
    assert "只读" in res["error"]


def test_multistatement_blocked(sqlite_db):
    res = sql_tool.run({"sql": "SELECT 1; SELECT 2"})
    assert res["ok"] is False
    assert "单条语句" in res["error"]


def test_row_cap_appends_limit(sqlite_db, monkeypatch):
    monkeypatch.setenv("SQL_MAX_ROWS", "2")
    get_settings.cache_clear()
    try:
        res = sql_tool.run({"sql": "SELECT region FROM fact_sales"})
        assert res["ok"] is True
        assert res["row_count"] == 2  # 超过上限被 LIMIT 截断
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 用户级数据权限（表级白名单 / 列级黑名单 / 行级过滤）
# --------------------------------------------------------------------------- #
def test_table_whitelist_allows(sqlite_db):
    tok = auth.set_current(auth.Principal(user_id="u1", allowed_tables=["fact_sales"]))
    try:
        res = sql_tool.run({"sql": "SELECT region FROM fact_sales"})
        assert res["ok"] is True, res.get("error")
    finally:
        auth.reset_current(tok)


def test_table_whitelist_blocks_unlisted(sqlite_db):
    tok = auth.set_current(auth.Principal(user_id="u2", allowed_tables=["other_table"]))
    try:
        res = sql_tool.run({"sql": "SELECT region FROM fact_sales"})
        assert res["ok"] is False
        assert "无权访问表 fact_sales" in res["error"]
    finally:
        auth.reset_current(tok)


def test_column_blacklist_blocks(sqlite_db):
    tok = auth.set_current(auth.Principal(user_id="u3", denied_columns=["secret_col"]))
    try:
        res = sql_tool.run({"sql": "SELECT region, secret_col FROM fact_sales"})
        assert res["ok"] is False
        assert "secret_col" in res["error"]
    finally:
        auth.reset_current(tok)


def test_column_blacklist_allows_other_columns(sqlite_db):
    tok = auth.set_current(auth.Principal(user_id="u4", denied_columns=["secret_col"]))
    try:
        res = sql_tool.run({"sql": "SELECT region, amount FROM fact_sales"})
        assert res["ok"] is True, res.get("error")
    finally:
        auth.reset_current(tok)


def test_row_filter_appends_predicate():
    sql = "SELECT region, amount FROM fact_sales"
    principal = auth.Principal(user_id="u5", row_filters={"fact_sales": "region='华东'"})
    new_sql, applied = apply_row_filters(sql, principal)
    assert applied == ["fact_sales"]
    assert "region='华东'" in new_sql
    assert new_sql.strip().startswith("SELECT")


def test_row_filter_no_clobber_existing_where():
    sql = "SELECT region FROM fact_sales WHERE amount > 100"
    principal = auth.Principal(user_id="u6", row_filters={"fact_sales": "region='华北'"})
    new_sql, _ = apply_row_filters(sql, principal)
    assert "amount > 100" in new_sql
    assert "region='华北'" in new_sql


def test_guard_sql_no_principal_is_open():
    assert guard_sql("SELECT 1 FROM fact_sales", None) is None
