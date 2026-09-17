"""D43：**单会话 token 熔断** —— 超预算就拒绝继续烧。

为什么需要它
------------
单次运行的真实成本**没有硬上限**：REPLAN 循环、`_llm_model` 的失败重试、多模型链回落，
任一环节打滑都会让同一轮不断发请求。实测一次 `--only-real` 全量约 91 万 tokens（正常），
但**没有任何东西阻止它变成 9000 万**。

熔断点选在 `router._call_model`：**全部 LLM 调用（含 judge）的唯一咽喉**。
语义是"**拒绝下一次**"而不是"事后报警"——所以检查在**调用之前**。

边界（如实写明，不含糊）
------------------------
key 取 `tracing.current_run_id()`。API 路径下一次请求 = 一个 run，故本熔断是
**按 run（一次 Agent 执行）**，不是跨多轮对话的会话级配额——后者需要把 session_id
透传进 router（要动全部调用点），本日不做。
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

from ..observability.tracing import current_run_id

logger = logging.getLogger("da.llm")


class SessionTokenBudgetExceeded(Exception):
    """本次运行累计 token 已超预算 → **拒绝发起新的 LLM 调用**。

    不可重试（见 `router._non_retryable`）：重试不会让预算变多，只会把墙钟拖长。
    """


class SessionTokenBudget:
    """按 key（run）累计 token，线程安全。

    与 `tools/specs.RateLimiter` 同一范式（进程内 + 锁），不引外部依赖：
    熔断必须**足够快**，不能自己成为故障点。
    """

    def __init__(self) -> None:
        self._used: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def add(self, key: str, tokens: int) -> int:
        """累加并返回累计值（<=0 的增量直接忽略）。"""
        if not tokens or tokens < 0:
            return self.used(key)
        with self._lock:
            self._used[key] += int(tokens)
            return self._used[key]

    def used(self, key: str) -> int:
        with self._lock:
            return self._used.get(key, 0)

    def exceeded(self, key: str, budget: int) -> bool:
        """**预算 <= 0 表示关闭**（向后兼容：不配就不改变既有行为）。"""
        if not budget or budget <= 0:
            return False
        return self.used(key) >= budget

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._used.clear()
            else:
                self._used.pop(key, None)


_BUDGET = SessionTokenBudget()
_FALLBACK_KEY = "_no_run_context"


def _current_key() -> str:
    """当前 run 作为熔断 key；无 trace 上下文（脚本/测试）时退化为进程级。"""
    return current_run_id() or _FALLBACK_KEY


def used_tokens() -> int:
    return _BUDGET.used(_current_key())


def check_and_reserve(budget: int) -> None:
    """发起调用**之前**检查；超预算即抛（拒绝下一次，而不是事后报警）。"""
    key = _current_key()
    if _BUDGET.exceeded(key, budget):
        logger.error("会话 token 熔断：run=%s 已用 %d tokens ≥ 预算 %d，拒绝继续调用",
                     key, _BUDGET.used(key), budget)
        raise SessionTokenBudgetExceeded(
            f"session token budget exceeded: {_BUDGET.used(key)} >= {budget}")


def record(tokens: int) -> int:
    """调用**之后**记账。"""
    return _BUDGET.add(_current_key(), tokens)


def reset_all() -> None:
    """测试/排障用：清空全部计数。"""
    _BUDGET.reset()
