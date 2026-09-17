"""D44：**日志采样** —— 只采"正常"，绝不采异常。

高并发下每个节点 emit 一条 `[span]` INFO 日志，量级随请求数线性上涨；
日志会成为 I/O 瓶颈与成本项，所以需要采样开关。

**但采样有一条铁律**：
> 把失败日志采掉等于"故障自愈"——出问题时翻不到任何记录。

这与本项目"降级必须可见"是同一条纪律的另一面，也是本模块所有设计的出发点。

采样式：**确定性"每 N 条记一条"**，不用随机数
--------------------------------------------
随机会让测试 flaky（有时采到有时采不到），也让"这条日志为什么没了"无法复现。
周期性采样可复现、可测、可解释。
**代价如实记录**：固定周期可能与同样周期的业务信号**共振**（都踩在同一个相位上），
真出现这个问题时应换成"随机起点 + 周期"，而不是退回纯随机。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("da.observability")


class SpanLogSampler:
    """线程安全的周期性采样器（进程内单例即可，见模块末尾）。"""

    def __init__(self) -> None:
        self._seen = 0
        self._lock = threading.Lock()

    @staticmethod
    def _normalize(ratio: float) -> float:
        """**配置写坏时退回"不采样"**，而不是把日志全关掉。

        ``0`` 不是"全丢弃"而是"不采样"——静音全部日志不是采样，是**失明**。
        """
        try:
            value = float(ratio)
        except (TypeError, ValueError):
            return 1.0
        if value <= 0 or value >= 1.0:
            return 1.0
        return value

    def should_log(self, ok: bool, ratio: float) -> bool:
        """``ok=False`` **永远记**；``ok=True`` 按比例记。"""
        if not ok:
            return True                      # 铁律：失败一条不漏
        ratio = self._normalize(ratio)
        if ratio >= 1.0:
            return True
        every = max(1, round(1.0 / ratio))
        with self._lock:
            self._seen += 1
            return self._seen % every == 0


_SAMPLER = SpanLogSampler()


def should_log_span(ok: bool) -> bool:
    """给 tracing 用的薄封装（读配置）。任何异常都退回"记"。"""
    try:
        from ...config import get_settings

        return _SAMPLER.should_log(ok=ok, ratio=get_settings().log_sample_ratio)
    except Exception:
        return True
