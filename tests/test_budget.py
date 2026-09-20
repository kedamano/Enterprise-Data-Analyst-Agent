"""Tests for the multi-level LLM usage budget system.

Covers: session limits, user daily accumulation, tenant monthly cross-user,
degrade suggestion, and dedup/quasi-simultaneous record merging.

Run: ``conda run -n base python -m pytest tests/test_budget.py -q``
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

import pytest

# Ensure the 'app' package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import Settings
from app.infrastructure.budget import (
    BudgetManager,
    BudgetDecision,
    reset_budget_manager,
)


def _settings(**overrides) -> Settings:
    """Build a Settings with budget overrides (isolated via monkeypatch)."""
    return Settings(
        **{
            "budget_enabled": True,
            "budget_per_session_tokens": 50_000,
            "budget_per_user_daily_tokens": 200_000,
            "budget_per_tenant_monthly_tokens": 10_000_000,
            "budget_overrun_policy": "degrade",
            "budget_degraded_model": "gpt-3.5-turbo",
            **overrides,
        }
    )


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "budget.db")


@pytest.fixture
def mgr(tmp_db):
    s = _settings()
    reset_budget_manager()
    m = BudgetManager(settings=s, db_path=tmp_db)
    return m


# 1. Empty DB → check_session returns OK with ratio=0
def test_session_under_limit(mgr):
    dec = mgr.check_session("sess-1")
    assert dec.ok is True
    assert dec.level == "OK"
    assert dec.ratio == 0.0
    assert dec.current == 0


# 2. Fill session to near-limit then check → EXCEEDED with ratio > 1.0
def test_session_exceed_limit(mgr, tmp_db):
    # record beyond the per-session token limit (50_000)
    mgr.record("sess-2", "u1", "t1",
               prompt_tokens=49_900, completion_tokens=0, cost_usd=0.0, stage="planner")
    mgr.record("sess-2", "u1", "t1",
               prompt_tokens=0, completion_tokens=500, cost_usd=0.0, stage="analyst")
    mgr.flush()
    # now check: total = 50_400 >= 50_000 → EXCEEDED
    dec = mgr.check_session("sess-2", estimated_prompt_tokens=1000)
    assert dec.level == "EXCEEDED"
    assert dec.ratio > 1.0
    assert dec.current == 50_400


# 3. Different users accumulate independently
def test_user_daily_accumulate(mgr):
    mgr.record("s1", "alice", "t1", 50_000, 10_000, 0.0, stage="planner")
    mgr.record("s2", "bob", "t1", 30_000, 5_000, 0.0, stage="analyst")
    mgr.flush()

    dec_alice = mgr.check_user_daily("alice")
    dec_bob = mgr.check_user_daily("bob")
    assert dec_alice.current == 60_000
    assert dec_bob.current == 35_000


# 4. Tenant monthly = sum across users
def test_tenant_monthly_cross_user(mgr):
    mgr.record("s1", "alice", "acme", 50_000, 10_000, 0.0, stage="planner")
    mgr.record("s2", "bob", "acme", 30_000, 5_000, 0.0, stage="analyst")
    mgr.record("s3", "carol", "other", 99_000, 1_000, 0.0, stage="reporter")
    mgr.flush()

    dec = mgr.check_tenant_monthly("acme")
    # acme = alice (60k) + bob (35k) = 95k
    assert dec.current == 95_000


# 5. overrun_policy=degrade → EXCEEDED suggests a model
def test_check_suggests_degrade_on_exceed(mgr):
    mgr.record("s1", "u9", "t9", 50_000, 0, 0.0, stage="planner")
    mgr.flush()
    dec = mgr.check_session("s1")
    assert dec.level == "EXCEEDED"
    assert dec.suggested_model == "gpt-3.5-turbo"


# 6. OK → suggested_model=None
def test_check_never_degrades_under_limit(mgr):
    mgr.record("s1", "u9", "t9", 10_000, 0, 0.0, stage="planner")
    mgr.flush()
    dec = mgr.check_session("s1")
    assert dec.level == "OK"
    assert dec.suggested_model is None


# 7. get_usage_summary returns fixed field names
def test_usage_summary_structure(mgr):
    summary = mgr.get_usage_summary("alice", "acme")
    assert "session_ratio" in summary
    assert "user_daily_ratio" in summary
    assert "tenant_monthly_ratio" in summary
    assert "user_daily_used" in summary
    assert "user_daily_limit" in summary
    assert "tenant_monthly_used" in summary
    assert "tenant_monthly_limit" in summary
    assert "overrun_policy" in summary


# 8. Simultaneous record → dedup (row count does NOT double)
def test_record_dedup_by_session(mgr, tmp_db):
    import sqlite3
    m = mgr
    # two back-to-back records on same session (<100ms apart) should merge into one row
    m.record("sess-dup", "u1", "t1", 100, 50, 0.01, stage="planner")
    m.record("sess-dup", "u1", "t1", 200, 100, 0.02, stage="analyst")
    m.flush()

    # Count rows in DB
    conn = sqlite3.connect(tmp_db)
    row_count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    conn.close()

    # Should be 1 row (merged), not 2
    assert row_count == 1, f"Expected 1 merged row, got {row_count}"
