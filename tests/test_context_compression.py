"""Tests for multi-turn context window governance (多轮上下文窗口治理).

Run: conda run -n base python -m pytest tests/test_context_compression.py -q
"""
from __future__ import annotations

from unittest import mock

import pytest

from app.core.memory.compression import (
    ContextCompressor,
    ContextOverflow,
    _compress_context,
    summarize_report_for_history,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class FakeToolResult:
    """Minimal stand-in for state.tool_results items."""

    def __init__(self, output: dict | str):
        if isinstance(output, dict):
            self.output = output
        else:
            self.output = {"content": output}


class FakeState:
    """Minimal stand-in for AgentState — only the fields _compress_context touches."""

    def __init__(self, **kwargs):
        self.user_query = kwargs.get("user_query", "test")
        self.context = kwargs.get("context", None)
        self.plan = kwargs.get("plan", None)
        self.tool_results = kwargs.get("tool_results", [])
        self.report = kwargs.get("report", "")
        self.conversation_history = kwargs.get("conversation_history", [])
        self.metadata = kwargs.get("metadata", {})


@pytest.fixture
def default_settings():
    """Default settings fixture with compression enabled."""
    return mock.MagicMock(
        context_compression_enabled=True,
        context_max_tokens=60_000,
        context_compress_threshold=0.8,
        context_hard_max_tokens=80_000,
        context_compress_tool_result_chars=500,
        context_compress_report_chars=300,
    )


@pytest.fixture
def disabled_settings():
    """Settings with compression disabled."""
    return mock.MagicMock(
        context_compression_enabled=False,
        context_max_tokens=60_000,
        context_compress_threshold=0.8,
        context_hard_max_tokens=80_000,
        context_compress_tool_result_chars=500,
        context_compress_report_chars=300,
    )


# --------------------------------------------------------------------------- #
# 1. Short tool_result → passthrough
# --------------------------------------------------------------------------- #
def test_truncation_short_enough():
    """tool_result 只有 100 字符 → 不触发压缩，原文 passthrough。"""
    raw = "A" * 100
    result = ContextCompressor.compress_tool_result(raw, max_budget_chars=500)
    assert result == raw
    assert len(result) == 100


# --------------------------------------------------------------------------- #
# 2. Long tool_result → compressed within budget
# --------------------------------------------------------------------------- #
def test_truncation_long_tool_result():
    """tool_result = 5000 字符 → 压缩到 ≤ context_compress_tool_result_chars (500)。"""
    raw = "\n".join(f"row_{i},value_{i*3.14:.2f}" for i in range(200))  # ~5000 chars
    assert len(raw) > 500

    result = ContextCompressor.compress_tool_result(raw, max_budget_chars=500)
    assert len(result) <= 500
    assert "[已压缩]" in result


# --------------------------------------------------------------------------- #
# 3. Report summary within limit + contains key fields
# --------------------------------------------------------------------------- #
def test_report_executive_summary():
    """report = 2000+ 字符 → 输出 ≤ context_compress_report_chars 且含关键数值。"""
    # 构造一段带数值的长报告
    report = (
        "2025年Q3营收分析：总营收为 1,234.5 万元，同比增长 15.3%。\n" * 50
        + "用户活跃度达到 8,888 人，环比下降 2.1%。\n" * 50
    )
    assert len(report) > 300

    summary = summarize_report_for_history(report, max_chars=300)
    assert len(summary) <= 300
    # 必须含关键指标数值（来自 regex 提取）
    assert "1,234.5" in summary or "15.3" in summary or "8,888" in summary


# --------------------------------------------------------------------------- #
# 4. Token count estimation
# --------------------------------------------------------------------------- #
def test_token_count_estimation():
    """messages = 10 条 100 字符 → 返回 int > 0 且 < 实际字符数/2（保守估算）。"""
    messages = [{"role": "user", "content": "A" * 100} for _ in range(10)]
    actual_chars = sum(len(m["content"]) for m in messages)  # 1000

    tokens = ContextCompressor.count_messages_tokens(messages)
    assert isinstance(tokens, int)
    assert tokens > 0
    # 保守估算：tokens 应小于 actual_chars/2
    assert tokens < actual_chars // 2


# --------------------------------------------------------------------------- #
# 5. compress stats structure
# --------------------------------------------------------------------------- #
def test_compress_stats_structure(default_settings):
    """compress 出 dict 包含 before_tokens / after_tokens / triggered。"""
    state = FakeState(
        user_query="分析营收",
        tool_results=[FakeToolResult({"rows": ["A" * 100]})],
    )

    with mock.patch("app.core.memory.compression.get_settings", return_value=default_settings):
        _, stats = _compress_context(state)

    assert isinstance(stats, dict)
    assert "before_tokens" in stats
    assert "after_tokens" in stats
    assert "triggered" in stats
    assert isinstance(stats["before_tokens"], int)
    assert isinstance(stats["after_tokens"], int)
    assert isinstance(stats["triggered"], bool)


# --------------------------------------------------------------------------- #
# 6. No compression when disabled
# --------------------------------------------------------------------------- #
def test_no_compression_when_disabled(disabled_settings):
    """context_compression_enabled=False → 输出与输入(messages) 完全一致。"""
    long_result = "X" * 5000
    state = FakeState(
        user_query="分析营收",
        tool_results=[FakeToolResult({"rows": [long_result]})],
        report="Y" * 2000,
    )

    with mock.patch("app.core.memory.compression.get_settings", return_value=disabled_settings):
        result_state, stats = _compress_context(state)

    # tool_results 未被压缩（原文保留）
    tr = result_state.tool_results[0]
    assert long_result in (tr.output.get("content") or str(tr.output))
    # report 未被压缩
    assert result_state.report == "Y" * 2000
    # stats 表明未触发
    assert stats["triggered"] is False
    assert stats["tool_results_compressed"] == 0


# --------------------------------------------------------------------------- #
# Additional sanity: ContextOverflow triggered when post-compression > hard_max
# --------------------------------------------------------------------------- #
def test_context_overflow_when_over_hard_limit(default_settings):
    """构造极端场景：即使压缩后仍超 hard_max → raise ContextOverflow。"""
    # 超大数据：tool_results 每项都远超压缩预算，压缩后仍超 80k*4 chars
    # 模拟：把 hard_max 设得很小，强制溢出
    small_hard = mock.MagicMock(
        context_compression_enabled=True,
        context_max_tokens=10,       # 极小 → threshold=8 tokens 必触发
        context_compress_threshold=0.8,
        context_hard_max_tokens=20,  # 极小 → 必溢出
        context_compress_tool_result_chars=500,
        context_compress_report_chars=300,
    )
    state = FakeState(
        user_query="A" * 100,  # 超 20 tokens
    )

    with mock.patch("app.core.memory.compression.get_settings", return_value=small_hard):
        with pytest.raises(ContextOverflow):
            _compress_context(state)


# --------------------------------------------------------------------------- #
# would_overflow check
# --------------------------------------------------------------------------- #
def test_would_overflow_true_for_large_messages():
    """超大 messages → would_overflow 返回 True。"""
    messages = [{"content": "A" * 10_000} for _ in range(20)]  # 200k chars → ~50k tokens
    assert ContextCompressor.would_overflow(messages, max_ctx=8000) is True


def test_would_overflow_false_for_small_messages():
    """小 messages → would_overflow 返回 False。"""
    messages = [{"content": "hi"} for _ in range(3)]
    assert ContextCompressor.would_overflow(messages, max_ctx=100_000) is False


# --------------------------------------------------------------------------- #
# compress_via_llm falls back gracefully
# --------------------------------------------------------------------------- #
def test_compress_via_llm_mvp_fallback():
    """MVP 阶段 compress_via_llm 应 fallback 到 trunc（不抛错）。"""
    raw = "Z" * 3000
    result = ContextCompressor.compress_via_llm(raw, query="test")
    assert len(result) <= 500
    assert "[已压缩]" in result
