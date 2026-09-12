"""TDD suite for sql_query statement-level timeout (spec §22/§23 residue).

Contract under test:

* ``timeout_seconds`` (default 30) is enforced at the *statement* level:
  a runaway query is aborted and reported as a timeout error.
* The timeout error classifies as RETRYABLE (spec §23: SQL Timeout → retry,
  possibly with a reduced range).
"""
from __future__ import annotations

from app.core.tools import execute_tool
from app.core.tools.errors import ErrorClass, classify_error
from app.core.tools.sql_tool import run as sql_run

# 无限递归 CTE：不加超时会永远跑下去
_RUNAWAY_SQL = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt) "
    "SELECT COUNT(*) FROM cnt"
)


def test_timeout_seconds_param_is_enforced():
    res = sql_run({"sql": _RUNAWAY_SQL, "timeout_seconds": 1})
    assert res["ok"] is False, "失控查询必须被超时中止"
    assert "超时" in res["error"] or "timeout" in res["error"].lower(), res["error"]


def test_timeout_error_is_retryable():
    res = sql_run({"sql": _RUNAWAY_SQL, "timeout_seconds": 1})
    assert classify_error(res["error"]) is ErrorClass.RETRYABLE


def test_timeout_surface_through_execute_tool():
    # 端到端：execute_tool 层面拿到 FAILED + error_class=RETRYABLE
    # （execute_tool 会按协议重试 2 次，共 ~3 次 1s 超时）
    res = execute_tool("t1", "sql_query", {"sql": _RUNAWAY_SQL, "timeout_seconds": 1}, "timeout_test")
    assert res.status == "FAILED"
    assert res.error_class == "RETRYABLE"
    assert res.attempts >= 1


def test_normal_query_unaffected_by_timeout():
    res = sql_run({"sql": "SELECT COUNT(*) AS n FROM fact_sales", "timeout_seconds": 5})
    assert res["ok"] is True
    assert res["rows"][0]["n"] > 0
