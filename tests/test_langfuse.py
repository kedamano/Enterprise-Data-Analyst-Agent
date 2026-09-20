"""LangFuse 集成测试。

Env 未配 LANGFUSE_SECRET_KEY/PUBLIC_KEY 时，所有函数必须为 no-op（绝不抛异常、
不发起网络请求）。配对 key 时，emit_span 应能进入 LangFuse client（本测试用
sys.modules 伪造一个最小 LangFuse stub 验证双写路径）。
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest


def _env_missing(monkeypatch):
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)


def test_init_returns_false_when_keys_missing(monkeypatch):
    _env_missing(monkeypatch)
    # 迫使 re-init
    import app.infrastructure.observability.langfuse as lf
    lf._langfuse_client = None
    lf._tracer = None
    lf._enabled = False
    assert lf.init() is False
    assert lf.is_enabled() is False


def test_emit_span_noop_when_disabled(monkeypatch):
    """Env 未配 → emit_span 不抛异常、不发网络。"""
    _env_missing(monkeypatch)
    import app.infrastructure.observability.langfuse as lf
    lf._langfuse_client = None
    lf._tracer = None
    lf._enabled = False
    # 不应抛
    lf.emit_span(
        name="planner",
        trace_id="abc",
        status="SUCCESS",
        duration_ms=120.0,
        input="q",
        output="p",
        metadata={"k": "v", "n": 1},
    )


def test_flush_noop_when_disabled(monkeypatch):
    _env_missing(monkeypatch)
    import app.infrastructure.observability.langfuse as lf
    lf._langfuse_client = None
    lf._tracer = None
    lf._enabled = False
    lf.flush()  # 不抛


def test_observe_span_yields_noop(monkeypatch):
    _env_missing(monkeypatch)
    import app.infrastructure.observability.langfuse as lf
    lf._langfuse_client = None
    lf._tracer = None
    lf._enabled = False
    with lf.observe_span("x", run_id="r", user_query="q") as span:
        span.update(output="anything")
    # noop span 应可视作 _NoopSpan


def test_emit_span_dual_writes_when_stub_present(monkeypatch, tmp_path):
    """配对 key + 伪造 LangFuse stub → emit_span 调 tracer().span() 并 end()。"""

    calls = []

    class FakeSpan:
        def __init__(self, **kw):
            calls.append(("span", kw))
        def end(self, *a, **kw):
            calls.append(("span_end", kw))

    class FakeTrace:
        def __init__(self, **kw):
            calls.append(("trace_init", kw))
        def span(self, **kw):
            return FakeSpan(**kw)

    fake = types.ModuleType("langfuse")
    class Langfuse:
        def __init__(self, **kw):
            calls.append(("client_init", kw))
        def trace(self, **kw):
            return FakeTrace(**kw)
        def flush(self):
            calls.append(("flush", {}))
    fake.Langfuse = Langfuse
    sys.modules["langfuse"] = fake

    try:
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        import app.infrastructure.observability.langfuse as lf
        lf._langfuse_client = None
        lf._tracer = None
        lf._enabled = False
        assert lf.init() is True
        lf.emit_span(name="analyst", trace_id="run-1", status="SUCCESS",
                     duration_ms=50.0, output="result")
        assert lf.flush() is None
        kinds = [c[0] for c in calls]
        assert "client_init" in kinds
        assert "trace_init" in kinds
        assert "span" in kinds
        assert "span_end" in kinds
        assert "flush" in kinds
    finally:
        del sys.modules["langfuse"]
