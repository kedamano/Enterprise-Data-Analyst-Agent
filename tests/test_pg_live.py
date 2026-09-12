"""LIVE integration tests for the PostgreSQL-backed long-term memory (spec §20).

Runs against the docker compose ``postgres`` service (localhost:5432). Skipped
when PG is unreachable. Verifies that configuring ``POSTGRES_DSN`` moves the
long-term store from JSONL to PG, with JSONL kept as the offline fallback.
"""
from __future__ import annotations

import socket

import pytest

from app.config import get_settings
from app.core.memory import long_term

_DSN = "postgresql+psycopg2://da:da@localhost:5432/da_agent"


def _pg_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 5432), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="PostgreSQL 不可达（跳过 live 测试）")


@pytest.fixture
def pg_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTGRES_DSN", _DSN)
    get_settings.cache_clear()
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "long_term.jsonl"))
    get_settings.cache_clear()
    long_term._pg_engine.cache_clear()
    yield
    # 清理测试数据
    try:
        eng = long_term._pg_engine()
        if eng is not None:
            from sqlalchemy import text
            with eng.begin() as conn:
                conn.execute(text("DELETE FROM da_long_term WHERE data->>'session_id' LIKE 'pg_it_%'"))
    except Exception:
        pass
    long_term._pg_engine.cache_clear()
    get_settings.cache_clear()


def test_append_writes_to_pg_not_jsonl(pg_env):
    long_term.append({"type": "analysis_summary", "session_id": "pg_it_1",
                      "objective": "华北营收分析", "findings": ["华北营收下滑 25%"]})
    # JSONL 不应有该条（PG 后端接管）
    assert not long_term._path().exists() or "pg_it_1" not in long_term._path().read_text(encoding="utf-8")
    # PG 里应有
    from sqlalchemy import text
    eng = long_term._pg_engine()
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT data FROM da_long_term WHERE data->>'session_id' = 'pg_it_1'")).fetchall()
    assert len(rows) == 1
    assert rows[0][0]["objective"] == "华北营收分析"


def test_search_hits_pg_entries(pg_env):
    long_term.append({"type": "analysis_summary", "session_id": "pg_it_2",
                      "objective": "复购率分析", "findings": ["老客复购率下降 22%"]})
    long_term.append({"type": "analysis_summary", "session_id": "pg_it_3",
                      "objective": "渠道分析", "findings": ["线上渠道增长"]})
    hits = long_term.search("复购率")
    assert any("复购率" in h.get("objective", "") or any("复购率" in f for f in h.get("findings", []))
               for h in hits), f"应检索到复购率条目，实际: {hits}"


def test_sensitive_keys_never_persisted(pg_env):
    long_term.append({"session_id": "pg_it_4", "objective": "x",
                      "api_key": "sk-secret", "password": "hunter2"})
    from sqlalchemy import text
    eng = long_term._pg_engine()
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT data FROM da_long_term WHERE data->>'session_id' = 'pg_it_4'")).fetchall()
    assert rows, "条目应写入"
    assert "api_key" not in rows[0][0] and "password" not in rows[0][0]


def test_jsonl_fallback_without_dsn(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTGRES_DSN", "")
    get_settings.cache_clear()
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "lt.jsonl"))
    get_settings.cache_clear()
    long_term._pg_engine.cache_clear()
    try:
        long_term.append({"session_id": "pg_it_5", "objective": "离线模式"})
        assert "pg_it_5" in long_term._path().read_text(encoding="utf-8"), \
            "无 DSN 时应回退 JSONL"
        assert long_term.search("离线模式"), "JSONL 回退检索可用"
    finally:
        long_term._pg_engine.cache_clear()
        get_settings.cache_clear()
