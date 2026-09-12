"""Short-term (per-session) memory.

Holds the working context of an in-flight analysis: the resolved context,
current plan, latest tool results, etc. Backed by Redis when ``redis_url`` is
set, otherwise an in-process dict (fine for a single worker / development).
"""
from __future__ import annotations

import json
from typing import Any

_store: dict[str, dict[str, Any]] = {}


def _redis():
    """Redis 客户端（**委托** cache 层，单一收口：短超时、快速失败，见 DEGRADE/02）。"""
    try:
        from ...infrastructure.cache.redis import get_client

        return get_client()
    except Exception:
        return None


def _dict(session_id: str) -> dict[str, Any]:
    return _store.setdefault(session_id, {})


def _expire(r, session_id: str) -> None:
    """滑动过期：写入/读取即续期。

    不设过期的后果是实打实的：Redis 里 `da:st:<sid>` 会永久驻留（实测 TTL=-1），
    每个会话一条，只增不减。参考实现的 Java 版本有 TTL，本层此前漏了。
    """
    try:
        from ...config import get_settings

        ttl = int(get_settings().short_term_ttl_s)
    except Exception:
        return
    if ttl <= 0:
        return  # 显式关闭过期（仅调试用）
    try:
        r.expire(f"da:st:{session_id}", ttl)
    except Exception:
        pass  # 续期失败不打断主流程（与记忆降级同一原则）


def put(session_id: str, key: str, value: Any) -> None:
    r = _redis()
    if r is not None:
        try:
            r.hset(f"da:st:{session_id}", key, json.dumps(value, default=str))
            _expire(r, session_id)
            return
        except Exception:
            pass  # Redis 故障 → 降级内存（记忆故障绝不打断主流程）
    _dict(session_id)[key] = value


def get(session_id: str, key: str, default: Any = None) -> Any:
    r = _redis()
    if r is not None:
        try:
            raw = r.hget(f"da:st:{session_id}", key)
            if raw is not None:
                return json.loads(raw)
            # Redis 里没有 → 再看内存兜底。写入降级时值只在内存里，
            # 若这里直接 return default，那份兜底就永远读不回来（读路径与写路径不对称）。
        except Exception:
            pass  # Redis 故障 → 降级内存
    return _store.get(session_id, {}).get(key, default)


def get_all(session_id: str) -> dict[str, Any]:
    r = _redis()
    if r is not None:
        try:
            raw = r.hgetall(f"da:st:{session_id}")
            if raw:
                _expire(r, session_id)  # 每轮开始读一次 → 顺势滑动续期
                return {k: json.loads(v) for k, v in raw.items()}
        except Exception:
            pass  # Redis 故障 → 降级内存
    return dict(_store.get(session_id, {}))
