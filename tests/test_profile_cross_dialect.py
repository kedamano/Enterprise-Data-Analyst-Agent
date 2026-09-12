"""``dataset_profile`` 的跨方言回归 —— 三个**只在真库上才暴露**的坑。

背景：内置样例库是 SQLite，走 ``PRAGMA table_info`` 分支 + 双引号标识符，
所以下面这些缺陷在 ``test_e4_profile_quality.py`` 里**长期是绿的**，
直到把 ``DATA_DB_URL`` 指向真 PG / MySQL 才炸（见 docs/progress/live-validation.md）：

1. **``Row`` 不支持字符串下标**：取列名写 ``r["column_name"]`` 时，
   ``text()`` 查询返回的 Row 抛
   ``TypeError: tuple indices must be integers or slices, not str``。
2. **``Row._mapping`` 下标区分大小写**：即便改成 ``r._mapping["column_name"]``，
   **MySQL 的 information_schema 列名是大写 ``COLUMN_NAME``**（PG 是小写
   ``column_name``）→ MySQL 抛 ``Could not locate column in row for column
   'column_name'``。用例 ② 的做法是按**位置**取值，彻底绕开大小写折叠差异。
3. **标识符引号不能用 ANSI 双引号**：MySQL 默认 sql_mode 下 ``"..."`` 是
   **字符串字面量**而非标识符引号，``SELECT COUNT(*) FROM "bench_fact"``
   直接 ``ERROR 1064`` 语法错误 → 必须按方言用反引号。

offline 用例覆盖 1/2/3 的判定逻辑（无需服务端，CI 的 ``test`` job 也能跑）；
live 用例在 PG / MySQL 端口可达时**端到端**复跑 ``profile_tool.run``（CI 的
``live`` job 带 ``mysql:8.0`` service container）。
"""
from __future__ import annotations

import os
import socket
from functools import partial

import pytest

from app.config import get_settings
from app.core.tools import profile_tool

# --------------------------------------------------------------------------- #
# 工具：造真实 Row / 假连接
# --------------------------------------------------------------------------- #
def _row_with(key: str, value: str):
    """造一个**真实** SQLAlchemy ``Row``，其键名为 ``key``（可大写，模拟 MySQL）。"""
    from sqlalchemy import create_engine, text

    eng = create_engine("sqlite://")
    with eng.connect() as conn:
        return conn.execute(text(f"SELECT :v AS {key}"), {"v": value}).fetchone()


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """只够 ``_table_columns`` 用的假连接：把预造好的 Row 原样回吐。"""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


# --------------------------------------------------------------------------- #
# 坑 ③：标识符引号必须随方言（MySQL 反引号）
# --------------------------------------------------------------------------- #
def test_identifier_quote_is_backtick_on_mysql():
    assert profile_tool._q("bench_fact", "mysql") == "`bench_fact`", \
        "MySQL 下必须用反引号：双引号会被当成字符串字面量 → ERROR 1064"


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_identifier_quote_is_double_quote_elsewhere(dialect):
    assert profile_tool._q("bench_fact", dialect) == '"bench_fact"'


def test_query_built_for_mysql_uses_backticks():
    """整条 SQL 的引号都要跟着方言走（不能只改一处）。"""
    q = partial(profile_tool._q, dialect="mysql")
    sql = f"SELECT COUNT({q('revenue')}), COUNT(DISTINCT {q('revenue')}) FROM {q('bench_fact')}"
    assert sql == "SELECT COUNT(`revenue`), COUNT(DISTINCT `revenue`) FROM `bench_fact`"
    assert '"' not in sql, "MySQL 分支不得出现双引号"


def test_q_of_binds_to_connection_dialect():
    """`_q_of(conn)` 必须按**连接自己**的方言产引号，且互不干扰。

    并行执行器是裸 `ThreadPoolExecutor`（`ContextVar` 不传播），
    因此不能靠模块级“当前方言”全局——必须按连接绑定。
    """
    from sqlalchemy import create_engine

    sqlite_q = profile_tool._q_of(create_engine("sqlite://"))
    assert sqlite_q("t") == '"t"'

    class _FakeEngine:
        class dialect:  # noqa: N801
            name = "mysql"

    class _FakeConnEngine:
        engine = _FakeEngine()

    mysql_q = profile_tool._q_of(_FakeConnEngine())
    assert mysql_q("t") == "`t`"
    assert sqlite_q("t") == '"t"', "两个引用器必须互不影响（线程安全的要点）"


# --------------------------------------------------------------------------- #
# 坑 ① ②：取列名必须免疫 Row 的字符串下标/大小写差异
# --------------------------------------------------------------------------- #
def test_naive_string_indexing_really_fails():
    """钉住根因：旧写法在这种 Row 上**必定**抛错（谁改回去谁红）。"""
    row = _row_with("column_name", "sale_id")

    with pytest.raises(TypeError):
        row["column_name"]  # 坑 ①：Row 不支持字符串下标

    # 坑 ②：`_mapping` 下标区分大小写 —— 模拟 MySQL 的大写 COLUMN_NAME
    mysql_row = _row_with("COLUMN_NAME", "sale_id")
    assert mysql_row._mapping["COLUMN_NAME"] == "sale_id"      # 大写键可读
    with pytest.raises(Exception) as ei2:
        mysql_row._mapping["column_name"]                       # 小写键读不到
    assert "Could not locate column" in str(ei2.value), str(ei2.value)


def _table_columns(rows, table: str, dialect: str) -> list[str]:
    """用假连接喂入真实 Row，只验证 ``_table_columns`` 的取值/去重逻辑。"""
    from sqlalchemy import text

    return profile_tool._table_columns(_FakeConn(rows), text, table, dialect)


