"""D44：**日志采样**（Gap 六 的"日志无采样"）。

为什么要有它
------------
高并发下每个节点 emit 一条 `[span]` INFO 日志，量级随请求数线性上涨；
日志本身会成为 I/O 瓶颈与成本项。所以需要有采样开关。

**但采样有一条铁律**：
> **只采"正常"，绝不采异常。**

把失败日志采掉等于"故障自愈"——出问题时翻不到任何记录。
这与本项目"降级必须可见"是同一条纪律的另一面。

采样式选择：**确定性"每 N 条记一条"**，不用随机数
------------------------------------------------
随机会让测试变成 flaky（有时采到有时采不到），也会让"为什么这条日志没了"无法复现。
周期性采样可复现、可测、可解释，代价是**可能与小周期信号共振**（如实记录在边界里）。
"""
from __future__ import annotations

from app.config import Settings
from app.infrastructure.observability.sampling import SpanLogSampler


def _sample(ratio: float, n: int, ok: bool = True) -> int:
    s = SpanLogSampler()
    return sum(1 for _ in range(n) if s.should_log(ok=ok, ratio=ratio))


# --------------------------------------------------------------------------- #
# 一、比例
# --------------------------------------------------------------------------- #
def test_full_ratio_logs_everything():
    assert _sample(1.0, 50) == 50


def test_ten_percent_keeps_about_a_tenth():
    assert _sample(0.1, 100) == 10


def test_half_ratio_keeps_half():
    assert _sample(0.5, 100) == 50


def test_ratio_above_one_is_clamped_to_all():
    assert _sample(3.0, 20) == 20


# --------------------------------------------------------------------------- #
# 二、铁律：**失败永不采样**
# --------------------------------------------------------------------------- #
def test_failures_are_never_sampled():
    """这条是本模块存在的意义。采掉失败日志 = 故障自愈。"""
    assert _sample(0.01, 200, ok=False) == 200
    assert _sample(0.001, 50, ok=False) == 50


def test_failures_mixed_with_successes_are_all_kept():
    s = SpanLogSampler()
    kept_fail = kept_ok = 0
    for i in range(100):
        ok = i % 2 == 0
        if s.should_log(ok=ok, ratio=0.1):
            if ok:
                kept_ok += 1
            else:
                kept_fail += 1
    assert kept_fail == 50, "失败必须一条不漏"
    assert kept_ok < 50, "成功应当被采样"


# --------------------------------------------------------------------------- #
# 三、配置边界
# --------------------------------------------------------------------------- #
def test_zero_or_negative_does_not_silence_everything():
    """`0` 不是"全丢弃"而是"不采样"——静音全部日志不是采样，是**失明**。

    配置写坏（0/负数/None）时**退回不采样**，而不是把日志全关掉。
    """
    assert _sample(0.0, 20) == 20
    assert _sample(-1.0, 20) == 20


def test_default_ratio_is_one():
    assert Settings(_env_file=None).log_sample_ratio == 1.0


# --------------------------------------------------------------------------- #
# 四、确定性（可复现、可测）
# --------------------------------------------------------------------------- #
def test_sampling_is_deterministic():
    assert _sample(0.25, 40) == _sample(0.25, 40) == 10


def test_sampler_is_thread_safe_enough():
    """并发下计数不丢（否则实际采样率会偏高）。"""
    import threading

    s = SpanLogSampler()
    hits = []
    lock = threading.Lock()

    def worker():
        local = sum(1 for _ in range(100) if s.should_log(ok=True, ratio=0.1))
        with lock:
            hits.append(local)

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(hits) == 50, f"500 条的成功日志应留 50 条，实际 {sum(hits)}"
