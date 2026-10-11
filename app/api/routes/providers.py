"""LLM provider management API.

CRUD for model providers + connectivity testing + model listing + activation.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ...core.providers import store
from ...core.providers.probe import fetch_models, test_message
from ...infrastructure.llm import router as llm_router

logger = logging.getLogger("da.api.providers")

router = APIRouter(tags=["providers"])


# ── helpers ──────────────────────────────────────────────────────────────────
def _mask_key(key: str) -> str:
    """Mask API key for list responses — keep last 4 chars."""
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def _sanitize(row: dict[str, Any], *, with_key: bool = False) -> dict[str, Any]:
    d = dict(row)
    d["api_key"] = d["api_key"] if with_key else _mask_key(d["api_key"])
    return d


# ── CRUD ─────────────────────────────────────────────────────────────────────
@router.get("/providers/active")
def get_active_provider() -> dict[str, Any]:
    """Return the currently active provider + the live LLM router config."""
    active = store.get_active()
    live = llm_router.get_active_config()
    return {
        "provider": _sanitize(active) if active else None,
        "live": live,
    }


@router.get("/providers")
def list_providers() -> dict[str, Any]:
    rows = store.list_all()
    return {"providers": [_sanitize(r) for r in rows]}


@router.get("/providers/{provider_id}")
def get_provider(provider_id: int) -> dict[str, Any]:
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    return _sanitize(row)


@router.post("/providers")
def create_provider(body: dict[str, Any]) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    api_key = body.get("api_key", "")
    if not name:
        raise HTTPException(400, "name is required")
    if not base_url:
        raise HTTPException(400, "base_url is required")
    try:
        row = store.create(name, base_url, api_key)
    except Exception as exc:
        if "unique" in str(exc).lower():
            raise HTTPException(409, f"provider with name '{name}' already exists")
        raise HTTPException(500, str(exc))
    return _sanitize(row)


@router.put("/providers/{provider_id}")
def update_provider(provider_id: int, body: dict[str, Any]) -> dict[str, Any]:
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = body["name"]
    if "base_url" in body:
        fields["base_url"] = body["base_url"]
    if "api_key" in body:
        fields["api_key"] = body["api_key"]
    updated = store.update(provider_id, **fields)
    return _sanitize(updated) if updated else _sanitize(row)


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: int) -> dict[str, str]:
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    store.delete(provider_id)
    return {"status": "deleted", "id": str(provider_id)}


# ── models / test ────────────────────────────────────────────────────────────
@router.get("/providers/{provider_id}/models")
def get_models(provider_id: int) -> dict[str, Any]:
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    return {"id": provider_id, "models": row.get("available_models", [])}


@router.post("/providers/{provider_id}/probe")
def probe_provider(provider_id: int) -> dict[str, Any]:
    """Fetch available models; auto-persist on success."""
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    base_url, api_key = row["base_url"], row["api_key"]
    models = fetch_models(base_url, api_key)
    if models:
        store.set_models(provider_id, models)
    return {
        "id": provider_id,
        "ok": bool(models),
        "models": models,
        "count": len(models),
    }


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a real chat completion to verify connectivity + model availability."""
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    body = body or {}
    model = body.get("model", "")
    ok, detail = test_message(row["base_url"], row["api_key"], model)
    return {"id": provider_id, "ok": ok, "detail": detail, "model_tested": model or "auto"}


# ── activation ───────────────────────────────────────────────────────────────
@router.post("/providers/{provider_id}/activate")
def activate_provider(provider_id: int, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Set this provider as the active one + switch LLM router at runtime."""
    row = store.get(provider_id)
    if not row:
        raise HTTPException(404, "provider not found")
    model_override = (body or {}).get("model", "")

    if not store.set_active(provider_id):
        raise HTTPException(500, "activation failed")

    # Switch LLM router to the new provider
    applied_model = llm_router.apply_provider(
        base_url=row["base_url"],
        api_key=row["api_key"],
        model=model_override or None,
    )

    return {
        "status": "activated",
        "id": provider_id,
        "name": row["name"],
        "model": applied_model,
    }
