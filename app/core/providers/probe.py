"""Connectivity probe — test a provider and fetch its available models."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("da.providers.probe")


def fetch_models(base_url: str, api_key: str, timeout: float = 15.0) -> list[str]:
    """Fetch available model IDs from an OpenAI-compatible /models endpoint.

    Returns empty list on any failure (caller decides how to surface).
    """
    import httpx

    url = base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        r = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            logger.debug("GET /models -> %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        items = data.get("data", [])
        ids = [m["id"] for m in items if "id" in m]
        ids.sort()
        return ids
    except Exception as exc:
        logger.debug("fetch_models failed: %s", exc)
        return []


def test_message(base_url: str, api_key: str, model: str = "",
                 timeout: float = 30.0) -> tuple[bool, str]:
    """Send a trivial chat completion to verify the endpoint + key + model work.

    Returns (ok, detail) where detail is the response text on success or an
    error message on failure.
    """
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Pick first available model if not specified
    if not model:
        models = fetch_models(base_url, api_key)
        if not models:
            return False, "No models available and none specified"
        model = models[0]

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            try:
                reply = r.json()["choices"][0]["message"]["content"]
            except Exception:
                reply = "(non-JSON or empty choices)"
            return True, reply
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except httpx.TimeoutException:
        return False, f"Timeout after {timeout}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
