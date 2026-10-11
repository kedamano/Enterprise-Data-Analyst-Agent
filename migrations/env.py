"""Alembic migration environment.

Reads DATABASE_URL from environment (falls back to local SQLite for dev).
Each migration is a versioned DDL diff — audit trail for schema evolution.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# Ensure project root is on sys.path so that app.* imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Alembic Config object
config = context.config

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# --------------------------------------------------------------------------- #
# Database URL resolution
# --------------------------------------------------------------------------- #
def _resolve_url() -> str:
    """Resolve DB URL from env → .env → default SQLite."""
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    # Fall back to local SQLite
    db_path = Path("data/db/metadata.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


# --------------------------------------------------------------------------- #
# Migration entry points
# --------------------------------------------------------------------------- #
def run_migrations_offline() -> None:
    """Generate SQL script (no DB connection required)."""
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=None,  # We use raw SQL in migrations (no ORM models)
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations via live DB connection."""
    url = _resolve_url()
    # Use NullPool for CLI migration tool (short-lived process)
    engine = create_engine(url, poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=None,
            include_schemas=True,
            compare_type=True,
            render_as_batch=True,  # SQLite ALTER TABLE workaround
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
