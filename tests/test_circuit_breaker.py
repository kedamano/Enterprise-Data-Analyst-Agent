"""TDD suite for the three-state LLM circuit breaker.

Two layers:

* Pure unit tests of the ``CircuitBreaker`` state machine with an injected
  clock (deterministic OPEN → HALF_OPEN → CLOSED/OPEN transitions).
* Integration: ``OpenAILLM.complete`` wired to a breaker — after
  ``failure_threshold`` failed calls the circuit trips OPEN and subsequent
  calls short-circuit WITHOUT touching the (counted) upstream stub.

Contract:
* ``allow_request()`` False once OPEN; callers must not hit the network.
* A single failure in HALF_OPEN re-opens; a success closes & resets.
* Success in CLOSED resets the consecutive-failure counter.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.infrastructure.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)
from app.infrastructure.llm.router import OpenAILLM, reset_fallback_events


# --------------------------------------------------------------------------- #
# Deterministic clock
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _flaky(fn):
    def _fail(*a, **k):
        raise ConnectionError("upstream down")

    return _fail


def _ok():
    return "good"


# --------------------------------------------------------------------------- #
# 1. State machine unit tests
# --------------------------------------------------------------------------- #
def test_starts_closed():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=60, now=FakeClock())
    assert cb.state is CircuitState.CLOSED
    assert cb.allow_request() is True


def test_trips_open_after_threshold_failures():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=60, now=FakeClock())
    for _ in range(3):
        with pytest.raises(ConnectionError):
            cb(_flaky(None))
    assert cb.state is CircuitState.OPEN
    assert cb.allow_request() is False


def test_open_short_circuits_without_invoking():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=60, now=FakeClock())
    calls = {"n": 0}

    def counted_fail(*a, **k):
        calls["n"] += 1
        raise ConnectionError

    with pytest.raises(ConnectionError):
        cb(counted_fail)
    assert cb.state is CircuitState.OPEN
    # 后续调用：不执行 fn，直接抛 CircuitOpenError
    with pytest.raises(CircuitOpenError):
        cb(counted_fail)
    assert calls["n"] == 1, "Open 态不应再触碰上游"


def test_recovers_to_half_open_after_timeout():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=60, now=clock)
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    assert cb.state is CircuitState.OPEN
    clock.advance(60)
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.allow_request() is True


def test_half_open_success_resets_to_closed():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=60, now=clock)
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    clock.advance(60)
    assert cb.state is CircuitState.HALF_OPEN
    assert cb(_ok) == "good"
    assert cb.state is CircuitState.CLOSED
    assert cb.allow_request() is True


def test_half_open_failure_returns_to_open():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=60, now=clock)
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    clock.advance(60)  # HALF_OPEN
    with pytest.raises(ConnectionError):
        cb(_flaky(None))  # 探测失败 → 回到 OPEN
    assert cb.state is CircuitState.OPEN
    # 需再等 recovery 窗口才允许探测
    clock.advance(59)
    assert cb.allow_request() is False
    clock.advance(1)
    assert cb.allow_request() is True


def test_closed_success_resets_failure_counter():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=60, now=clock)
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    assert cb.state is CircuitState.CLOSED  # 未达阈值
    assert cb(_ok) == "good"  # 成功清零
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    with pytest.raises(ConnectionError):
        cb(_flaky(None))
    assert cb.state is CircuitState.CLOSED  # 清零后需重新累计


# --------------------------------------------------------------------------- #
# 2. OpenAILLM wiring
# --------------------------------------------------------------------------- #
class _CountingStub:
    """上游桩：计数并在调用时抛错，代替真实 OpenAI client。"""

    def __init__(self) -> None:
        self.calls = 0

    def create(self, *a, **k):
        self.calls += 1
        raise ConnectionError("upstream refused")


def _make_llm() -> tuple[OpenAILLM, _CountingStub]:
    reset_fallback_events()
    settings = Settings(
        llm_api_key="sk-dummy",            # 覆盖 .env
        llm_base_url="http://127.0.0.1:9/v1",  # 不可达端口（正常不会真连）
        llm_model="dummy",
        llm_max_retries=1,                 # 每次请求只打一发
        llm_timeout_s=5,
        llm_no_fallback=False,
        mock_llm=False,
        cb_failure_threshold=3,
        cb_recovery_timeout_s=60.0,
    )
    llm = OpenAILLM(settings)
    stub = _CountingStub()
    # 用计数桩替换真实 client，确保离线且可断言调用次数
    llm._client = type("Stub", (), {"chat": type("Chat", (), {"completions": stub})()})()
    return llm, stub


def test_openai_complete_trips_open_then_short_circuits():
    from app.infrastructure.llm.router import fallback_events

    llm, stub = _make_llm()
    # 3 次失败 → OPEN；每次失败都因 no_fallback=False 降级返回 mock 文本（不抛）
    for _ in range(3):
        out = llm.complete("sys", "user", stage="context", json_mode=True)
        assert out and isinstance(out, str)
    assert stub.calls == 3
    assert llm._breaker.state is CircuitState.OPEN
    # 第 4 次：OPEN 短路线——不再触碰上游，直接降级返回
    out = llm.complete("sys", "user", stage="context", json_mode=True)
    assert isinstance(out, str)
    assert stub.calls == 3, "Open 态不应再调用上游"
    assert len(fallback_events()) >= 4


def test_openai_complete_raises_when_no_fallback_on_open():
    from app.infrastructure.llm.router import LLMError

    reset_fallback_events()
    settings = Settings(
        llm_api_key="sk-dummy",
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="dummy",
        llm_max_retries=1,
        llm_timeout_s=5,
        llm_no_fallback=True,
        mock_llm=False,
        cb_failure_threshold=2,
        cb_recovery_timeout_s=60.0,
    )
    llm = OpenAILLM(settings)
    stub = _CountingStub()
    llm._client = type("Stub", (), {"chat": type("Chat", (), {"completions": stub})()})()
    # 前 2 次失败 → no_fallback=True 时直接 raise（不降级）
    for _ in range(2):
        with pytest.raises(Exception):
            llm.complete("sys", "u", stage="context", json_mode=True)
    assert llm._breaker.state is CircuitState.OPEN
    # Open 态且 no_fallback：仍 raise（短路 → degrade 分支同样 raise）
    with pytest.raises(Exception):
        llm.complete("sys", "u", stage="context", json_mode=True)
    assert stub.calls == 2, "Open 态不应再调用上游"


# --------------------------------------------------------------------------- #
# 配置类错误不得计入熔断（2026-09-12 真实评测暴露）
# --------------------------------------------------------------------------- #
def test_config_error_does_not_trip_breaker():
    """写错的模型 ID（400/404/422）不是"上游不可用"，不该把服务降级成 Mock。

    实测事故：免费池里一个 slug 已下架（404）、一个 ID 写错（400），
    两者都被计入熔断 → 5 次后 circuit OPEN → 之后所有调用短路走 Mock，
    "真实评测"其实大部分是 mock 跑出来的。
    """
    from app.infrastructure.llm.circuit_breaker import CircuitBreaker
    from app.infrastructure.llm.router import _config_error

    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=60)

    class _Http404(Exception):
        status_code = 404

    for _ in range(4):  # 远超阈值
        with pytest.raises(_Http404):
            cb(lambda: (_ for _ in ()).throw(_Http404()),
               count_failure=lambda exc: not _config_error(exc))

    assert cb.allow_request() is True, "配置类错误不该打开熔断"

    # 对照：真正的服务错误（500）仍应正常计数并打开
    class _Http500(Exception):
        status_code = 500

    cb2 = CircuitBreaker(failure_threshold=2, recovery_timeout_s=60)
    for _ in range(2):
        with pytest.raises(_Http500):
            cb2(lambda: (_ for _ in ()).throw(_Http500()),
                count_failure=lambda exc: not _config_error(exc))
    assert cb2.allow_request() is False, "服务级错误必须照常熔断"
