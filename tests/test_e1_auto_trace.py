"""E1 auto-trace：analyst 后处理给数值 evidence 自动补 sql_id。"""
from __future__ import annotations

from app.core.agents.data_analyst.sources import (
    auto_trace,
    last_successful_sql_id,
    resolve_sql_source,
)
from app.core.agents.data_analyst.state import Evidence, Finding, ToolResult


def _sql(step_id: str, status: str = "SUCCESS") -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status=status, output={"rows": [{"v": 1}]})


def _finding(evidence: list[Evidence]) -> Finding:
    return Finding(finding="f", evidence=evidence, confidence=0.8)


def test_last_successful_sql_id_is_latest_success():
    results = [_sql("s1", "FAILED"), _sql("s2"), _sql("s3")]
    assert last_successful_sql_id(results) == "s3"


def test_auto_trace_fills_missing_numeric_sql_id():
    f = _finding([Evidence(value=61.0)])
    auto_trace([f], [_sql("s2")])
    assert f.evidence[0].sql_id == "s2"
    assert resolve_sql_source(f.evidence[0].sql_id, [_sql("s2")]) is not None


def test_auto_trace_keeps_existing_and_non_numeric():
    f = _finding([Evidence(value=5, sql_id="s1"),
                  Evidence(source="analyst", value="解读")])
    auto_trace([f], [_sql("s1"), _sql("s2")])
    evs = f.evidence
    assert evs[0].sql_id == "s1"          # 已有不动
    assert evs[1].sql_id is None           # 非数值不强求


def test_auto_trace_no_sql_available_leaves_none():
    f = _finding([Evidence(value=1)])
    auto_trace([f], [])
    assert f.evidence[0].sql_id is None


def test_full_pipeline_auto_traces_numeric_evidence(monkeypatch, tmp_path):
    """mock 全流水线结束后，数值型 evidence 应自动带可解析 sql_id。"""
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm
    from app.core.agents.data_analyst.graph import run_analysis
    from app.core.agents.data_analyst.sources import resolve_sql_source

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    try:
        state = run_analysis("e1_auto", "分析最近营收变化的原因，按地区维度下钻")
        assert state.status == "FINISH", state.error
        numeric_evs = [ev for f in state.analysis.findings
                       for ev in f.evidence
                       if isinstance(getattr(ev, "value", None), (int, float, str))]
        traced = [ev for ev in numeric_evs if ev.sql_id and
                  resolve_sql_source(ev.sql_id, state.tool_results)]
        assert traced, "mock 流水线后应有数值 evidence 带可解析 sql_id"
    finally:
        get_settings.cache_clear(); reset_llm()
