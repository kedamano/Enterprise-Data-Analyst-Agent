"""E2 free-form SQL: 自由 SQL 文本经守卫执行（突破模板、仍只读）。"""
from __future__ import annotations

import pytest

from app.core.tools.freeform import execute_free_sql


def test_free_join_window_sql_runs():
    """超出模板的自由 SQL（join + 窗口）应可只读执行并返回行。"""
    sql = (
        "SELECT r.region_name, f.channel_id, "
        "SUM(f.revenue) OVER (PARTITION BY r.region_name) AS region_total "
        "FROM fact_sales f JOIN dim_region r ON f.region_id = r.region_id "
        "LIMIT 5"
    )
    res = execute_free_sql(sql)
    assert res["ok"] is True, res
    assert res.get("rows"), "自由 SQL 应返回行"
    assert len(res["rows"]) <= 5


def test_free_sql_still_read_only():
    res = execute_free_sql("DELETE FROM fact_sales")
    assert res["ok"] is False
    assert "只读" in (res.get("error") or "")


def test_free_sql_rejects_multi_statement():
    res = execute_free_sql("SELECT 1; SELECT 2")
    assert res["ok"] is False
    assert res.get("error")


def test_free_sql_empty_fails():
    res = execute_free_sql("   ")
    assert res["ok"] is False
