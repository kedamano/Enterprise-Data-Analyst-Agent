"""DEGRADE/02 中间件快速失败：配了但没起的 Redis 必须立刻降级，而不是等满 TCP 超时。

Spec: docs/specs/DEGRADE/02-fast-fail-middleware.md
来源：D19 定位测试慢——本机未启动 Redis，每次操作等 OS 级超时 ~2s → 每节点 ~4s。
"""
from __future__ import annotations

import time

import pytest

from app.config import get_settings

# 指向一个必定连不上的本机端口（不产生真实外发流量）
_DEAD_REDIS = "redis://127.0.0.1:6399/0"


@pytest.fixture
def dead_redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", _DEAD_REDIS)
    monkeypatch.setenv("REDIS_CONNECT_TIMEOUT_S", "0.2")
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT_S", "0.5")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def no_redis_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_timeouts_are_passed_to_client(dead_redis_env):
    from app.infrastructure.cache.redis import get_client

    client = get_client()
    assert client is not None
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs.get("socket_connect_timeout") == 0.2, kwargs
    assert kwargs.get("socket_timeout") == 0.5, kwargs


def test_dead_redis_fails_fast(dead_redis_env):
    """核心断言：一次操作必须快速返回（修复前等满 ~2s 的 TCP 超时）。"""
    from app.core.memory import short_term

    started = time.time()
    short_term.get("ff_session", "k", default="fallback")
    elapsed = time.time() - started
    assert elapsed < 1.0, f"降级耗时 {elapsed:.2f}s，未快速失败"


def test_degradation_semantics_unchanged(dead_redis_env):
    """快失败不能改变降级语义：写进去的仍能读出来（内存兜底）。"""
    from app.core.memory import short_term

    short_term.put("ff_mem", "last_dataset", {"csv": "x.csv", "columns": ["a"]})
    got = short_term.get("ff_mem", "last_dataset")
    assert got == {"csv": "x.csv", "columns": ["a"]}
    assert short_term.get_all("ff_mem")["last_dataset"]["csv"] == "x.csv"


def test_no_url_returns_none(no_redis_env):
    from app.infrastructure.cache.redis import get_client

    assert get_client() is None


def test_short_term_uses_shared_factory(no_redis_env):
    """short_term 不再自己 from_url（单一收口），空配置下返回 None。"""
    from app.core.memory import short_term

    assert short_term._redis() is None
