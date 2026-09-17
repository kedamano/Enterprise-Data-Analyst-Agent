"""知识库嵌入失败自愈调度器 — 后台 daemon 线程周期性重试 embed_failed 的 chunk。

设计原则：
* **短路优先**：``embed_retry_interval_s == 0`` 时启动即返回 None（不启线程）。
* **可测试**：``retry_once(store)`` 是纯函数式单次跑批（无状态，便于单测）；
  ``start_background_scheduler`` 只是套了 daemon 线程壳。
* **留痕**：每次 tick 通过 ``metrics`` 埋点（kb_embed_failed_retry_total /
  kb_embed_failed_fixed_total / kb_embed_failed_abandoned_total），不记查询内容。
* **打满一次就关**：调度器从 tick 函数内捕获所有异常——tick 里爆任何错也只是
  该 tick 停、下一 tick 继续，绝不因单次失败让 daemon 线程死掉。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("da.kb.embed_scheduler")

# 全局弱引用，让测试能 kill 运行中的线程
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


def retry_once(store: Any, max_chunks: Optional[int] = None) -> dict[str, int]:
    """单次跑批：尝试修复 store 里的 embed_failed chunk。

    调用 ``store.retry_embed(kb_id=None, limit=max_chunks)``，并记录 Prometheus 指标。
    ``store`` 可以是 ``KnowledgeStore`` 或任何实现了 ``retry_embed()`` 鸭子类型的后端。
    """
    try:
        stats = store.retry_embed(kb_id=None, limit=max_chunks)
    except Exception as exc:  # noqa: BLEW — 调度器绝不因单次失败崩溃
        logger.warning("embed retry tick failed: %s", exc)
        return {"retried": 0, "fixed": 0, "still_failed": 0, "abandoned_now": 0}

    try:
        from ...infrastructure.observability.metrics import metrics
        metrics.inc("kb_embed_failed_retry_total", stats.get("retried", 0))
        metrics.inc("kb_embed_failed_fixed_total", stats.get("fixed", 0))
        metrics.inc("kb_embed_failed_abandoned_total", stats.get("abandoned_now", 0))
    except Exception:
        pass  # 指标埋点失败不影响调度

    return stats


def _tick_loop(store: Any, interval_s: int, stop: threading.Event) -> None:
    """daemon 线程体：stop 被 set 时退出循环。"""
    # 首次进入立刻跑一次（不 interval 秒后再跑）
    retry_once(store)
    while not stop.wait(timeout=interval_s):
        retry_once(store)


def start_background_scheduler(store: Any, interval_s: int) -> Optional[threading.Thread]:
    """启动 daemon 线程周期性调用 ``retry_once``。

    返回线程句柄；``interval_s <= 0`` 时返回 None（调度禁用）。
    重复调用时只启一个——第二调用返回同一线程句柄。
    """
    global _scheduler_thread, _scheduler_stop
    if interval_s <= 0:
        return None
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return _scheduler_thread
    _scheduler_stop.clear()
    t = threading.Thread(
        target=_tick_loop, args=(store, interval_s, _scheduler_stop),
        name="embed-scheduler", daemon=True,
    )
    _scheduler_thread = t
    t.start()
    logger.info("embed scheduler started (interval=%ds)", interval_s)
    return t


def stop_background_scheduler() -> None:
    """通知后台 scheduler 线程在下一 tick 后退出。"""
    global _scheduler_thread
    _scheduler_stop.set()
    t = _scheduler_thread
    if t is not None:
        t.join(timeout=10)
        _scheduler_thread = None
