"""E1 溯源对外呈现：报告 citations、/trace 端点、SSE trace_summary。"""
from __future__ import annotations

import json

from app.core.agents.data_analyst.sources import (
    append_citations,
    trace_manifest,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    Evidence,
    Finding,
    ToolResult,
)


def _sql(step_id: str = "s2") -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status="SUCCESS",
                      input={"sql": "SELECT region_id, SUM(rev) r FROM fact_sales GROUP BY 1"},
                      output={"rows": [{"region_id": 1, "r": 100}]})


def _state(finding: Finding | None = None) -> AgentState:
    s = AgentState(session_id="t", user_query="q")
    s.context = ContextModel(objective="obj")
    s.analysis = AnalysisResult(findings=[finding] if finding else [])
    s.tool_results = [_sql()]
    return s


def test_append_citations_adds_src_lines():
    rep = "营收下滑主要来自地区 A。"
    out = append_citations(rep, _state(Finding(
        finding="地区 A 下滑 25%",
        evidence=[Evidence(value=0.25, sql_id="s2")], confidence=0.8)).analysis,
        [_sql()])
    assert "[src: s2]" in out and "数字来源" in out


def test_append_citations_no_numeric_leaves_unchanged():
    rep = "渠道结构是主因"
    st = _state(Finding(finding="渠道结构是主因",
                        evidence=[Evidence(source="analyst", value="解读")]))
    assert append_citations(rep, st.analysis, st.tool_results) == rep


def test_trace_manifest_carries_claim_sql_and_sample():
    m = trace_manifest(_state(Finding(
        finding="营收 100", evidence=[Evidence(value=100, sql_id="s2")], confidence=0.9)))
    assert m["coverage"]["numeric_claims"] == 1
    assert m["coverage"]["traced_claims"] == 1
    claim = m["claims"][0]
    assert claim["evidence"][0]["sql"] and claim["evidence"][0]["rows_sample"]


def test_trace_endpoint_roundtrip(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm
    from app.main import app

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    try:
        state = run_analysis("trace_e2e", "分析最近营收变化的原因，按地区维度下钻")
        with TestClient(app) as c:
            r = c.get(f"/api/v1/chat/analyze/trace/{state.session_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "coverage" in body and "claims" in body
        with TestClient(app) as c:
            assert c.get("/api/v1/chat/analyze/trace/no_such_sid").status_code == 404
    finally:
        get_settings.cache_clear(); reset_llm()


def test_sse_finish_carries_trace_summary(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm
    from app.main import app

    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear(); reset_llm()
    try:
        with TestClient(app) as c:
            resp = c.post("/api/v1/chat/analyze/stream",
                          json={"query": "对比各区域营收表现", "session_id": "sse_trace"})
        last = None
        for ln in resp.text.splitlines():
            if ln.startswith("data: ") and ln[6:] != "[DONE]":
                last = json.loads(ln[6:])
        assert last and last.get("status") == "FINISH"
        assert last.get("trace_summary") is not None
    finally:
        get_settings.cache_clear(); reset_llm()
