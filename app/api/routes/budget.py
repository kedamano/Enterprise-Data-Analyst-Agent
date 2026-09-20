"""Budget usage & config endpoints.

GET /api/v1/budget/usage  – current user's daily + monthly usage vs. limits.
GET /api/v1/budget/config – server-side budget limits (denominators for % bars).

Auth model mirrors analytics: when AUTH is off, the anonymous principal is used.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...config import get_settings
from ...core.security.auth import current_principal
from ...infrastructure.budget import BudgetManager, get_budget_manager

router = APIRouter(prefix="/budget", tags=["budget"])


def _manager() -> BudgetManager:
    return get_budget_manager()


@router.get("/usage")
def budget_usage(
    principal=Depends(current_principal),
    manager: BudgetManager = Depends(_manager),
):
    """Current user's daily + tenant's monthly usage summary."""
    user_id = getattr(principal, "user_id", "anonymous")
    tenant_id = getattr(principal, "tenant", "") or ""
    try:
        return manager.get_usage_summary(user_id, tenant_id)
    except Exception as exc:  # noqa: BLE001 — never let budget endpoint 500 the UI
        return {
            "session_ratio": 0.0,
            "user_daily_ratio": 0.0,
            "tenant_monthly_ratio": 0.0,
            "user_daily_used": 0,
            "user_daily_limit": 0,
            "tenant_monthly_used": 0,
            "tenant_monthly_limit": 0,
            "overrun_policy": "degrade",
            "error": str(exc)[:200],
        }


@router.get("/config")
def budget_config(
    principal=Depends(current_principal),
):
    """Expose budget limits so the front-end knows the denominator for % bars."""
    s = get_settings()
    return {
        "enabled": bool(s and s.budget_enabled),
        "per_session_tokens": int(s.budget_per_session_tokens) if s else 0,
        "per_user_daily_tokens": int(s.budget_per_user_daily_tokens) if s else 0,
        "per_tenant_monthly_tokens": int(s.budget_per_tenant_monthly_tokens) if s else 0,
        "overrun_policy": str(s.budget_overrun_policy) if s else "degrade",
    }
