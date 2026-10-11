"""ARCH/08 降级事件无界增长修复

Spec: docs/specs/ARCH/08-fallback-events-bounded.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from app.infrastructure.llm.router import (
    record_fallback,
    fallback_events,
    reset_fallback_events,
    fallback_occurred,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_events():
    reset_fallback_events()
    yield
    reset_fallback_events()


# --------------------------------------------------------------------------- #
# 有界队列测试
# --------------------------------------------------------------------------- #
def test_record_fallback_event(clean_events):
    record_fallback("test_stage", "test error")
    events = fallback_events()
    assert len(events) == 1
    assert events[0]["stage"] == "test_stage"
    assert events[0]["error"] == "test error"


def test_events_bounded_at_100(clean_events):
    for i in range(150):
        record_fallback("test_stage", f"error {i}")
    events = fallback_events()
    assert len(events) == 100
    assert events[0]["error"] == "error 50"
    assert events[-1]["error"] == "error 149"


def test_fallback_occurred_true_after_record(clean_events):
    reset_fallback_events()
    assert fallback_occurred() is False
    record_fallback("test_stage", "test error")
    assert fallback_occurred() is True


def test_reset_clears_events(clean_events):
    record_fallback("test_stage", "test error")
    reset_fallback_events()
    assert fallback_occurred() is False
    assert len(fallback_events()) == 0


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_fallback_events_returns_list(clean_events):
    record_fallback("test_stage", "test error")
    events = fallback_events()
    assert isinstance(events, list)


def test_fallback_events_with_run_id(clean_events):
    record_fallback("test_stage", "test error")
    events = fallback_events(run_id="nonexistent")
    assert isinstance(events, list)
