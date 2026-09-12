"""短期记忆必须有 TTL —— 否则 Redis 里的会话键只增不减。

**背景（真跑才暴露）**：把 Redis 真起起来之后 `redis-cli HKEYS da:st:<sid>` 看到
`history/last_dataset/last_analysis/clarify_rounds/...` 一个不少，但 **`TTL` 是 -1**——
`short_term` 从头到尾**没有调用过 `EXPIRE`**，进程内 dict 也从不过期。
即：一次生产环境跑下来，每个会话都会在 Redis 里留一个永久 hash，只增不减。

参考项目 `references/ai-agent-interview-guide/.../project-java` 的短期记忆是**有** TTL 的
（`agent.memory.short-term.ttl-minutes`，默认 60 分钟）；本仓库缺这一层。

设计：**滑动过期**（写入即续期），默认 24h——比 Java 的 60 分钟长，因为本项目的会话
可能停在 `CLARIFY` 等用户回答，跨天回来还得能续跑；`short_term_ttl_s <= 0` 可显式关掉。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.memory import short_term


class _FakeRedis:
    """只记录调用，不真连——离线也能钉住"有没有设过期"。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: list[tuple[str, int]] = []

    def hset(self, name: str, key: str, value: str) -> None:
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name: str, key: str):
        return self.hashes.get(name, {}).get(key)

    def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    def expire(self, name: str, ttl: int) -> None:
        self.expires.append((name, ttl))


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(short_term, "_redis", lambda: fake)
    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()
    short_term._store.clear()


def test_put_sets_expiry(fake_redis):
    short_term.put("s1", "history", [{"role": "user", "content": "hi"}])
    assert fake_redis.expires, "put 之后必须设置过期，否则会话键永久驻留 Redis"
    key, ttl = fake_redis.expires[-1]
    assert key == "da:st:s1"
    assert ttl == int(get_settings().short_term_ttl_s)


def test_read_all_refreshes_expiry(fake_redis):
    """滑动过期：每轮开始都会 get_all，顺手续期，避免长会话中途被回收。"""
    short_term.put("s2", "objective", "分析营收")
    fake_redis.expires.clear()
    short_term.get_all("s2")
    assert [k for k, _ in fake_redis.expires] == ["da:st:s2"]


def test_zero_ttl_disables_expiry(monkeypatch, fake_redis):
    """显式配 0 表示"不过期"（本地开发/调试用），此时不该调用 expire。"""
    monkeypatch.setenv("SHORT_TERM_TTL_S", "0")
    get_settings.cache_clear()
    short_term.put("s3", "k", "v")
    assert not fake_redis.expires, "ttl<=0 时不应设置过期"


def test_fallback_to_memory_still_records_nothing(fake_redis, monkeypatch):
    """Redis 故障时降级内存——不应因为要设 TTL 而把异常抛出去。"""
    def boom(*_a, **_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(fake_redis, "hset", boom)
    short_term.put("s4", "k", "v")  # 不得抛
    assert short_term.get("s4", "k") == "v", "降级后仍要能从内存读回"


# --------------------------------------------------------------------------- #
# 真 Redis 上的断言（无 Redis 则跳过，绝不把"没跑"写成"通过"）
# --------------------------------------------------------------------------- #

_LIVE_URL = "redis://localhost:6379/15"  # db 15：测试专用，不碰业务库（同 test_redis_live）


def _redis_reachable() -> bool:
    import socket

    try:
        s = socket.create_connection(("localhost", 6379), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _redis_reachable(), reason="Redis 不可达（跳过 live 断言）")
def test_live_session_key_has_positive_ttl(monkeypatch):
    """真 Redis：写一个会话键，TTL 必须是正数（此前恒为 -1）。"""
    monkeypatch.setenv("REDIS_URL", _LIVE_URL)
    get_settings.cache_clear()
    import redis as redis_lib

    r = redis_lib.Redis.from_url(_LIVE_URL, decode_responses=True)
    sid = "ttl_live_probe"
    try:
        short_term.put(sid, "probe", {"ok": True})
        ttl = r.ttl(f"da:st:{sid}")
        assert ttl > 0, f"会话键必须带过期时间，实际 TTL={ttl}"
        assert ttl <= int(get_settings().short_term_ttl_s)
    finally:
        r.delete(f"da:st:{sid}")
        get_settings.cache_clear()
