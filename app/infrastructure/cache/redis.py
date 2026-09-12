"""Cache adapter – Redis (optional).

Returns a Redis client when ``redis_url`` is set, else ``None``. Callers (e.g.
short-term memory) fall back to an in-process store. Dependency is optional.

DEGRADE/02：客户端带**短连接/读写超时**——"配了但没起"的 Redis 必须立刻降级，
而不是让每次操作等满 OS 级 TCP 超时（本机 ~2s，摊到每个节点就是数秒）。
本模块是 Redis 客户端的**单一收口**，`core.memory.short_term` 委托到这里。
"""
from __future__ import annotations

from typing import Any

from ...config import get_settings


def build_client(url: str, connect_timeout_s: float, socket_timeout_s: float) -> Any | None:
    """按给定超时构造客户端；任何异常都返回 ``None``（调用方走内存兜底）。"""
    try:
        import redis  # lazy

        return redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=connect_timeout_s,
            socket_timeout=socket_timeout_s,
            # 降级要快：不在超时上再退避重试（重试是"想连上"的策略，此处要"快点放弃"）
            retry_on_timeout=False,
        )
    except Exception:
        return None


def get_client() -> Any | None:
    settings = get_settings()
    if not settings.redis_url:
        return None
    return build_client(settings.redis_url,
                        settings.redis_connect_timeout_s,
                        settings.redis_socket_timeout_s)
