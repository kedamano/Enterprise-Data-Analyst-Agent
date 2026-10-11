"""LLM provider store — SQLite-backed CRUD for model provider configurations.

Each provider entry holds name, base_url, api_key, and whether it's the active one.
API keys are stored in plaintext for simplicity (MVP). Future: encrypt with a KMS-derived key.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("da.providers")

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "db", "providers.db")
_LOCK = threading.Lock()

# ── schema ──────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    available_models TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_providers_active ON providers(is_active);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    c = sqlite3.connect(_DB_PATH, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


# ── helpers ──────────────────────────────────────────────────────────────────
def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "api_key": row["api_key"],
        "is_active": bool(row["is_active"]),
        "available_models": json.loads(row["available_models"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────
def list_all() -> list[dict[str, Any]]:
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM providers ORDER BY is_active DESC, name ASC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_active() -> Optional[dict[str, Any]]:
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM providers WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def get(provider_id: int) -> Optional[dict[str, Any]]:
    with _LOCK:
        row = _conn().execute(
            "SELECT * FROM providers WHERE id = ?", (provider_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create(name: str, base_url: str, api_key: str = "") -> dict[str, Any]:
    ts = time.time()
    with _LOCK:
        c = _conn()
        cur = c.execute(
            "INSERT INTO providers (name, base_url, api_key, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, base_url, api_key, 0, ts, ts),
        )
        c.commit()
        pid = cur.lastrowid
    return get(pid)  # type: ignore[return-value]


def update(provider_id: int, **kwargs: Any) -> Optional[dict[str, Any]]:
    allowed = {"name", "base_url", "api_key"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return get(provider_id)
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [provider_id]
    with _LOCK:
        c = _conn()
        c.execute(
            f"UPDATE providers SET {set_clause} WHERE id = ?", values
        )
        c.commit()
    return get(provider_id)


def delete(provider_id: int) -> bool:
    with _LOCK:
        c = _conn()
        cur = c.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
        c.commit()
        return cur.rowcount > 0


def set_active(provider_id: int) -> bool:
    with _LOCK:
        c = _conn()
        row = c.execute("SELECT id FROM providers WHERE id = ?", (provider_id,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE providers SET is_active = 0")
        c.execute(
            "UPDATE providers SET is_active = 1, updated_at = ? WHERE id = ?",
            (time.time(), provider_id),
        )
        c.commit()
    return True


def set_models(provider_id: int, models: list[str]) -> None:
    with _LOCK:
        c = _conn()
        c.execute(
            "UPDATE providers SET available_models = ?, updated_at = ? WHERE id = ?",
            (json.dumps(models), time.time(), provider_id),
        )
        c.commit()


def set_key(provider_id: int, api_key: str) -> Optional[dict[str, Any]]:
    return update(provider_id, api_key=api_key)
