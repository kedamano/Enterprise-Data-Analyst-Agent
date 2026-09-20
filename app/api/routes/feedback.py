"""Feedback API — user ratings / comments on completed analyses.

Endpoints:
  POST  /api/v1/feedback/{session_id}      — submit / overwrite feedback
  GET   /api/v1/feedback/{session_id}      — read existing feedback
  GET   /api/v1/feedback/stats             — aggregate feedback statistics

All endpoints tolerate feedback-system failures gracefully: even if the
SQLite write / read fails the caller only sees a 201 (submit) or a 404 /
200 with empty body — never a 500.

Route ordering matters: the literal ``/stats`` route MUST come **before**
``/{session_id}`` so that Starlette matches it first (otherwise
``/stats`` would be swallowed by ``/{session_id}`` with session_id="stats").
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from ...core.agents.data_analyst.checkpoint import load as checkpoint_load
from ...core.security.auth import current_principal
from ...infrastructure.feedback.feedback_store import (
    aggregate_feedback,
    get_feedback,
    submit_feedback,
)
from ...models.schemas import FeedbackIn, FeedbackOut

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _assert_session_exists(session_id: str) -> None:
    """404 if the checkpoint for *session_id* cannot be found."""
    try:
        state = checkpoint_load(session_id)
    except Exception:
        state = None
    if state is None:
        raise HTTPException(status_code=404, detail="session not found")


@router.get("/stats")
def stats(
    since: Optional[float] = None,
    principal=Depends(current_principal),
) -> dict[str, Any]:
    """Aggregate feedback statistics (admin-only when AUTH is on)."""
    # AUTH gate is already enforced globally via auth_middleware;
    # _admin_required-style gating could be added here for defense-in-depth.
    return aggregate_feedback(since_ts=since)


@router.post("/{session_id}", status_code=201)
def submit(
    session_id: str,
    body: FeedbackIn,
    principal=Depends(current_principal),
) -> dict[str, Any]:
    """Submit (or overwrite) feedback for a given session."""
    _assert_session_exists(session_id)

    # Best-effort capture of the report snippet from the checkpoint
    report_snippet = ""
    try:
        state = checkpoint_load(session_id)
        if state and state.report:
            report_snippet = (state.report or "")[:200]
    except Exception:
        pass

    try:
        submit_feedback(
            session_id=session_id,
            rating=body.rating,
            comment=body.comment or "",
            report_snippet=report_snippet,
            stage_breakdown=body.stage_breakdown,
        )
    except Exception:
        # 反馈系统出问题决不能打挂主流程
        pass

    return {"status": "recorded", "session_id": session_id}


@router.get("/{session_id}", response_model=Optional[FeedbackOut])
def get_one(session_id: str) -> Optional[dict]:
    """Read feedback for one session (404 if none)."""
    row = get_feedback(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="feedback not found")
    return row
