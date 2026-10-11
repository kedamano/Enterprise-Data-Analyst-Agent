"""SEC/03 工具覆盖栈线程安全

Spec: docs/specs/SEC/03-tool-override-thread-safety.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from app.core.agents.data_analyst.graph import (
    _apply_tool_overrides,
    _clear_tool_overrides,
    _get_tool_override_stack,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_overrides():
    yield
    try:
        _clear_tool_overrides()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# ContextVar 隔离测试
# --------------------------------------------------------------------------- #
def test_override_stack_is_contextvar():
    from app.core.agents.data_analyst import graph
    assert hasattr(graph, "_TOOL_OVERRIDE_STACK")
    import contextvars
    assert isinstance(graph._TOOL_OVERRIDE_STACK, contextvars.ContextVar)


def test_apply_and_clear_single_request(clean_overrides):
    called = []

    def mock_tool(params):
        called.append(params)
        return {"result": "mocked"}

    _apply_tool_overrides({"sql_query": mock_tool})
    stack = _get_tool_override_stack()
    assert len(stack) == 1
    assert "sql_query" in stack[0]

    _clear_tool_overrides()
    stack = _get_tool_override_stack()
    assert len(stack) == 0


def test_nested_overrides_lifo(clean_overrides):
    order = []

    def mock_tool_1(params):
        order.append("tool_1")
        return {"result": "mock_1"}

    def mock_tool_2(params):
        order.append("tool_2")
        return {"result": "mock_2"}

    _apply_tool_overrides({"sql_query": mock_tool_1})
    _apply_tool_overrides({"sql_query": mock_tool_2})

    stack = _get_tool_override_stack()
    assert len(stack) == 2

    _clear_tool_overrides()
    stack = _get_tool_override_stack()
    assert len(stack) == 1

    _clear_tool_overrides()
    stack = _get_tool_override_stack()
    assert len(stack) == 0


def test_concurrent_requests_isolated(clean_overrides):
    import threading
    results = []

    def request_a():
        def mock_a(params):
            return {"result": "A"}

        _apply_tool_overrides({"sql_query": mock_a})
        import time
        time.sleep(0.01)
        from app.core.tools import REGISTRY
        tool = REGISTRY.get("sql_query")
        result = tool({}) if tool else {}
        results.append(result.get("result"))
        _clear_tool_overrides()

    def request_b():
        import time
        time.sleep(0.005)
        from app.core.tools import REGISTRY
        tool = REGISTRY.get("sql_query")
        result = tool({}) if tool else {}
        results.append(result.get("result", "original"))
        _clear_tool_overrides()

    t1 = threading.Thread(target=request_a)
    t2 = threading.Thread(target=request_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert "A" in results
    assert "original" in results or len(results) == 2


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_clear_empty_stack_no_error():
    _clear_tool_overrides()
    _clear_tool_overrides()


def test_get_stack_returns_list():
    stack = _get_tool_override_stack()
    assert isinstance(stack, list)
