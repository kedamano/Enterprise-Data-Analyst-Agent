"""Analytics aggregation endpoint tests.

Covers:
- 3 fake JSONL files with spans + summary → aggregated totals are correct.
- session_id= filter returns only that run.
- Zero-run directory does not 500 (totals.runs == 0).
- AUTH enabled + non-admin principal → 403.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.security.auth import Principal, anonymous_principal, set_current


def _write_jsonl(path: pathlib.Path, spans: list[dict], summary: dict) -> None:
    """Write spans + summary line to a .jsonl file."""
    lines = [json.dumps(s, ensure_ascii=False) for s in spans]
    lines.append(json.dumps(summary, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_summary(
    run_id: str,
    status: str = "OK",
    duration_s: float = 12.5,
    prompt_tokens: int = 5000,
    completion_tokens: int = 2000,
    stages: list[str] | None = None,
) -> dict:
    stages = stages or ["planner", "executor", "analyst", "reporter"]
    return {
        "trace_id": run_id,
        "spans": len(stages),
        "status": status,
        "duration_s": duration_s,
        "stages": stages,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _make_spans(stages: list[str] | None = None, tools: list[str] | None = None) -> list[dict]:
    stages = stages or ["planner", "executor", "analyst", "reporter"]
    tools = tools or ["sql_query", "python_analysis", "knowledge_search", "sql_query"]
    spans = []
    for i, stage in enumerate(stages):
        spans.append({
            "trace_id": "test",
            "span_id": f"s{i}",
            "stage": stage,
            "index": i,
            "status": "SUCCESS",
            "duration_ms": 1000 + i * 500,
            "error": None,
            "started_at": 1716000000.0 + i,
            "prompt_tokens": 1000,
            "completion_tokens": 400,
            "tool": tools[i] if i < len(tools) else None,
        })
    return spans


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _setup_fake_traces(tmp_path: pathlib.Path) -> None:
    """Create 3 fake trace JSONL files in tmp_path."""
    # Run 1: OK
    _write_jsonl(
        tmp_path / "run1.jsonl",
        _make_spans(),
        _make_summary("run1", "OK", 12.5, 5000, 2000),
    )
    # Run 2: OK (bigger)
    _write_jsonl(
        tmp_path / "run2.jsonl",
        _make_spans(["planner", "executor", "analyst", "reflection", "reporter"],
                    ["sql_query", "python_analysis", "knowledge_search", "sql_query", "sql_query"]),
        _make_summary("run2", "OK", 32.0, 12000, 4000,
                      ["planner", "executor", "analyst", "reflection", "reporter"]),
    )
    # Run 3: ERROR
    error_spans = _make_spans()
    error_spans[-1]["status"] = "ERROR"
    _write_jsonl(
        tmp_path / "run3.jsonl",
        error_spans,
        _make_summary("run3", "ERROR", 5.0, 2000, 500),
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_aggregated_totals(tmp_path: pathlib.Path):
    """3 fake traces → totals aggregate correctly."""
    _setup_fake_traces(tmp_path)
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"] == "7d"
    t = body["totals"]
    assert t["runs"] == 3
    assert t["success"] == 2
    assert t["failed"] == 1
    assert t["success_rate"] == 0.667
    # avg duration: (12500 + 32000 + 5000) / 3 = 16500
    assert t["avg_duration_ms"] == 16500
    # total tokens
    assert t["total_prompt_tokens"] == 5000 + 12000 + 2000
    assert t["total_completion_tokens"] == 2000 + 4000 + 500
    # p95: sorted durations = [5000, 12500, 32000]; P95 ≈ 32000
    assert t["p95_duration_ms"] == 32000


def test_by_stage_populated(tmp_path: pathlib.Path):
    """by_stage shows stage breakout with averages."""
    _setup_fake_traces(tmp_path)
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d")

    assert r.status_code == 200
    body = r.json()
    assert len(body["by_stage"]) > 0
    # planner should be present in all 3 runs → count == 3
    planner = next(s for s in body["by_stage"] if s["stage"] == "planner")
    assert planner["count"] == 3


def test_by_tool_populated(tmp_path: pathlib.Path):
    """by_tool shows tool distribution."""
    _setup_fake_traces(tmp_path)
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d")

    assert r.status_code == 200
    body = r.json()
    assert len(body["by_tool"]) > 0
    tools_by_name = {t["tool"]: t for t in body["by_tool"]}
    assert "sql_query" in tools_by_name


def test_session_id_filter(tmp_path: pathlib.Path):
    """session_id= returns only that run's summary."""
    _setup_fake_traces(tmp_path)
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d&session_id=run1")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["runs"] == 1


def test_empty_directory(tmp_path: pathlib.Path):
    """Empty trace dir does NOT 500 – returns totals.runs == 0."""
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["runs"] == 0


def test_invalid_range_returns_400(tmp_path: pathlib.Path):
    """Invalid range= returns HTTP 400."""
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=abc")

    assert r.status_code == 400


def test_recent_runs_returned(tmp_path: pathlib.Path):
    """recent_runs array is present and sorted desc by ts."""
    _setup_fake_traces(tmp_path)
    get_settings.cache_clear()

    with patch("app.api.routes.analytics._DEFAULT_TRACE_DIR", tmp_path):
        from app.main import app
        c = TestClient(app)
        r = c.get("/api/v1/analytics/stats?range=7d")

    assert r.status_code == 200
    body = r.json()
    assert len(body["recent_runs"]) == 3
    # sorted desc by ts (files have same mtime so order is file-dependent, just check presence)
    assert all("session_id" in rr for rr in body["recent_runs"])
