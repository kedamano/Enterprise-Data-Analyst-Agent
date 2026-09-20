"""Feedback Loop: persistence + API + aggregation.

Covers:
- submit / upsert / invalid rating (store layer)
- aggregate stats + low_quality_sessions
- endpoint behaviour (404 on unknown session, 201 recorded)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# Isolate the SQLite feedback DB before anything imports the settings
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_feedback_db(tmp_path, monkeypatch):
    """Point feedback DB at a temp path to avoid touching real data/."""
    fb_dir = tmp_path / "feedback"
    fb_dir.mkdir()
    db_path = fb_dir / "feedback.db"

    def _mock_db_path(_self=None):
        return db_path

    from app.infrastructure.feedback import feedback_store
    monkeypatch.setattr(feedback_store, "_db_path", lambda: db_path)
    # prevent WAL leftover files confusing things
    yield db_path


# --------------------------------------------------------------------------- #
# Store layer
# --------------------------------------------------------------------------- #
def test_submit_feedback_valid():
    """submit +1 + comment → row id >= 1."""
    from app.infrastructure.feedback.feedback_store import submit_feedback

    rid = submit_feedback("sess_valid", 1, "很有帮助，分析全面")
    assert rid >= 1


def test_submit_feedback_upsert(tmp_path):
    """Same session_id submitted twice → only 1 row (overwrite, not duplicate)."""
    from app.infrastructure.feedback.feedback_store import submit_feedback, _conn

    submit_feedback("sess_dup", 1, "first")
    submit_feedback("sess_dup", -1, "actually it was wrong")

    conn = _conn()
    count = conn.execute("SELECT COUNT(*) FROM feedback WHERE session_id = ?",
                         ("sess_dup",)).fetchone()[0]
    conn.close()
    assert count == 1


def test_submit_feedback_invalid_rating():
    """rating ∉ {-1, 1} → ValueError."""
    from app.infrastructure.feedback.feedback_store import submit_feedback

    with pytest.raises(ValueError):
        submit_feedback("sess_bad", 2)


def test_submit_feedback_long_comment_truncated():
    """Comment > 500 chars is auto-truncated (no error)."""
    from app.infrastructure.feedback.feedback_store import submit_feedback, _conn

    long = "啊" * 600
    submit_feedback("sess_long", 1, long)
    conn = _conn()
    row = conn.execute("SELECT comment FROM feedback WHERE session_id = ?",
                       ("sess_long",)).fetchone()
    conn.close()
    assert row is not None
    assert len(row[0]) == 500


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def test_aggregate_feedback():
    """3 feedbacks (2 up, 1 down) → satisfaction_rate ≈ 0.667."""
    from app.infrastructure.feedback.feedback_store import (
        aggregate_feedback,
        low_quality_sessions,
        submit_feedback,
    )

    submit_feedback("agg_a", 1)
    submit_feedback("agg_b", 1)
    submit_feedback("agg_c", -1)

    agg = aggregate_feedback()
    assert agg["thumbs_up"] == 2
    assert agg["thumbs_down"] == 1
    assert agg["total"] == 3
    assert abs(agg["satisfaction_rate"] - 0.667) < 0.001

    bad = low_quality_sessions()
    assert len(bad) == 1
    assert "agg_c" in bad


def test_aggregate_feedback_empty():
    """Empty DB → zeros and empty lists."""
    from app.infrastructure.feedback.feedback_store import aggregate_feedback

    agg = aggregate_feedback()
    assert agg["total"] == 0
    assert agg["satisfaction_rate"] == 0.0
    assert agg["recent_10"] == []
    assert agg["comments_with_text"] == []


def test_aggregate_feedback_since_ts():
    """since_ts filters correctly (0 feedbacks before epoch)."""
    from app.infrastructure.feedback.feedback_store import (
        aggregate_feedback,
        submit_feedback,
    )
    import time

    submit_fb("since_a", 1)
    agg = aggregate_feedback(since_ts=time.time() + 1000)
    assert agg["total"] == 0  # no feedback in the future


def test_low_quality_sessions():
    """3 feedbacks (2 up, 1 down) → 1 bad session_id string."""
    from app.infrastructure.feedback.feedback_store import (
        low_quality_sessions,
        submit_feedback,
    )

    submit_fb("lq_x", 1)
    submit_fb("lq_y", 1)
    submit_fb("lz_z", -1)

    bad = low_quality_sessions()
    assert isinstance(bad, list)
    assert len(bad) == 1
    assert bad[0] == "lz_z"


# --------------------------------------------------------------------------- #
# API endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    """Test client (AUTH disabled for simplicity)."""
    import os
    os.environ["AUTH_ENABLED"] = "false"
    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import app
    with TestClient(app) as c:
        yield c


def test_submit_endpoint_404_on_unknown_session(client):
    """POST /feedback/{id} where no checkpoint exists → 404."""
    resp = client.post("/api/v1/feedback/unknown-session-xyz",
                       json={"rating": 1, "comment": "test"})
    assert resp.status_code == 404


def test_submit_endpoint_recorded(client, tmp_path, monkeypatch):
    """Mock checkpoint_load OK + mock submit_feedback → HTTP 201 + body."""
    with patch("app.api.routes.feedback.checkpoint_load") as mock_cp, \
         patch("app.api.routes.feedback.submit_feedback") as mock_submit:
        mock_cp.return_value = object()  # any non-None
        mock_submit.return_value = 42

        resp = client.post("/api/v1/feedback/sess_ok",
                           json={"rating": 1, "comment": "good"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "recorded"
        assert resp.json()["session_id"] == "sess_ok"


def test_get_feedback_404(client):
    """GET /feedback/{id} when no feedback row → 404."""
    resp = client.get("/api/v1/feedback/no-feedback-session")
    assert resp.status_code == 404


def test_get_feedback_recorded_and_readable(client, tmp_path, monkeypatch):
    """Submit (real store) → GET returns the record."""
    with patch("app.api.routes.feedback.checkpoint_load") as mock_cp:
        mock_cp.return_value = object()

        resp = client.post("/api/v1/feedback/sess_round_trip",
                           json={"rating": 1, "comment": "nice"})
        assert resp.status_code == 201

    from app.infrastructure.feedback.feedback_store import get_feedback
    rec = get_feedback("sess_round_trip")
    assert rec is not None
    assert rec["rating"] == 1
    assert rec["comment"] == "nice"


def test_stats_endpoint(client):
    """GET /feedback/stats returns aggregate payload."""
    resp = client.get("/api/v1/feedback/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert "total" in body
    assert "thumbs_up" in body
    assert "satisfaction_rate" in body


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def submit_fb(sid: str, rating: int, comment: str = "") -> None:
    """Shorthand for store-layer submit (used inside parametrized-style tests)."""
    from app.infrastructure.feedback.feedback_store import submit_feedback
    submit_feedback(sid, rating, comment)
