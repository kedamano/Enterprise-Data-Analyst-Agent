"""Vector store adapter – Milvus (optional).

Returns a client only when Milvus is configured; otherwise ``None`` and callers
fall back to the local SQLite knowledge store. Keeps the dependency optional so
the project boots without a running Milvus.

Three ways to point at a backend (priority top-down):

1. ``MILVUS_LITE_PATH`` — a local path, using **Milvus Lite** (embedded,
   single-process, no server; it creates a *directory* at that path). Makes the
   real vector store exercisable in local dev / CI without Docker, e.g.
   ``MILVUS_LITE_PATH=./data/milvus_lite.db``.
   pymilvus **hard-requires** local URIs to end with ``.db``; we append it
   automatically when missing so ``MILVUS_LITE_PATH=./data/milvus_lite`` works too.
2. ``MILVUS_URI`` — ``http://host:19530`` (real server).
3. ``MILVUS_HOST`` + ``MILVUS_PORT`` — kept for backward compatibility.

.. warning::
   Do **not** put a file path in ``MILVUS_URI``. That variable is pymilvus' own
   global and pymilvus parses it as ``http(s)://...`` at import time, so a path
   there makes ``import pymilvus`` fail outright. Use ``MILVUS_LITE_PATH`` for
   the embedded file backend.
"""
from __future__ import annotations

import logging
from typing import Any

from ...config import get_settings

logger = logging.getLogger("da.vectorstore.milvus")

_LITE_SUFFIX = ".db"


def _as_lite_path(path: str) -> str:
    """Milvus Lite 本地路径规范化：pymilvus 要求以 ``.db`` 结尾（否则 URI 非法）。"""
    p = path.strip().rstrip("\\/")
    if not p:
        return p
    return p if p.lower().endswith(_LITE_SUFFIX) else p + _LITE_SUFFIX


def resolve_uri() -> str | None:
    """Return the Milvus URI to connect to, or ``None`` when unconfigured."""
    settings = get_settings()
    lite = (getattr(settings, "milvus_lite_path", "") or "").strip()
    if lite:
        return _as_lite_path(lite)
    uri = (getattr(settings, "milvus_uri", "") or "").strip()
    if uri:
        return uri
    host = (settings.milvus_host or "").strip()
    if not host:
        return None
    return f"http://{host}:{settings.milvus_port}"


# 观测态（与 app.infrastructure.llm.router 的 _llm_state 同构）：
# 「配了 Milvus」是**配置意图**，「真的连上了」是**观测事实**。两者必须分开报，
# 否则 URI 写错 / 服务没起会与"压根没配"表现成同一个结果（静默降级，铁律 3 禁止）。
_state: dict[str, Any] = {"last_error": None, "last_uri": None}


def milvus_last_error() -> str | None:
    """最近一次「已配置但连接失败」的原因；未配置或连接成功则为 ``None``。"""
    return _state.get("last_error")


def milvus_last_uri() -> str | None:
    """最近一次尝试连接的 URI（脱敏前，仅用于排障展示）。"""
    return _state.get("last_uri")


def reset_milvus_state() -> None:
    """仅供测试：清空观测态。"""
    _state["last_error"] = None
    _state["last_uri"] = None


def get_client() -> Any | None:
    uri = resolve_uri()
    if not uri:
        # 未配置 → 调用方回退 SQLite 是**预期**行为，不是降级，故不告警。
        _state["last_error"] = None
        _state["last_uri"] = None
        return None
    _state["last_uri"] = uri
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=uri)
    except Exception as exc:  # noqa: BLE001 - 回退是设计，但绝不能无声
        # 配了 Milvus 却拿不到客户端，**必须留痕**：否则"URI 写错/服务没起"
        # 会与"压根没配"表现成同一个结果（静默降级，铁律 3 禁止）。
        _state["last_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Milvus 已配置但连接失败，回退 SQLite 知识库：uri=%s err=%s: %s",
            uri,
            type(exc).__name__,
            exc,
        )
        return None
    _state["last_error"] = None
    return client
