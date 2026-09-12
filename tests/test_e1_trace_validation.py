"""E1 溯源校验：数值型 evidence 必须能解析回真实成功 SQL step（红→绿）。"""
from __future__ import annotations

from app.core.agents.data_analyst.sources import unresolved_numeric_claims
from app.core.agents.data_analyst.state import Evidence, Finding, ToolResult


def _sql(step_id: str, status: str = "SUCCESS") -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status=status,
                      output={"rows": [{"v": 1}]})


def _schema(step_id: str) -> ToolResult:
    return ToolResult(step_id=step_id, tool="schema_search", status="SUCCESS",
                      output={"tables": []})


def _finding(text: str, evidence: list[Evidence]) -> Finding:
    return Finding(finding=text, evidence=evidence, confidence=0.8)


def test_numeric_claim_without_sql_id_is_flagged():
    f = _finding("Region A 贡献 61% 的下降",
                 [Evidence(source="sql", metric="contribution", value=0.61)])
    msgs = unresolved_numeric_claims([f], [_sql("s2")])
    assert msgs and "sql_id" in msgs[0]


def test_numeric_claim_with_valid_sql_id_passes():
    f = _finding("营收 120M", [Evidence(source="sql", value=120, sql_id="s2")])
    assert unresolved_numeric_claims([f], [_schema("s1"), _sql("s2")]) == []


def test_sql_id_to_failed_or_unknown_step_is_flagged():
    f_ok = _finding("a", [Evidence(value=1, sql_id="bad")])
    msgs = unresolved_numeric_claims([f_ok], [_sql("s2", status="FAILED")])
    assert msgs, "sql_id 指向失败步骤应判无有效溯源"


def test_non_numeric_interpretation_not_required_to_trace():
    f = _finding("渠道结构变化是主因", [Evidence(source="analyst", value="解读文字")])
    assert unresolved_numeric_claims([f], [_sql("s2")]) == []
