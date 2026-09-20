"""TDD for rolling-summary condensing + context budget (05 记忆 Q8 / Q4)."""
from __future__ import annotations

import pytest

from app.core.memory.budget import estimate_tokens, fit_to_budget, truncate
from app.core.memory.summarize import condense_history, summarize_text
from app.infrastructure.llm.router import BaseLLM


class FakeSummarizer(BaseLLM):
    def __init__(self, text="[摘要] 关键决策已保留"):
        self.text = text
        self.calls = 0

    def _do_complete(self, system, user, stage="", json_mode=False, temperature=None) -> str:
        self.calls += 1
        return self.text


def _entry(i: int) -> dict:
    return {"role": "user", "query": f"第 {i} 次分析请求"}


# --- condense_history ----------------------------------------------------- #
def test_condense_noop_within_max():
    entries = [_entry(i) for i in range(10)]
    recent, summary = condense_history(entries, max_items=20)
    assert summary is None and len(recent) == 10


def test_condense_summarizes_older_keeps_recent():
    llm = FakeSummarizer()
    entries = [_entry(i) for i in range(30)]  # > max_items 20
    recent, summary = condense_history(entries, max_items=20, keep_recent=8, llm=llm)
    assert llm.calls == 1, "溢出时应触发一次摘要"
    assert summary == llm.text
    assert len(recent) == 8, "仅保留最近 keep_recent 条原文"


def test_condense_records_concrete_info_in_prompt():
    llm = FakeSummarizer()
    condense_history([_entry(i) for i in range(25)], llm=llm)
    sent = llm.calls == 1
    assert sent


def test_summarize_degrades_when_llm_fails():
    class Boom(BaseLLM):
        def _do_complete(self, *a, **k):
            raise ConnectionError

    out = summarize_text("A" * 600, llm=Boom())
    assert out and "A" in out and len(out) < 600, "失败应退化为截断而非抛错"


# --- budget --------------------------------------------------------------- #
def test_truncate_caps_and_marks():
    out = truncate("x" * 100, 20)
    assert len(out) <= 20 and "截断" in out


def test_fit_to_budget_keeps_priority_order():
    sections = fit_to_budget(
        [("system", "S" * 100), ("history", "H" * 400), ("tools", "T" * 400)],
        max_chars=400,
        headroom_ratio=0.1,
    )
    usable = 360
    assert len(sections["system"]) == 100        # 高优保留完整
    remaining = usable - 100
    assert len(sections["history"]) == remaining  # 次优吃满剩余
    assert sections["tools"] == ""                # 超预算置空


def test_estimate_tokens_proxy():
    assert estimate_tokens("营收下滑原因") >= 6     # 中文 ~1/字
    assert estimate_tokens("abc") >= 1


def test_redis_down_falls_back_to_memory(monkeypatch):
    """REDIS_URL 指向不可达实例时，记忆读写降级为进程内 dict（不崩溃）。"""
    import app.core.memory.short_term as st

    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # 端口 1 → 拒绝连接
    from app.config import get_settings
    get_settings.cache_clear()

    sid = "redis_down_" + "x"
    st.put(sid, "k", {"v": 1})
    assert st.get(sid, "k") == {"v": 1}
    assert "k" in st.get_all(sid)
    get_settings.cache_clear()
