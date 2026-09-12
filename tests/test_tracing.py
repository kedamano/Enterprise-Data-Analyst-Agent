"""TDD suite for structured span tracing.

Contract:
* ``@trace`` emits a structured span (stage/status/duration_ms/error) for both
  success and failure, visible in ``recent_spans``.
* ``trace_run`` persists all in-block spans as JSONL under ``{trace_dir}/{run_id}.jsonl``
  with a trailing summary; ``load_run`` reads it back.
* The graph entry points (``run_analysis`` / ``stream_analysis``) wrap execution
  in a ``trace_run`` keyed by session id — each finished node leaves a span file.
"""
from __future__ import annotations

import json

import pytest

from app.infrastructure.observability.tracing import (
    load_run,
    recent_spans,
    trace,
    trace_run,
)


# --------------------------------------------------------------------------- #
# 1. @trace decorator
# --------------------------------------------------------------------------- #
@trace("alpha_stage")
def _alpha_ok():
    return "ok"


@trace("beta_stage")
def _beta_boom():
    raise ValueError("boom")


def _latest_span(stage: str) -> dict | None:
    return next((s for s in recent_spans(limit=1000) if s["stage"] == stage), None)


def test_decorator_records_success_span():
    assert _alpha_ok() == "ok"
    top = _latest_span("alpha_stage")
    assert top is not None, "应有 alpha_stage 的 span"
    assert top["status"] == "SUCCESS"
    assert top["duration_ms"] is not None and top["duration_ms"] >= 0
    assert top["span_id"] and top["trace_id"]


def test_decorator_records_error_span():
    with pytest.raises(ValueError):
        _beta_boom()
    beta = _latest_span("beta_stage")
    assert beta is not None, "应有 beta_stage 的 span"
    assert beta["status"] == "ERROR"
    assert beta["error"] and "boom" in beta["error"]


def test_decorator_standalone_does_not_persist(tmp_path):
    """节点级单独调用（如单测）不落盘——仅进内存缓冲。"""
    from app.infrastructure.observability import tracing as tr

    default_dir = tr._DEFAULT_DIR
    tr._DEFAULT_DIR = tmp_path
    try:
        _alpha_ok()
        assert list(tmp_path.iterdir()) == [], "standalone trace 不应写文件"
    finally:
        tr._DEFAULT_DIR = default_dir


# --------------------------------------------------------------------------- #
# 2. trace_run persistence
# --------------------------------------------------------------------------- #
@trace("inner_stage")
def _inner():
    return 1


def test_trace_run_persists_jsonl(tmp_path):
    run_id = "tracing_test_run"
    with trace_run(run_id=run_id, trace_dir=tmp_path):
        _inner()
        _inner()
    target = tmp_path / f"{run_id}.jsonl"
    assert target.exists(), "trace_run 退出时应落盘"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, "两个 span + 一个 summary"
    first = json.loads(lines[0])
    assert first["stage"] == "inner_stage" and first["status"] == "SUCCESS"
    summary = json.loads(lines[-1])
    assert summary["spans"] == 2
    assert summary["status"] == "OK"


def test_load_run_reads_back(tmp_path):
    run_id = "readback_run"
    with trace_run(run_id=run_id, trace_dir=tmp_path):
        _inner()
    spans = load_run(run_id, trace_dir=tmp_path)
    assert len(spans) == 2
    assert spans[-1]["spans"] == 1  # summary


@trace("usage_stage")
def _usage_fn():
    from app.infrastructure.observability.tracing import record_tokens
    record_tokens(prompt_tokens=120, completion_tokens=45)
    return 1


def test_llm_usage_recorded_into_active_span(tmp_path):
    """网关调用 record_tokens 后，usage 进入当前 span 与 summary。"""
    run_id = "usage_run"
    with trace_run(run_id=run_id, trace_dir=tmp_path):
        _usage_fn()
    spans = load_run(run_id, trace_dir=tmp_path)
    body = [s for s in spans[:-1]]
    assert body and body[0]["stage"] == "usage_stage"
    assert body[0]["prompt_tokens"] == 120
    assert body[0]["completion_tokens"] == 45
    assert spans[-1]["prompt_tokens"] == 120
    assert spans[-1]["completion_tokens"] == 45


# --------------------------------------------------------------------------- #
# 3. Graph entry points persist a trace keyed by session id
# --------------------------------------------------------------------------- #
def test_run_analysis_persists_trace(tmp_path, monkeypatch):
    """真实 graph 入口会按 session_id 落盘 spans（用 Mock 驱动全流水线）。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.infrastructure.observability import tracing as tr

    tr._DEFAULT_DIR = tmp_path
    from app.core.agents.data_analyst.graph import run_analysis

    state = run_analysis("trace_e2e_abc", "分析最近营收变化的原因，按地区维度下钻")
    assert state.status in ("FINISH", "ERROR", "FAILED")
    target = tmp_path / "trace_e2e_abc.jsonl"
    assert target.exists(), f"run_analysis 应落盘 trace，实际文件: {list(tmp_path.iterdir())}"
    spans = load_run("trace_e2e_abc", trace_dir=tmp_path)
    stages = [s.get("stage") for s in spans[:-1]]
    assert "context" in stages
    assert "planner" in stages
