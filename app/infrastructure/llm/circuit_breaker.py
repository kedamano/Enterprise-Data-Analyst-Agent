"""Three-state circuit breaker for the LLM gateway.

Mirrors ``project-python``'s ``circuit_breaker.py``: after ``failure_threshold``
consecutive failures the circuit trips OPEN and short-circuits (skips the
remote call entirely, degrading fast); after ``recovery_timeout_s`` it relaxes
to HALF_OPEN and lets a single probe through — success resets to CLOSED,
failure trips OPEN again. Guards the rate-limited / flaky upstream so a sick
endpoint cannot hang every request behind tenacity retries.
"""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """Raised when the circuit is OPEN and a call is short-circuited."""


class CircuitBreaker:
    """Thread-safe breaker. ``now`` is injectable for deterministic tests."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_s = max(0.0, recovery_timeout_s)
        self._now = now or time.monotonic
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            # Lazy OPEN→HALF_OPEN transition on read, so callers see it.
            self._maybe_relax()
            return self._state

    def _maybe_relax(self) -> None:
        if self._state is CircuitState.OPEN:
            if self._now() - self._opened_at >= self.recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN

    def allow_request(self) -> bool:
        with self._lock:
            self._maybe_relax()
            return self._state is not CircuitState.OPEN

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state is CircuitState.HALF_OPEN:
                self._trip_open()
            elif self._consecutive_failures >= self.failure_threshold:
                self._trip_open()

    def _trip_open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._now()

    def __call__(self, fn: Callable[..., T], *args: Any,
                 count_failure: Optional[Callable[[BaseException], bool]] = None,
                 **kwargs: Any) -> T:
        """执行 ``fn``；异常时按 ``count_failure`` 决定是否计入失败数。

        ``count_failure`` 的用途：**配置类错误**（400 无效模型 ID / 404 模型已下架）
        不代表"上游服务不可用"——把它们计入熔断，会让一个写错的模型名
        在 5 次之后把整条服务静默降级成 Mock（实测踩过）。
        """
        if not self.allow_request():
            raise CircuitOpenError(
                f"circuit {self._state.value}: short-circuiting (no remote call)"
            )
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if count_failure is None or count_failure(exc):
                self.record_failure()
            raise
        else:
            self.record_success()
            return result
