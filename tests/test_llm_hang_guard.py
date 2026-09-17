"""D40：LLM 调用**挂住检测**——`timeout=` 兜不住的那种挂起。

现场（D39 实跑，2026-09-14）
--------------------------
进程在 analyst 的 LLM 调用上卡死：日志停在 `23:56`，到 `03:07` 仍无输出，
**进程存活但 CPU=0s**（在等 I/O）。而配置是 `LLM_TIMEOUT_S=300` + 5 次重试，
最坏也该 ~25 分钟结束 —— **超时没兜住**，且**没有任何告警**。

为什么 SDK 的 `timeout=` 不够
-----------------------------
httpx 的 **read timeout 会在每次收到数据块时重置**。上游只要周期性吐
keep-alive 字节（网关/反代很常见），read timeout 就**永不触发**。
传进去的 300s 一直"在计时"，却永远等不到那一刻。

修法：加一层**与传输无关的墙钟**——daemon 线程执行 + `join(deadline)`。
超时抛 `LLMDeadlineExceeded`，且**不可重试**（连着挂 5 次 × deadline 只会更糟）。

设计取舍（必须写明）
--------------------
超时后工作线程**仍在后台跑**（Python 无法强杀线程）。这是**有意为之**：
宁可漏一个线程 + 一条半开连接，也不能让整条流水线被永久占住。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.config import Settings, get_settings
from app.infrastructure.llm.router import (
    LLMDeadlineExceeded,
    OpenAILLM,
    _call_with_deadline,
    _non_retryable,
)


# --------------------------------------------------------------------------- #
# 一、墙钟兜底本身
# --------------------------------------------------------------------------- #
def test_returns_value_when_within_deadline():
    assert _call_with_deadline(lambda: "ok", deadline_s=5.0) == "ok"


def test_raises_when_call_hangs():
    """**确定性**：用一个永不 set 的 Event 模拟挂死，不靠 sleep 竞态。"""
    gate = threading.Event()
    t0 = time.time()
    with pytest.raises(LLMDeadlineExceeded):
        _call_with_deadline(lambda: gate.wait(), deadline_s=0.2)
    assert time.time() - t0 < 3.0, "必须在 deadline 附近就返回，不能跟着一起挂"


def test_hang_guard_is_traceable(caplog):
    """挂起是**故障**，必须留痕（铁律 3：不许静默）。"""
    import logging

    gate = threading.Event()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMDeadlineExceeded):
            _call_with_deadline(lambda: gate.wait(), deadline_s=0.2)
        gate.set()
    assert any("墙钟" in r.message or "挂" in r.message for r in caplog.records), \
        [r.message for r in caplog.records]


def test_deadline_error_is_not_retried():
    """**不可重试**：重试一个已挂起的调用，只会把 deadline 乘上重试次数。"""
    assert _non_retryable(LLMDeadlineExceeded("hang"))


# --------------------------------------------------------------------------- #
# 二、配置
# --------------------------------------------------------------------------- #
def test_default_deadline_is_larger_than_sdk_timeout():
    s = Settings(_env_file=None)
    assert s.llm_hard_deadline_s > 0
    assert s.llm_hard_deadline_s > s.llm_timeout_s, (
        "墙钟必须**大于** SDK timeout —— 否则会误杀正常但慢的调用")


def test_deadline_can_be_disabled(monkeypatch):
    """设为 0 = 关闭墙钟（保留 SDK timeout 的旧行为，便于排障）。"""
    monkeypatch.setenv("LLM_HARD_DEADLINE_S", "0")
    get_settings.cache_clear()
    try:
        assert get_settings().llm_hard_deadline_s == 0
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 三、端到端：挂在 SDK 调用上也能被兜住
# --------------------------------------------------------------------------- #
class _HangingCreate:
    def __init__(self, gate: threading.Event):
        self._gate = gate

    def create(self, **kwargs):
        self._gate.wait()          # 模拟"连接半开、只吐心跳"
        raise AssertionError("不该走到这里")


class _HangingClient:
    def __init__(self, gate: threading.Event):
        self.chat = type("C", (), {"completions": _HangingCreate(gate)})()


def _bare_settings(**kw) -> Settings:
    """构造不读 .env 的 Settings；给个假 key 让 OpenAI 客户端能构造（随后会被替换）。"""
    s = Settings(_env_file=None, llm_api_key="test-key", **kw)
    return s


def test_call_model_is_bounded_by_wall_clock(monkeypatch):
    gate = threading.Event()
    settings = _bare_settings()
    settings.llm_hard_deadline_s = 0.3
    settings.llm_max_retries = 5          # 即便配了 5 次重试也不该真的等 5 轮
    llm = OpenAILLM(settings)
    llm._client = _HangingClient(gate)

    t0 = time.time()
    with pytest.raises(Exception) as exc_info:
        llm._call_model(model="m", system="s", content="c", stage="analyst",
                        json_mode=True, temperature=0.0)
    elapsed = time.time() - t0
    gate.set()                             # 放掉后台线程，别留给下一个用例
    assert isinstance(exc_info.value, LLMDeadlineExceeded), exc_info.value
    assert elapsed < 5.0, f"应被墙钟兜住，实际等了 {elapsed:.1f}s"


def test_normal_call_unaffected(monkeypatch):
    """不许误伤：正常返回的调用照常返回。"""
    class _Ok:
        def create(self, **kwargs):
            msg = type("M", (), {"content": '{"findings": []}'})()
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 2})()
            return type("R", (), {"choices": [type("Ch", (), {"message": msg})()],
                                  "usage": usage})()

    llm = OpenAILLM(_bare_settings())
    llm._client = type("Cl", (), {"chat": type("C", (), {"completions": _Ok()})()})()
    out = llm._call_model(model="m", system="s", content="c", stage="analyst",
                          json_mode=True, temperature=0.0)
    assert out == '{"findings": []}'
