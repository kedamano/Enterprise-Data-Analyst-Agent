"""E1 溯源解析器：evidence 的 sql_id 必须能解析回一条真实 sql_query step。

契约（docs/specs/E1/01）：
* resolve_sql_source(sql_id, results) → 命中且为 SUCCESS 的 sql_query ToolResult；
* 找不到 / 非 sql / 失败 步骤 → None（宁可无溯源也不链到假源）。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst.sources import resolve_sql_source
from app.core.agents.data_analyst.state import ToolResult


def _sql(step_id: str, status: str = "SUCCESS") -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status=status,
                      output={"rows": [{"region_id": 1}], "csv_path": f"data/{step_id}.csv"})


def _schema(step_id: str) -> ToolResult:
    return ToolResult(step_id=step_id, tool="schema_search", status="SUCCESS",
                      output={"tables": []})


def test_resolves_success_sql_step():
    results = [_schema("s1"), _sql("s2")]
    hit = resolve_sql_source("s2", results)
    assert hit is not None and hit.step_id == "s2" and hit.tool == "sql_query"
    assert hit.status == "SUCCESS"


def test_unknown_or_non_sql_returns_none():
    results = [_schema("s1"), _sql("s2")]
    assert resolve_sql_source("s1", results) is None   # schema 不是数值源
    assert resolve_sql_source("nope", results) is None  # 不存在


def test_failed_sql_is_not_a_valid_source():
    results = [_schema("s1"), _sql("s2", status="FAILED")]
    assert resolve_sql_source("s2", results) is None, "失败的查询不能作为数值溯源"


def test_empty_results_returns_none():
    assert resolve_sql_source("s1", []) is None
