"""LangFuse observability integration (opt-in via env).

Design: env-gated. No-op unless LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY are set.
This keeps local dev and mock-mode runs free of external dependencies.

Usage:
    from .langfuse import get_langfuse_context, observe_span

    with observe_span("planner", run_id=session_id, user_query=query) as span:
        result = run_planner(state)
        span.update(output=result[:200])

Alternatively decorator:
    @observe(stage="analyst")
    def run_analyst(state): ...
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("observability.langfuse")

_langfuse_client = None
_tracer = None
_enabled = False


def init() -> bool:
    """Lazy init LangFuse client (idempotent). Returns True if enabled."""
    global _langfuse_client, _tracer, _enabled
    if _langfuse_client is not None or _enabled:
        return _enabled
    from ...config import get_settings
    st = get_settings()
    secret = st.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY", "")
    public = st.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY", "")
    host = st.langfuse_host or os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    if not (secret and public):
        _enabled = False
        return False
    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            secret_key=secret,
            public_key=public,
            host=host,
        )
        _tracer = _langfuse_client.trace
        _enabled = True
        logger.info("LangFuse enabled (host=%s)", host)
    except ImportError:
        logger.warning("langfuse package not installed; observability falls through to local trace")
        _enabled = False
    except Exception as exc:
        logger.warning("LangFuse init failed (%s); falling back to local trace only", exc)
        _enabled = False
    return _enabled


def is_enabled() -> bool:
    return init()


@contextlib.contextmanager
def observe_span(
    name: str,
    run_id: str = "",
    user_query: str = "",
    metadata: Optional[dict] = None,
) -> Iterator[Any]:
    """Context manager that yields a LangFuse span (or no-op sentinel).

    Usage:
        with observe_span("planner", run_id=sid, user_query=q) as span:
            result = actual_work()
            span.update(output=result)
    """
    if not is_enabled():
        yield _NoopSpan()
        return
    try:
        trace = _tracer(id=run_id or None, name=f"agent-{name}", user_id="agent", metadata=metadata or {})
        span = trace.span(name=name, input=user_query)
        try:
            yield span
        finally:
            span.end()
    except Exception as exc:
        logger.warning("LangFuse span %s failed (non-blocking): %s", name, exc)
        yield _NoopSpan()


def observe(stage: str) -> Callable:
    """Decorator factory: @observe("analyst") def run_analyst(...): ..."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with observe_span(stage, run_id=_extract_session_id(args, kwargs)):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def emit_span(
    *,
    name: str,
    trace_id: str,
    status: str,
    duration_ms: float | None = None,
    input: Any = None,
    output: Any = None,
    metadata: dict | None = None,
) -> None:
    """推一个 span 级观测到 LangFuse（env 未配置 / 包未装 → noop）。

    由 ``tracing.Tracer.end`` 在每次本地 span 落盘后回调，把同一份观测双写到
    LangFuse 云端——本地 trace JSONL 与云端 trace 互不依赖、互为备份。
    """
    if not is_enabled():
        return
    try:
        trace = _tracer(
            id=trace_id or None,
            name=f"agent-{name}",
            metadata={**(metadata or {}), "local_stage": name},
        )
        span = trace.span(
            name=name,
            input=input if input is not None else "",
            output=output if output is not None else "",
            level="WARNING" if status == "ERROR" else "DEFAULT",
        )
        span.end()
    except Exception as exc:
        logger.warning("LangFuse emit_span %s failed (non-blocking): %s", name, exc)


def flush() -> None:
    if is_enabled() and _langfuse_client:
        try:
            _langfuse_client.flush()
        except Exception as exc:
            logger.warning("LangFuse flush error: %s", exc)


class _NoopSpan:
    def update(self, *args, **kwargs): pass
    def end(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _extract_session_id(args: tuple, kwargs: dict) -> str:
    """尝试从参数里抽 session_id（agent node 的第一参数通常是 state 或 session_id str）。"""
    if "session_id" in kwargs:
        return kwargs["session_id"]
    if args and isinstance(args[0], str):
        return args[0]
    return ""
