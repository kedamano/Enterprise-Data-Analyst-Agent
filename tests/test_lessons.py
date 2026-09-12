"""Reflexion-style lesson persistence: failed/low-quality analyses write a
non-sensitive takeaway to long-term memory for future queries to avoid.
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.nodes import _persist_memory
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    ReflectionDimension,
    ReflectionResult,
)
from app.core.memory import long_term, short_term


@pytest.fixture
def tmp_long(tmp_path, monkeypatch):
    # 路径已可配（INTERVIEW/01 §1）
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "lt.jsonl"))
    get_settings.cache_clear()
    yield tmp_path / "lt.jsonl"


def _dim():
    return ReflectionDimension(score=0.4, issues=[])


def _refl(decision="PASS") -> ReflectionResult:
    return ReflectionResult(
        decision=decision, confidence=0.5, data_quality=_dim(),
        metric_quality=_dim(), evidence_coverage=_dim(), logical_validity=_dim(),
        completeness=_dim(), business_relevance=_dim(),
        missing_evidence=["缺少核心指标数据"], replan_objectives=["补指标数据"],
        summary="证据不足")


def _state(sid: str, *, error=None, decision="PASS") -> AgentState:
    s = AgentState(session_id=sid, user_query="q")
    s.context = ContextModel(objective="分析营收下滑")
    s.analysis = AnalysisResult(findings=[])
    if error or decision != "PASS":
        s.error = error
        s.reflection = _refl(decision)
    return s


def test_normal_pass_writes_no_lesson(tmp_long):
    _persist_memory(_state("less_ok"))
    lines = [json.loads(x) for x in tmp_long.read_text(encoding="utf-8").splitlines()] \
        if tmp_long.exists() else []
    assert not any(e.get("type") == "lesson" for e in lines)


def test_failed_run_writes_lesson(tmp_long):
    _persist_memory(_state("less_fail", error="无效列名 region_xx", decision="FAIL"))
    lines = [json.loads(x) for x in tmp_long.read_text(encoding="utf-8").splitlines()]
    lessons = [e for e in lines if e.get("type") == "lesson"]
    assert lessons, "失败应写入 lesson"
    assert "region_xx" in lessons[-1]["reason"]
    assert lessons[-1]["objective"] == "分析营收下滑"


def test_lessons_also_in_short_term_bounded(tmp_long):
    sid = "less_short"
    for i in range(8):
        s = _state(sid, error=f"错误 {i}", decision="FAIL")
        _persist_memory(s)
    lessons = short_term.get(sid, "lessons", [])
    assert lessons and len(lessons) <= 5, "短期 lessons 应有界"
    assert lessons[-1]["reason"].startswith("错误 7")
