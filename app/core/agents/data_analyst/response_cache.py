"""INTERVIEW/01 ④ 请求级缓存：同问重复问直接命中（八股文 08.2 Token 成本控制）。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §4

分析师最典型的重复劳动是**把同一个问题再问一遍**（换台机器、给同事复现、隔天回来的确认）。
每次重跑全链要 5 次 LLM 调用；命中缓存则 **0 次**。

设计取舍：
- **按会话隔离**（key 含 session_id）：跨会话复用会泄漏数据，且多租户下不可接受。
- 只缓存 **FINISH**：`CLARIFY`/`ERROR`/`FAILED` 没有可复用结论。
- `force_full_rerun=true` **绕过并刷新**缓存（用户显式要求重算）。
- 命中时 `metadata["cache_hit"]=True` **显式透出**——绝不假装是新一轮分析（防假绿）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("da.cache")

# 进程内兜底（Redis 未配时用它；与 short_term 的降级策略一致）
_MEM: dict[str, dict[str, Any]] = {}


def normalize_query(query: str) -> str:
    """归一化：折叠空白 + 小写（大小写对语义无影响，能提高命中率）。"""
    return " ".join(str(query or "").split()).lower()


def cache_key(session_id: str, query: str, mode: str = "") -> str:
    raw = json.dumps([session_id or "", normalize_query(query), mode or ""], ensure_ascii=False)
    return "resp:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _client():
    try:
        from ....infrastructure.cache.redis import get_client

        return get_client()
    except Exception:
        return None


def get_cached(session_id: str, query: str, mode: str = "") -> Optional[dict]:
    """返回缓存的 state dict（调用方自行 model_validate 成**新对象**，避免共享可变状态）。"""
    key = cache_key(session_id, query, mode)
    client = _client()
    if client is not None:
        try:
            raw = client.get(key)
            if raw:
                payload = json.loads(raw)
                if time.time() - float(payload.get("_ts", 0)) < _ttl():
                    return payload.get("state")
        except Exception:
            pass  # 缓存故障静默降级（绝不打断主流程）
    hit = _MEM.get(key)
    if hit and time.time() - float(hit.get("_ts", 0)) < _ttl():
        return hit.get("state")
    if hit:
        _MEM.pop(key, None)
    return None


def put_cached(session_id: str, query: str, state: Any, mode: str = "") -> None:
    """只缓存**可信的成功**结果。

    三个不缓存的理由（每条都是踩过的）：
    - **非 FINISH**：`CLARIFY`/`ERROR`/`FAILED` 没有可复用结论；
    - **无报告**：没报告的"成功"复用价值为零；
    - **降级（degraded）**：LLM 不可用时兜底出的报告是**模板**，
      缓存它等于"真模型恢复了也照旧返回模板"，一小时 TTL 内都会骗人
      （DEGRADE/01 的教训：降级必须可见，更不能被当成可信结果复用）。
    """
    if getattr(state, "status", "") != "FINISH":
        return
    if not getattr(state, "report", ""):
        return
    try:
        if (getattr(state, "metadata", None) or {}).get("degraded"):
            logger.warning("降级结果不入缓存（避免模板报告被复用）")
            return
    except Exception:
        pass
    try:
        payload = {"state": state.model_dump(mode="json"), "_ts": time.time()}
    except Exception as exc:
        logger.warning("响应缓存序列化失败（跳过）：%s", exc)
        return
    key = cache_key(session_id, query, mode)
    client = _client()
    if client is not None:
        try:
            client.setex(key, int(_ttl()), json.dumps(payload, ensure_ascii=False))
            return
        except Exception:
            pass
    _MEM[key] = payload


def clear(session_id: Optional[str] = None) -> None:
    """清缓存（测试与运维用；给 session_id 时只清该会话）。"""
    if session_id is None:
        _MEM.clear()
        return
    prefix = cache_key(session_id, "", "")
    for k in [k for k in _MEM if k.startswith(prefix[:12])]:
        _MEM.pop(k, None)


def enabled() -> bool:
    try:
        from ....config import get_settings

        return bool(getattr(get_settings(), "response_cache_enabled", True))
    except Exception:
        return False


def _ttl() -> float:
    try:
        from ....config import get_settings

        return float(getattr(get_settings(), "response_cache_ttl_s", 3600) or 3600)
    except Exception:
        return 3600.0
