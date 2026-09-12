"""E2 free-form code execution — analyst-supplied SQL/Python under existing guards.

* ``execute_free_sql`` — arbitrary read-only SQL via ``sql_tool`` (single
  statement, no DML/DDL, row cap, per-statement timeout). Beyond the template
  executor, still locked down.
* ``execute_free_python`` — planned: analyst python text through AST guard +
  sandbox (wired in later E2 days).

Free-form output keeps the same ``{ok, rows/…, error}`` shape as the tools so it
flows through the existing ToolResult / audit / trace pipeline unchanged.
"""
from __future__ import annotations

from typing import Any

from . import sql_tool


def execute_free_sql(sql_text: str) -> dict[str, Any]:
    text = (sql_text or "").strip()
    if not text:
        return {"ok": False, "error": "空 SQL", "rows": []}
    # sql_tool 内置：只读正则 + 单语句 + 行数上限 + 语句级超时
    return sql_tool.run({"sql": text})


def run(params: dict) -> dict[str, Any]:
    """Registry-callable form：params={'sql': ...}。"""
    return execute_free_sql((params or {}).get("sql") or "")
