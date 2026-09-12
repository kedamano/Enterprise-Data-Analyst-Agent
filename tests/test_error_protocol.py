"""TDD suite for the Error Handling Protocol (spec §23).

Contract under test:

* ``classify_error`` distinguishes Retryable (timeout / connection / rate
  limit / transient infra) from Non-Retryable (invalid SQL / schema / params /
  unauthorized) errors. Unknown errors default to Non-Retryable (safe side).
* ``execute_tool`` retries Retryable failures, bounded by
  ``max_tool_retries`` (spec: 2~3, never infinite).
* Every failed ToolResult carries ``error_class`` so the Reflection / Planner
  stages can route (REPLAN vs FAIL) on evidence.
"""
from __future__ import annotations

import pytest

from app.core.tools import REGISTRY, execute_tool
from app.core.tools.errors import ErrorClass, classify_error


# --------------------------------------------------------------------------- #
# 1. classify_error
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message", [
    "执行超时 (30s)",
    "connection reset by peer",
    "Connection refused",
    "HTTP 503 service unavailable",
    "rate limit exceeded, retry later",
    "too many requests",
    "database is locked",
    "temporarily unavailable",
])
def test_retryable_errors(message):
    assert classify_error(message) is ErrorClass.RETRYABLE, message


@pytest.mark.parametrize("message", [
    "no such column: revenue_usd",
    "no such table: fact_sale",
    "syntax error near 'FROM'",
    "只读模式禁止写操作 (DROP/DELETE/...)",
    "缺少 sql 参数",
    "禁止导入模块: os",
    "permission denied",
    "unknown tool: foo",
])
def test_non_retryable_errors(message):
    assert classify_error(message) is ErrorClass.NON_RETRYABLE, message


def test_unknown_error_defaults_to_non_retryable():
    # 分类器不认识的一律不重试——宁可 REPLAN 也不无限重试
    assert classify_error("完全未知的奇怪错误") is ErrorClass.NON_RETRYABLE


# --------------------------------------------------------------------------- #
# 2. Bounded retry in execute_tool
# --------------------------------------------------------------------------- #
class _FlakyTool:
    """Fails N times with a retryable error, then succeeds."""

    def __init__(self, failures: int, error: str = "connection reset by peer") -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def __call__(self, params: dict, workdir: str | None = None) -> dict:
        self.calls += 1
        if self.calls <= self.failures:
            return {"ok": False, "error": self.error}
        return {"ok": True, "rows": [{"n": self.calls}]}


@pytest.fixture
def flaky():
    tool = _FlakyTool(failures=1)
    REGISTRY["_flaky_test_tool"] = tool
    yield tool
    REGISTRY.pop("_flaky_test_tool", None)


def test_retryable_failure_is_retried_and_succeeds(flaky):
    res = execute_tool("r1", "_flaky_test_tool", {}, "err_proto")
    assert res.status == "SUCCESS", res.error
    assert flaky.calls == 2, "应在第一次失败后重试一次"
    assert res.attempts == 2


def test_non_retryable_failure_fails_fast(flaky):
    flaky.error = "no such column: xyz"  # 不可重试
    res = execute_tool("r2", "_flaky_test_tool", {}, "err_proto")
    assert res.status == "FAILED"
    assert flaky.calls == 1, "不可重试错误不应重试"
    assert res.attempts == 1
    assert res.error_class == "NON_RETRYABLE"


def test_retry_is_bounded_not_infinite(flaky):
    flaky.failures = 99  # 永远失败
    res = execute_tool("r3", "_flaky_test_tool", {}, "err_proto")
    assert res.status == "FAILED"
    assert res.error_class == "RETRYABLE"
    # 1 次原始调用 + max_tool_retries 次重试（默认 2），绝不能是 99
    from app.config import get_settings
    assert res.attempts == 1 + get_settings().max_tool_retries
    assert flaky.calls == res.attempts


def test_successful_tool_records_no_error_class(flaky):
    flaky.failures = 0
    res = execute_tool("r4", "_flaky_test_tool", {}, "err_proto")
    assert res.status == "SUCCESS"
    assert res.error_class is None
    assert res.attempts == 1
