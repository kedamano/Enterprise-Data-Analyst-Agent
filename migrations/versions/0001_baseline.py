"""Baseline schema — captures all current inline DDLs from code.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-11

Purpose: Establish a known schema baseline so future migrations can diff against it.
This migration is idempotent (all statements use IF NOT EXISTS).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply baseline schema — all DDLs are idempotent."""

    # ── audit_store: data/audit/audit.db ──────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            error_class TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # ── feedback_store: data/feedback/feedback.db ─────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
            comment TEXT,
            report_snippet TEXT,
            stage_breakdown TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(session_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_fb_session ON feedback(session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fb_rating ON feedback(rating)")

    # ── budget: data/budget/budget.db ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            stage TEXT,
            created_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_events(user_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage_events(tenant_id, created_at)")

    # ── filestore: data/files.db ──────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS fs_nodes (
            id TEXT PRIMARY KEY,
            parent_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            mime TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # ── knowledge_catalog: data/knowledge.db ───────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_bases (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_documents (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            name TEXT NOT NULL,
            source TEXT,
            status TEXT NOT NULL,
            embed_model_version TEXT,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # ── semantic_cache: data/cache/semantic_cache.db ──────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS semantic_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            query TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            query_hash TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_semantic_session ON semantic_cache(session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_semantic_hash ON semantic_cache(query_hash)")

    # ── attachments metadata (per-DB) ─────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS attachments_meta (
            table_name TEXT PRIMARY KEY,
            payload TEXT
        )
    """)


def downgrade() -> None:
    """Drop all baseline tables. Destructive — use only in dev / test."""
    tables = [
        "attachments_meta",
        "semantic_cache",
        "kb_documents",
        "knowledge_bases",
        "fs_nodes",
        "usage_events",
        "feedback",
        "tool_audit",
    ]
    for t in tables:
        op.execute(f"DROP TABLE IF EXISTS {t}")
