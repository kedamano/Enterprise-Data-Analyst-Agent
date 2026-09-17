"""D43：**单会话 token 熔断** —— 超预算就拒绝继续烧，而不是无限跑。

缺口（Gap 三）
-------------
> `prompt 组装未真正按预算强制执行`；`无**单会话 token 熔断**`。
> D42 已把前者补上（`prompts/budget.py`），本日补后者。

为什么需要它
------------
单次运行的真实成本**没有硬上限**：REPLAN 循环、`_llm_model` 的失败重试、
多模型链回落，任一环节打滑都会让同一轮不断发请求。实测一次 `--only-real` 全量
约 91 万 tokens——**正常**，但没有任何东西阻止它变成 9000 万。

熔断点选在 `router._call_model`：**全部 LLM 调用（含 judge）的唯一咽喉**。
语义是"**拒绝下一次**"，不是"事后报警"——所以检查在**调用之前**。

边界（如实写明）
----------------
key 取 `tracing.current_run_id()`。API 路径下一次请求 = 一个 run，
故本熔断是**按 run**（一次 Agent 执行）而非跨多轮对话的会话。
跨轮会话级配额需要把 session_id 透传进 router（要动所有调用点），
**本日不做**，留明确边界而不是含糊其辞。
"""
from __future__ import annotations

import threading

import pytest

from app.config import Settings, get_settings
from app.infrastructure.llm.router import (
    SessionTokenBudgetExceeded,
    _non_retryable,
)
from app.infrastructure.llm.token_budget import SessionTokenBudget


# --------------------------------------------------------------------------- #
# 一、累计与熔断（纯逻辑）
# --------------------------------------------------------------------------- #
def test_accumulates_per_key():
    b = SessionTokenBudget()
    b.add("run-a", 100)
    b.add("run-a", 50)
    b.add("run-b", 7)
    assert b.used("run-a") == 150
    assert b.used("run-b") == 7
    assert b.used("never-seen") == 0


def test_does_not_breaker_before_budget():
    b = SessionTokenBudget()
    b.add("r", 900)
    assert b.exceeded("r", budget=1000) is False


def test_breakers_at_or_over_budget():
    b = SessionTokenBudget()
    b.add("r", 1000)
    assert b.exceeded("r", budget=1000) is True
    b.add("r", 1)
    assert b.exceeded("r", budget=1000) is True


def test_zero_budget_never_breakers():
    """0 = 关闭（向后兼容）——不配就不改变既有行为。"""
    b = SessionTokenBudget()
    b.add("r", 10 ** 9)
    assert b.exceeded("r", budget=0) is False


def test_keys_are_isolated():
    """一个 run 烧穿不该连累另一个 run。"""
    b = SessionTokenBudget()
    b.add("run-a", 10 ** 6)
    assert b.exceeded("run-a", budget=1000) is True
    assert b.exceeded("run-b", budget=1000) is False


def test_reset_clears_one_or_all():
    b = SessionTokenBudget()
    b.add("a", 5)
    b.add("b", 5)
    b.reset("a")
    assert b.used("a") == 0 and b.used("b") == 5
    b.reset()
    assert b.used("b") == 0


def test_thread_safe_accumulation():
    """并发调用点（并行执行器/多线程 worker）下计数不能丢。"""
    b = SessionTokenBudget()

    def worker():
        for _ in range(200):
            b.add("r", 1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert b.used("r") == 1600


# --------------------------------------------------------------------------- #
# 二、异常语义
# --------------------------------------------------------------------------- #
def test_budget_error_is_not_retried():
    """**不可重试**：重试不会让预算变多，只会把墙钟拖长。"""
    assert _non_retryable(SessionTokenBudgetExceeded("over"))


# --------------------------------------------------------------------------- #
# 三、配置
# --------------------------------------------------------------------------- #
def test_default_budget_is_generous_but_bounded():
    """默认值必须**大于**一次正常全量（实测 ≈91 万 tokens）——
    否则正常跑会被自己的熔断打断；同时必须有限，否则等于没配。"""
    s = Settings(_env_file=None)
    assert s.session_token_budget == 0 or 500_000 <= s.session_token_budget <= 10_000_000


def test_budget_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SESSION_TOKEN_BUDGET", "0")
    get_settings.cache_clear()
    try:
        assert get_settings().session_token_budget == 0
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 四、端到端：咽喉处真的会拒绝
# --------------------------------------------------------------------------- #
class _Ok:
    def __init__(self, tokens: int = 10):
        self._t = tokens
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        msg = type("M", (), {"content": '{"ok": 1}'})()
        usage = type("U", (), {"prompt_tokens": self._t, "completion_tokens": 0})()
        return type("R", (), {"choices": [type("Ch", (), {"message": msg})()],
                              "usage": usage})()


def _llm(tokens: int = 10, budget: int = 0):
    from app.infrastructure.llm.router import OpenAILLM

    # 注意：这里用**新建实例**而非 get_settings()，所以预算必须设在**它自己**身上
    # （monkeypatch get_settings() 影响不到这条路径——第一版测试就栽在这）
    s = Settings(_env_file=None, llm_api_key="test-key")
    s.session_token_budget = budget
    s.llm_hard_deadline_s = 5.0
    llm = OpenAILLM(s)
    client = _Ok(tokens)
    llm._client = type("Cl", (), {"chat": type("C", (), {"completions": client})()})()
    return llm, client


def test_call_model_refuses_after_budget(monkeypatch):
    from app.infrastructure.llm.router import token_budget as tb

    tb.reset_all()
    llm, client = _llm(tokens=10, budget=30)   # 只够 3 次
    monkeypatch.setattr(tb, "_current_key", lambda: "test-run")

    for _ in range(3):
        llm._call_model(model="m", system="s", content="c", stage="analyst",
                        json_mode=True, temperature=0.0)
    with pytest.raises(SessionTokenBudgetExceeded):
        llm._call_model(model="m", system="s", content="c", stage="analyst",
                        json_mode=True, temperature=0.0)
    assert client.calls == 3, "熔断之后不得再发请求"
    tb.reset_all()


def test_call_model_unaffected_when_budget_disabled(monkeypatch):
    """0 = 关闭：既有行为不变（默认值不能打断正常跑）。"""
    from app.infrastructure.llm.router import token_budget as tb

    tb.reset_all()
    llm, client = _llm(tokens=10, budget=0)
    monkeypatch.setattr(tb, "_current_key", lambda: "test-run")
    for _ in range(5):
        llm._call_model(model="m", system="s", content="c", stage="analyst",
                        json_mode=True, temperature=0.0)
    assert client.calls == 5
    tb.reset_all()
