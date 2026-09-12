"""LIVE integration tests against a real Redis (docker: agent-redis, localhost:6379).

Skipped automatically when Redis is unreachable, so the offline suite stays
green in environments without the middleware stack.
"""
from __future__ import annotations

import json
import socket

import pytest

from app.config import get_settings
from app.core.memory import short_term

_REDIS_URL = "redis://localhost:6379/15"  # db 15: 测试专用，不碰业务库


def _redis_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 6379), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _redis_reachable(), reason="Redis 不可达（跳过 live 测试）")


@pytest.fixture
def redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", _REDIS_URL)
    get_settings.cache_clear()
    yield
    # 清理测试 db
    import redis as redis_lib
    r = redis_lib.Redis.from_url(_REDIS_URL)
    keys = r.keys("da:st:redis_it_*")
    if keys:
        r.delete(*keys)
    get_settings.cache_clear()


def test_short_term_roundtrip_on_real_redis(redis_env):
    sid = "redis_it_1"
    short_term.put(sid, "last_analysis", {"objective": "营收分析", "status": "FINISH"})
    short_term.put(sid, "history", [{"query": "q1"}])

    got = short_term.get(sid, "last_analysis")
    assert got == {"objective": "营收分析", "status": "FINISH"}

    all_ = short_term.get_all(sid)
    assert all_["last_analysis"]["objective"] == "营收分析"
    assert all_["history"] == [{"query": "q1"}]

    # 确认真的是 Redis 而不是进程内 dict：直接用原生客户端对账
    import redis as redis_lib
    r = redis_lib.Redis.from_url(_REDIS_URL)
    raw = r.hget(f"da:st:{sid}", "last_analysis")
    assert raw is not None, "数据应真实写入 Redis"
    assert json.loads(raw)["objective"] == "营收分析"


def test_short_term_survives_fresh_store_state(redis_env):
    """Redis 后端的意义：进程内 _store 清空后数据仍在（跨 worker / 重启）。"""
    sid = "redis_it_2"
    short_term.put(sid, "k", {"v": 1})
    short_term._store.clear()  # 模拟新进程
    assert short_term.get(sid, "k") == {"v": 1}, "Redis 后端下数据不应随进程丢失"
