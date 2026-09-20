"""Observability – structured span tracing + a bounded recent-span buffer.

Every orchestration node (context/planner/executor/analyst/reflection/reporter)
emits a structured ``Span`` {trace_id, span_id, stage, status, duration_ms,
error, started_at}. Spans created inside a ``trace_run`` context are persisted
as JSONL under ``data/traces/{run_id}.jsonl``; all spans (including ad-hoc node
calls) land in a bounded in-memory buffer queryable via ``recent_spans`` and the
``/debug/traces`` endpoint. An OpenTelemetry exporter can be added later by
consuming the same span dicts — this module is the structured source of truth.
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import pathlib
import re
import time
import uuid
from collections import deque
from typing import Any, Callable, Iterator, Optional

try:
    from loguru import logger as _log  # type: ignore
except Exception:  # pragma: no cover
    _log = logging.getLogger("da")  # type: ignore

# Bug 修复（2026-09-16）：原先 sampling 的 import 与 loguru 绑在同一个 try/except 里，
# loguru 缺失时 should_log_span 一并未定义 → Tracer.end() NameError，
# 把节点真正的异常整个替换掉（初始化阶段的原始错误被吞、只见 NameError）。
try:
    from .sampling import should_log_span
except Exception:  # pragma: no cover
    def should_log_span(ok: bool) -> bool:
        return True  # 采样器不可用 → 全记（铁律：失败一条不漏）

_DEFAULT_DIR = pathlib.Path("data/traces")
_RECENT: deque = deque(maxlen=400)  # newest appended at the end
_current_tracer: contextvars.ContextVar = contextvars.ContextVar("da_tracer", default=None)


def setup_logging(level: str = "INFO") -> None:
    try:
        from loguru import logger as lg
        lg.remove()
        lg.add(lambda msg: print(msg, end=""), level=level, colorize=True)
    except Exception:
        logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO))


def _safe_run_id(run_id: str) -> str:
    return re.sub(r"[^\w-]", "_", run_id)[:80] or uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# Span / Tracer
# --------------------------------------------------------------------------- #
class Span:
    def __init__(self, tracer: "Tracer", stage: str, index: int) -> None:
        self.trace_id = tracer.run_id
        self.span_id = uuid.uuid4().hex[:12]
        self.stage = stage
        self.index = index
        self.status = "RUNNING"
        self.error: Optional[str] = None
        self._started_ns = time.perf_counter_ns()
        self.duration_ms: Optional[float] = None
        self.prompt_tokens: Optional[int] = None   # LLM usage（由网关上报）
        self.completion_tokens: Optional[int] = None

    def finish(self, ok: bool, error: Optional[BaseException] = None) -> dict[str, Any]:
        self.status = "SUCCESS" if ok else "ERROR"
        self.duration_ms = round((time.perf_counter_ns() - self._started_ns) / 1e6, 2)
        self.error = str(error) if error else None
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "stage": self.stage,
            "index": self.index,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "started_at": round(self._started_ns / 1e9, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class Tracer:
    def __init__(self, run_id: str, persist: bool, trace_dir: Optional[pathlib.Path] = None) -> None:
        self.run_id = run_id
        self.persist_enabled = persist
        self.trace_dir = pathlib.Path(trace_dir) if trace_dir else _DEFAULT_DIR
        self.spans: list[Span] = []
        self.started_at = time.time()
        self._idx = 0
        self._active: Optional[Span] = None

    def start(self, stage: str) -> Span:
        self._idx += 1
        span = Span(self, stage, self._idx)
        self.spans.append(span)
        self._active = span
        return span

    def end(self, span: Span, ok: bool = True, error: Optional[BaseException] = None) -> None:
        data = span.finish(ok, error)
        # D44：日志采样——**只采"正常"，失败永不采样**（采掉失败=故障自愈）。
        if should_log_span(ok=span.status == "SUCCESS"):
            _log.info(f"[span] {span.stage:<10} {span.status:<7} "
                      f"{span.duration_ms}ms  run={span.trace_id}")
        _RECENT.append(data)
        # LangFuse 联动：每个 span 结束都往 LangFuse 推一个观测块（env 未配置时为 noop）。
        try:
            from .langfuse import emit_span
            emit_span(
                name=span.stage,
                trace_id=self.run_id,
                status=span.status,
                duration_ms=getattr(span, "duration_ms", None),
                input=getattr(span, "input", None),
                output=getattr(span, "output", None),
                metadata={
                    k: v for k, v in getattr(span, "metadata", {}).items()
                    if isinstance(v, (str, int, float, bool)) and k != "error"
                },
            )
        except Exception:
            pass
        if self._active is span:
            self._active = None

    def summary(self) -> dict[str, Any]:
        has_error = any(s.status == "ERROR" for s in self.spans)
        return {
            "trace_id": self.run_id,
            "spans": len(self.spans),
            "status": "ERROR" if has_error else "OK",
            "duration_s": round(time.time() - self.started_at, 3),
            "stages": [s.stage for s in self.spans],
            "prompt_tokens": sum(s.prompt_tokens or 0 for s in self.spans),
            "completion_tokens": sum(s.completion_tokens or 0 for s in self.spans),
        }

    def persist(self) -> Optional[pathlib.Path]:
        if not self.persist_enabled or not self.spans:
            return None
        safe = _safe_run_id(self.run_id)
        target = self.trace_dir / f"{safe}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(s.to_dict(), ensure_ascii=False) for s in self.spans]
        lines.append(json.dumps(self.summary(), ensure_ascii=False))
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def recent_spans(limit: int = 50) -> list[dict[str, Any]]:
    """Most-recently-ended spans, newest last per deque order → reverse for newest first."""
    return list(_RECENT)[-limit:][::-1]


def current_run_id() -> Optional[str]:
    """当前 trace 上下文的 run_id（无上下文 → None）。

    DEGRADE/01：LLM 降级事件据此归因到具体某次运行，避免"这个进程降级过"这种
    无法定位到请求的粗粒度信息。
    """
    tracer = _current_tracer.get()
    return getattr(tracer, "run_id", None) if tracer is not None else None


def record_tokens(prompt_tokens: int, completion_tokens: int,
                  tracer: Optional["Tracer"] = None) -> None:
    """Attach LLM usage to the currently-open span (called by the LLM gateway)."""
    t = tracer or _current_tracer.get()
    if t is not None and t._active is not None:
        span = t._active
        span.prompt_tokens = (span.prompt_tokens or 0) + int(prompt_tokens or 0)
        span.completion_tokens = (span.completion_tokens or 0) + int(completion_tokens or 0)
    # #3 可观测性：累计 LLM token / 成本指标（供 /metrics 抓取）
    try:
        from .metrics import record_llm_call

        record_llm_call(int(prompt_tokens or 0), int(completion_tokens or 0), None)
    except Exception:
        pass


def load_run(run_id: str, trace_dir: Optional[pathlib.Path] = None) -> list[dict[str, Any]]:
    """Read a persisted trace back as span dicts (last line is the summary)."""
    target = (pathlib.Path(trace_dir) if trace_dir else _DEFAULT_DIR) / f"{_safe_run_id(run_id)}.jsonl"
    if not target.exists():
        return []
    out = []
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class trace_run:
    """Context manager: collect spans for a run and persist them on exit.

    Inside the block the module-level ``@trace`` decorator attaches its spans to
    this tracer (same trace_id). Used by ``run_analysis`` / ``stream_analysis``.
    """

    def __init__(
        self,
        run_id: Optional[str] = None,
        trace_dir: Optional[pathlib.Path] = None,
    ) -> None:
        self._tracer = Tracer(run_id=run_id or uuid.uuid4().hex, persist=True, trace_dir=trace_dir)
        self.tracer = self._tracer
        self._token = None

    def __enter__(self) -> Tracer:
        self._token = _current_tracer.set(self._tracer)
        return self._tracer

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            try:
                _current_tracer.reset(self._token)
            except ValueError:
                # SSE/StreamingResponse 跨线程迭代时，__exit__ 与 __enter__ 可能不在
                # 同一 Context：reset 会抛 "created in a different Context"。
                # 此时把当前上下文的值清掉即可（原始 context 的残留随生成器结束而无关紧要）。
                try:
                    _current_tracer.set(None)
                except Exception:
                    pass
        self._tracer.persist()


def trace(name: Optional[str] = None) -> Callable:
    """Decorate a node: emit a structured span around the call.

    When called inside a ``trace_run`` block the span joins that run's trace;
    standalone calls create an ephemeral tracer (recorded in the recent buffer,
    not persisted) so node-level unit tests stay side-effect free on disk.
    """
    def decorator(fn: Callable) -> Callable:
        stage = name or fn.__name__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tracer = _current_tracer.get()
            owned = False
            if tracer is None:
                run_id = None
                if args and hasattr(args[0], "session_id"):
                    run_id = getattr(args[0], "session_id")
                tracer = Tracer(run_id=run_id or uuid.uuid4().hex, persist=False)
                token = _current_tracer.set(tracer)
                owned = True
            span = tracer.start(stage)
            try:
                result = fn(*args, **kwargs)
                tracer.end(span, ok=True)
                return result
            except Exception as exc:
                tracer.end(span, ok=False, error=exc)
                raise
            finally:
                if owned:
                    _current_tracer.reset(token)

        return wrapper

    return decorator


# Re-export for callers that imported the old iterator-style API.
def iter_spans(trace_id: str, trace_dir: Optional[pathlib.Path] = None) -> Iterator[dict[str, Any]]:
    yield from load_run(trace_id, trace_dir)
