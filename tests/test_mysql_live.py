"""LIVE MySQL 集成测试。

与 ``test_pg_live.py`` 同构：仅在 MySQL 真正可连接（端口可达 **且** 凭据正确）时
才运行；否则直接 ``skip``，绝不让 CI 因缺库而红。

启用方式（所需环境变量，通常写在 CI secret / .env）：
    MYSQL_DSN=mysql+pymysql://<user>:<pass>@<host>:<port>/<db>

本地自建实例实跑示例（无需外部依赖）：
    # 1) 用 MySQL zip 包初始化一个免密临时实例（ASCII 路径，避免中文路径报错）
    mysqld --defaults-file=<tmp>/my.ini --initialize-insecure --console
    mysqld --defaults-file=<tmp>/my.ini --console          # 监听 3307
    mysql -h127.0.0.1 -P3307 -uroot -e "CREATE DATABASE da_agent"
    # 2) 指向它跑 live 测试
    MYSQL_DSN='mysql+pymysql://root:@127.0.0.1:3307/da_agent' pytest tests/test_mysql_live.py -q

注：``--initialize-insecure`` 会创建**空密码** root；实例仅绑 127.0.0.1，用完即删。
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

_DSN = os.getenv("MYSQL_DSN", "mysql+pymysql://root:root@localhost:3306/da_agent")


def _host_port() -> tuple[str, int]:
    """从 DSN 解析 host/port（不再硬编码 3306，非默认端口实例也能探测）。"""
    try:
        from sqlalchemy.engine.url import make_url

        u = make_url(_DSN)
        return (u.host or "localhost", int(u.port or 3306))
    except Exception:
        return ("localhost", 3306)


def _port_open() -> bool:
    host, port = _host_port()
    try:
        s = socket.create_connection((host, port), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


@pytest.fixture
def mysql(request):
    """真实连接 MySQL；连不上（端口不通或无凭据）就跳过。"""
    if not _port_open():
        pytest.skip("MySQL 端口不可达（跳过 live 测试）")
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(_DSN, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # 端口通但无凭据 / 驱动缺失 → 跳过而非失败
        pytest.skip(f"MySQL 无法连接/鉴权（跳过 live 测试）: {exc}")
    # 建一张专用测试表（干净、独立）
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS da_live_check"))
        conn.execute(text(
            "CREATE TABLE da_live_check (region VARCHAR(32), amount DOUBLE, secret_col VARCHAR(32))"
        ))
        conn.execute(text(
            "INSERT INTO da_live_check VALUES ('华东',1200.0,'s1'),('华北',680.0,'s2'),('华南',540.0,'s3')"
        ))
    yield eng
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS da_live_check"))


def test_mysql_real_query(monkeypatch, mysql):
    """真实 MySQL 上跑通：命名源解析 → 只读查询返回行。"""
    monkeypatch.setenv("DATA_SOURCES",
                       '[{"name":"mysql","url":"%s","dialect":"mysql"}]' % _DSN)
    from app.config import get_settings
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    try:
        res = sql_tool.run({"sql": "SELECT region, amount FROM da_live_check", "source": "mysql"})
        assert res["ok"] is True, res.get("error")
        assert res["row_count"] == 3
    finally:
        get_settings.cache_clear()


def test_mysql_readonly_guard(monkeypatch, mysql):
    """真实 MySQL 上验证只读守卫拦截 DROP。"""
    monkeypatch.setenv("DATA_SOURCES",
                       '[{"name":"mysql","url":"%s","dialect":"mysql"}]' % _DSN)
    from app.config import get_settings
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    try:
        res = sql_tool.run({"sql": "DROP TABLE da_live_check", "source": "mysql"})
        assert res["ok"] is False
        assert "只读" in res["error"]
    finally:
        get_settings.cache_clear()


def _use_mysql_source(monkeypatch) -> None:
    """把命名源指向真实 MySQL 并清 settings 缓存。"""
    import json

    monkeypatch.setenv("DATA_SOURCES",
                       json.dumps([{"name": "mysql", "url": _DSN, "dialect": "mysql"}]))
    from app.config import get_settings

    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 文件/副作用原语：真实 MySQL 上必须被**执行前**拦下（不落到服务端）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql,label", [
    ("SELECT * FROM da_live_check INTO OUTFILE '/tmp/leak.csv'", "INTO OUTFILE 文件写"),
    ("SELECT * FROM da_live_check INTO/*x*/OUTFILE '/tmp/leak.csv'", "注释拆分绕过"),
    ("SELECT LOAD_FILE('/etc/passwd')", "LOAD_FILE 文件读"),
    ("SELECT SLEEP(5)", "SLEEP DoS"),
    ("SELECT BENCHMARK(10000000, MD5('a'))", "BENCHMARK DoS"),
    ("SELECT * FROM da_live_check INTO @x", "变量赋值"),
])
def test_mysql_side_effect_blocked_before_execution(monkeypatch, mysql, sql, label):
    """这些语句在真实 MySQL 上"合法"→ 若守卫漏检就会真的生效；必须被拦在守卫层。"""
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.tools import sql_tool

    try:
        res = sql_tool.run({"sql": sql, "source": "mysql"})
        assert res["ok"] is False, f"{label} 未被拦截: {res}"
        assert "只读" in res["error"], res["error"]
    finally:
        get_settings.cache_clear()


def test_mysql_statement_initial_side_effects_blocked(monkeypatch, mysql):
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.tools import sql_tool

    try:
        for sql in ["LOCK TABLES da_live_check READ",
                    "SET GLOBAL general_log=1",
                    "HANDLER da_live_check OPEN"]:
            res = sql_tool.run({"sql": sql, "source": "mysql"})
            assert res["ok"] is False, f"未被拦截: {sql}"
            assert "只读" in res["error"]
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 跨方言：真实 MySQL 上跑聚合（LIMIT/GROUP BY/反引号标识符）
# --------------------------------------------------------------------------- #
def test_mysql_cross_dialect_aggregate(monkeypatch, mysql):
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    try:
        res = sql_tool.run({
            "sql": "SELECT `region`, SUM(`amount`) AS total FROM da_live_check "
                   "GROUP BY `region` ORDER BY total DESC LIMIT 2",
            "source": "mysql",
        })
        assert res["ok"] is True, res.get("error")
        assert res["row_count"] == 2
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 用户级数据权限：真实 MySQL 上验行级过滤 / 表白名单 / 列黑名单
# --------------------------------------------------------------------------- #
def test_mysql_row_filter_end_to_end(monkeypatch, mysql):
    """行级过滤：只应看到 amount >= 1000 的行（样本 3 行 → 华东 1200 共 1 行）。"""
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.security import auth
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    tok = auth.set_current(auth.Principal(
        user_id="u1", roles=["analyst"],
        row_filters={"da_live_check": "amount >= 1000"},
    ))
    try:
        res = sql_tool.run({"sql": "SELECT region, amount FROM da_live_check", "source": "mysql"})
        assert res["ok"] is True, res.get("error")
        assert res["row_count"] == 1, res
        assert res["rows"][0]["region"] == "华东"
    finally:
        auth.reset_current(tok)
        get_settings.cache_clear()


def test_mysql_table_whitelist_denies(monkeypatch, mysql):
    """表级白名单：不在白名单的表 → 执行前拒绝。"""
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.security import auth
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    tok = auth.set_current(auth.Principal(
        user_id="u2", roles=["analyst"], allowed_tables=["dim_region"],
    ))
    try:
        res = sql_tool.run({"sql": "SELECT * FROM da_live_check", "source": "mysql"})
        assert res["ok"] is False
        assert "无权访问表" in res["error"]
    finally:
        auth.reset_current(tok)
        get_settings.cache_clear()


def test_mysql_denied_column_blocked(monkeypatch, mysql):
    """列级黑名单：命中即拒。"""
    _use_mysql_source(monkeypatch)
    from app.config import get_settings
    from app.core.security import auth
    from app.core.tools import sql_tool

    get_settings.cache_clear()
    tok = auth.set_current(auth.Principal(
        user_id="u3", roles=["analyst"], denied_columns=["secret_col"],
    ))
    try:
        res = sql_tool.run({"sql": "SELECT secret_col FROM da_live_check", "source": "mysql"})
        assert res["ok"] is False
        assert "无权访问列" in res["error"]
    finally:
        auth.reset_current(tok)
        get_settings.cache_clear()

