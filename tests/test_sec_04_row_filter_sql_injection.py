"""SEC/04 行级过滤 SQL 注入防护

Spec: docs/specs/SEC/04-row-filter-sql-injection.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from app.core.security.data_guard import apply_row_filters
from app.core.security.auth import Principal


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def row_filter_principal():
    return Principal(
        user_id="user1",
        roles=["analyst"],
        row_filters={"fact_sales": "region_id IN (1, 2)"},
    )


@pytest.fixture
def no_filter_principal():
    return Principal(user_id="user1", roles=["analyst"])


# --------------------------------------------------------------------------- #
# 简单查询测试
# --------------------------------------------------------------------------- #
def test_simple_query_injects_where(no_filter_principal, row_filter_principal):
    sql = "SELECT * FROM fact_sales"
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql
    assert "fact_sales" in applied


def test_simple_query_existing_where(no_filter_principal, row_filter_principal):
    sql = "SELECT * FROM fact_sales WHERE amount > 100"
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql
    assert "amount > 100" in new_sql
    assert new_sql.count("WHERE") == 1


def test_no_filter_returns_original(no_filter_principal):
    sql = "SELECT * FROM fact_sales"
    new_sql, applied = apply_row_filters(sql, no_filter_principal)
    assert new_sql == sql
    assert applied == []


# --------------------------------------------------------------------------- #
# 子查询测试
# --------------------------------------------------------------------------- #
def test_subquery_injects_in_outer_select(row_filter_principal):
    sql = "SELECT * FROM (SELECT * FROM fact_sales) AS sub"
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql


def test_cte_injects_correctly(row_filter_principal):
    sql = "WITH cte AS (SELECT * FROM fact_sales) SELECT * FROM cte"
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql


# --------------------------------------------------------------------------- #
# 复杂 JOIN 测试
# --------------------------------------------------------------------------- #
def test_join_query_injects_for_filtered_table(row_filter_principal):
    sql = """
        SELECT s.*, d.name
        FROM fact_sales s
        JOIN dim_region d ON s.region_id = d.id
    """
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql
    assert "fact_sales" in applied


def test_multiple_tables_only_filtered_gets_predicate(row_filter_principal):
    sql = """
        SELECT s.*, d.name, p.product_name
        FROM fact_sales s
        JOIN dim_region d ON s.region_id = d.id
        JOIN dim_product p ON s.product_id = p.id
    """
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "region_id IN (1, 2)" in new_sql
    assert "dim_region" not in applied
    assert "dim_product" not in applied


# --------------------------------------------------------------------------- #
# GROUP BY / ORDER BY / LIMIT 测试
# --------------------------------------------------------------------------- #
def test_group_by_order_by_limit_preserved(row_filter_principal):
    sql = """
        SELECT region_id, SUM(amount)
        FROM fact_sales
        GROUP BY region_id
        ORDER BY region_id
        LIMIT 10
    """
    new_sql, applied = apply_row_filters(sql, row_filter_principal)
    assert "GROUP BY" in new_sql
    assert "ORDER BY" in new_sql
    assert "LIMIT" in new_sql
    assert "region_id IN (1, 2)" in new_sql


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_none_principal_returns_original(no_filter_principal):
    sql = "SELECT * FROM fact_sales"
    new_sql, applied = apply_row_filters(sql, None)
    assert new_sql == sql
    assert applied == []


def test_empty_sql_returns_empty(no_filter_principal):
    new_sql, applied = apply_row_filters("", no_filter_principal)
    assert new_sql == ""
    assert applied == []
