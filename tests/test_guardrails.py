"""Offline guardrail test suite (negative paths) — no API key required.

Covers the safety / control-flow contracts that ``test_agent_real.py`` leaves
untested:

* sql_query   — read-only enforcement, single-statement rule, data integrity
* python_analysis — AST sandbox: blocked imports, dynamic-exec escapes, timeout
* reflection  — REPLAN trigger, max_replans truncation, FAIL routing
* LLM gateway — fallback spy records silent degradation; LLM_NO_FALLBACK raises
* HTTP API    — SSE stream shape, /documents/ingest

These run against the bundled SQLite sample with MockLLM, so they are safe for
CI. The real-LLM behavioural contract lives in ``test_agent_real.py``.
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings, get_settings
from app.core.agents.data_analyst.graph import run_analysis
from app.core.agents.data_analyst.nodes import run_reflection
from app.core.agents.data_analyst.state import AgentState, AnalysisResult, Finding
from app.core.tools import execute_tool
from app.infrastructure.llm.router import (
    MockLLM,
    OpenAILLM,
    fallback_events,
    fallback_occurred,
    get_llm,
    reset_llm,
)


# --------------------------------------------------------------------------- #
# Fixtures — force the offline MockLLM path (conftest forces real LLM on;
# our tests deliberately run offline and restore the env afterwards).
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


@pytest.fixture
def fast_python_timeout(monkeypatch):
    monkeypatch.setenv("PYTHON_MAX_EXEC_S", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 1. sql_query — read-only guardrails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM fact_sales",
        "DROP TABLE fact_sales",
        "UPDATE fact_sales SET revenue = 0",
        "INSERT INTO fact_sales VALUES (1, 2, 3)",
        "TRUNCATE TABLE fact_sales",
        "ALTER TABLE fact_sales ADD COLUMN x INT",
        "CREATE TABLE evil (id INT)",
    ],
)
def test_sql_write_operations_are_rejected(sql):
    res = execute_tool("neg", "sql_query", {"sql": sql}, "guard_sql")
    assert res.status == "FAILED", f"写操作未被拦截: {sql}"
    assert res.error and "只读" in res.error, res.error


def test_sql_multi_statement_is_rejected():
    res = execute_tool("neg", "sql_query", {"sql": "SELECT 1; SELECT 2"}, "guard_sql")
    assert res.status == "FAILED"
    assert res.error and "单条语句" in res.error


def test_sql_reject_leaves_data_intact():
    def _count() -> int:
        res = execute_tool("cnt", "sql_query",
                           {"sql": "SELECT COUNT(*) AS n FROM fact_sales"}, "guard_sql")
        assert res.status == "SUCCESS", res.error
        return res.output["rows"][0]["n"]

    before = _count()
    for sql in ("DELETE FROM fact_sales", "DROP TABLE fact_sales"):
        execute_tool("neg", "sql_query", {"sql": sql}, "guard_sql")
    after = _count()
    assert before == after and before > 0, "数据在写操作尝试后发生变化"


def test_unknown_tool_fails_cleanly():
    res = execute_tool("x", "not_a_tool", {}, "guard_sql")
    assert res.status == "FAILED" and "未知工具" in (res.error or "")


# --------------------------------------------------------------------------- #
# 2. python_analysis — sandbox guardrails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", [
    "import os\nos.getcwd()",
    "import socket\nsocket.socket()",
    "import subprocess\nsubprocess.run(['dir'])",
    "import requests\nrequests.get('http://evil.example')",
    "from pathlib import Path\nPath('.')",
])
def test_sandbox_blocks_forbidden_imports(code):
    res = execute_tool("neg", "python_analysis", {"code": code}, "guard_py")
    assert res.status == "FAILED", f"危险导入未被拦截: {code[:30]}"
    assert res.error and "禁止导入模块" in res.error, res.error


@pytest.mark.parametrize("code", [
    "__import__('os').getcwd()",
    "eval('__import__(\"os\").getcwd()')",
    "exec('import os')",
    "compile('import os', '<s>', 'exec')",
])
def test_sandbox_blocks_dynamic_exec_escapes(code):
    """AST import 扫描的直接绕道（__import__/eval/exec/compile）必须被拒。"""
    res = execute_tool("neg", "python_analysis", {"code": code}, "guard_py")
    assert res.status == "FAILED", f"动态执行逃逸未被拦截: {code[:40]}"
    assert res.error and "动态执行" in res.error, res.error


def test_sandbox_enforces_timeout(fast_python_timeout):
    res = execute_tool(
        "neg", "python_analysis",
        {"code": "import time\ntime.sleep(30)\nprint('should not reach')"},
        "guard_py",
    )
    assert res.status == "FAILED"
    assert res.error and "超时" in res.error


def test_sandbox_allows_benign_analysis():
    code = "print(_json.dumps({'rows': 0 if df is None else int(df.shape[0])}))"
    res = execute_tool("ok", "python_analysis", {"code": code}, "guard_py")
    assert res.status == "SUCCESS", res.error


# --------------------------------------------------------------------------- #
# 3. Reflection — REPLAN / max_replans / FAIL routing
# --------------------------------------------------------------------------- #
def _state_without_evidence() -> AgentState:
    state = AgentState(session_id="guard_refl", user_query="分析营收")
    state.analysis = AnalysisResult(
        findings=[Finding(finding="缺乏证据的结论", evidence=[], interpretation="", confidence=0.3)]
    )
    state.tool_results = []
    return state


def test_reflection_replan_triggers_when_evidence_missing(mock_llm_env):
    assert isinstance(get_llm(), MockLLM)
    state = run_reflection(_state_without_evidence())
    assert state.reflection is not None
    assert state.reflection.decision == "REPLAN"
    assert state.status == "REPLAN"
    assert state.replan_count == 1, "REPLAN 应回调 replan_count"


def test_reflection_replan_truncated_by_max_replans(mock_llm_env):
    state = _state_without_evidence()
    state.max_replans = 2
    state.replan_count = 2  # 已达上限
    state = run_reflection(state)
    assert state.reflection.decision == "REPLAN"  # 质检结论仍是 REPLAN
    assert state.status == "REPORT", "达上限后应转 REPORT 兜底而非继续循环"


def test_reflection_fail_routes_to_failed(monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes

    fail_json = json.dumps({
        "decision": "FAIL", "confidence": 0.2, "summary": "数据不足以完成分析",
        "data_quality": {"score": 0.2, "issues": ["数据缺失"]},
        "metric_quality": {"score": 0.3, "issues": []},
        "evidence_coverage": {"score": 0.1, "issues": ["无证据"]},
        "logical_validity": {"score": 0.5, "issues": []},
        "completeness": {"score": 0.2, "issues": []},
        "business_relevance": {"score": 0.4, "issues": []},
        "missing_evidence": ["核心指标数据"], "replan_objectives": [],
    })
    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: fail_json)
    state = AgentState(session_id="guard_fail", user_query="分析营收")
    state.analysis = AnalysisResult()
    state = run_reflection(state)
    assert state.status == "FAILED"
    assert state.error and "数据不足" in state.error


def test_full_pipeline_offline_with_mock(mock_llm_env):
    """MockLLM 全流水线仍端到端可用（回归保护：强类型化后不得破坏编排）。"""
    state = run_analysis("guard_e2e", "分析最近半年华北地区营收下滑的原因，按产品维度下钻")
    assert state.status == "FINISH", f"status={state.status}, error={state.error}"
    assert any(r.status == "SUCCESS" for r in state.tool_results)
    assert state.report.strip()


# --------------------------------------------------------------------------- #
# 4. LLM gateway — fallback spy
# --------------------------------------------------------------------------- #
def _unreachable_settings(**kw) -> Settings:
    base = dict(
        llm_api_key="bad-key",
        llm_base_url="http://127.0.0.1:9/v1",  # port 9 (discard) — refuses fast
        llm_max_retries=1,
        llm_timeout_s=2,
    )
    base.update(kw)
    return Settings(**base)


def test_fallback_spy_records_silent_degradation():
    llm = OpenAILLM(_unreachable_settings(llm_no_fallback=False))
    out = llm.complete("system", "user", stage="context", json_mode=True)
    assert isinstance(out, str) and out, "降级后应返回 Mock 响应"
    assert fallback_occurred() is True, "静默降级必须被 spy 记录"
    events = fallback_events()
    assert events and events[0]["stage"] == "context"


def test_no_fallback_mode_raises_and_spy_stays_clean():
    llm = OpenAILLM(_unreachable_settings(llm_no_fallback=True))
    with pytest.raises(Exception):
        llm.complete("system", "user", stage="planner", json_mode=True)
    assert fallback_occurred() is False, "LLM_NO_FALLBACK 下不应发生降级"


# --------------------------------------------------------------------------- #
# 5. HTTP API — SSE stream + document ingest
# --------------------------------------------------------------------------- #
def test_analyze_stream_emits_sse(mock_llm_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/chat/analyze/stream",
            json={"query": "分析各区域营收表现", "session_id": "guard_sse"},
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "data: " in body, "应为 SSE 事件流"
    assert body.rstrip().endswith("data: [DONE]"), "应以 [DONE] 结束"
    events = [ln[len("data: "):] for ln in body.splitlines() if ln.startswith("data: ")]
    statuses = [json.loads(e)["status"] for e in events[:-1]]  # 最后一个是 [DONE]
    assert "FINISH" in statuses, f"流应到达 FINISH，实际: {statuses}"


def test_document_ingest(mock_llm_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/documents/ingest",
            json={"text": "营收（revenue）定义为订单金额总和，扣除退款后的净额。"
                          "华北地区涵盖北京、天津、河北、山西、内蒙古。",
                  "source": "guard_test"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chunks"] >= 1, "文本应被分块入库"