def test_table_columns_reads_uppercase_keys_mysql_shape():
    """MySQL 形状（大写 ``COLUMN_NAME``）：位置取值必须拿到正确列名与顺序。"""
    rows = [_row_with("COLUMN_NAME", "sale_id"),
            _row_with("COLUMN_NAME", "region_id"),
            _row_with("COLUMN_NAME", "revenue")]
    assert _table_columns(rows, "bench_fact", "mysql") == ["sale_id", "region_id", "revenue"]


def test_table_columns_reads_lowercase_keys_pg_shape():
    """PG 形状（小写 ``column_name``）同样通过。"""
    rows = [_row_with("column_name", "a"), _row_with("column_name", "b")]
    assert _table_columns(rows, "t", "postgresql") == ["a", "b"]


def test_table_columns_dedupes_across_schemas():
    """同名表存在于多个 schema → 按 ordinal_position 去重，保持首次出现顺序。"""
    rows = [_row_with("COLUMN_NAME", "sale_id"),
            _row_with("COLUMN_NAME", "region_id"),
            _row_with("COLUMN_NAME", "sale_id"),   # 另一 schema 的同名列
            _row_with("COLUMN_NAME", "revenue")]
    assert _table_columns(rows, "fact", "mysql") == ["sale_id", "region_id", "revenue"]


# --------------------------------------------------------------------------- #
# live：真 PG / 真 MySQL 端到端（不可达**或鉴权失败**一律 skip，绝不让 CI 因缺库而红）
# --------------------------------------------------------------------------- #
_TABLE = "profile_cross_dialect_t"
_COLS = ["sale_id", "sale_date", "region_id", "revenue", "note"]


def _pg_dsn() -> str:
    """env 优先，其次项目 .env 的 POSTGRES_DSN（本地真库常用后者）。"""
    return (os.getenv("POSTGRES_DSN") or get_settings().postgres_dsn
            or "postgresql+psycopg2://da:da@localhost:5432/da_agent")


def _mysql_dsn() -> str:
    """env 优先；CI 的 live job 用的就是 MYSQL_DSN（mysql:8.0 service container）。"""
    return os.getenv("MYSQL_DSN") or "mysql+pymysql://root:root@localhost:3306/da_agent"


def _host_port(dsn: str, default_port: int) -> tuple[str, int]:
    try:
        from sqlalchemy.engine.url import make_url

        u = make_url(dsn)
        return (u.host or "localhost", int(u.port or default_port))
    except Exception:
        return ("localhost", default_port)


def _connect_or_skip(dsn: str, label: str, default_port: int):
    """端口不通 / 凭据不对 / 驱动缺失 → skip（而不是 fail）。"""
    host, port = _host_port(dsn, default_port)
    try:
        s = socket.create_connection((host, port), timeout=1.5)
        s.close()
    except OSError:
        pytest.skip(f"{label} 端口 {host}:{port} 不可达（跳过 live）")

    from sqlalchemy import create_engine, text

    eng = create_engine(dsn, pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # 端口通但无凭据 → skip 而非失败
        eng.dispose()
        pytest.skip(f"{label} 无法连接/鉴权（跳过 live）: {str(exc)[:120]}")
    return eng


def _seed_and_profile(dsn: str, dialect: str, monkeypatch) -> dict:
    from sqlalchemy import text

    eng = _connect_or_skip(dsn, dialect, {"postgresql": 5432, "mysql": 3306}[dialect])
    ddl_cols = {
        "mysql": "sale_id INT PRIMARY KEY, sale_date VARCHAR(10), region_id INT, "
                 "revenue DOUBLE, note VARCHAR(16)",
        "postgresql": "sale_id INT PRIMARY KEY, sale_date VARCHAR(10), region_id INT, "
                      "revenue DOUBLE PRECISION, note VARCHAR(16)",
    }[dialect]
    try:
        with eng.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
            conn.execute(text(f"CREATE TABLE {_TABLE} ({ddl_cols})"))
            conn.execute(text(
                f"INSERT INTO {_TABLE} (sale_id, sale_date, region_id, revenue, note) "
                "VALUES (1,'2024-01-01',1,100.0,'a'),(2,'2024-01-02',1,200.0,'b'),"
                "(3,'2024-01-03',2,300.0,'c')"))
        monkeypatch.setenv("DATA_DB_URL", dsn)
        monkeypatch.setenv("DATA_DB_DIALECT", dialect)
        monkeypatch.setenv("MOCK_LLM", "true")
        get_settings.cache_clear()
        return profile_tool.run({"table": _TABLE, "key": "sale_id"})
    finally:
        try:
            with eng.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        finally:
            eng.dispose()
        get_settings.cache_clear()


def test_pg_dataset_profile_succeeds(monkeypatch):
    """修复前：`无法读取表结构: tuple indices ...`（坑 ①）。"""
    out = _seed_and_profile(_pg_dsn(), "postgresql", monkeypatch)
    assert out.get("ok"), out.get("error")
    assert list(out["columns"].keys()) == _COLS, list(out["columns"].keys())
    assert out["row_count"] == 3


def test_mysql_dataset_profile_succeeds(monkeypatch):
    """修复前：先 `Could not locate column ...`（坑 ②），再 `ERROR 1064`（坑 ③）。"""
    out = _seed_and_profile(_mysql_dsn(), "mysql", monkeypatch)
    assert out.get("ok"), out.get("error")
    assert list(out["columns"].keys()) == _COLS, list(out["columns"].keys())
    assert out["row_count"] == 3
