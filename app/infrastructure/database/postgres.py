"""Relational adapter – PostgreSQL (optional, app metadata store).

The analytical data the agent queries is addressed via ``config.data_db_url``
(see ``sql_tool``). This adapter is for the *agent's own* metadata (run history,
feedback) and is only used when ``postgres_dsn`` is configured; otherwise the
service is fully functional without it.
"""
from __future__ import annotations

from typing import Any

from ...config import get_settings


def get_engine() -> Any | None:
    settings = get_settings()
    if not settings.postgres_dsn:
        return None
    try:
        from sqlalchemy import create_engine
        return create_engine(settings.postgres_dsn, pool_pre_ping=True)
    except Exception:
        return None
