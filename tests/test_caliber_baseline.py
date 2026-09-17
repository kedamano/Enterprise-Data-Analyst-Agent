"""E4/05 同环比基线自动判定 + caliber_check 联动 — D48。

Spec: docs/specs/E4/05-caliber-registry.md §2, §4, §5

确定性日期算术：环比→上一段等长期间；同比→去年同月/同季。
解析不出 → None（宁缺勿滥，不猜基线）。
两个新 issue kind 均为**披露级**：``baseline_mismatch`` / ``caliber_deviation``。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.core.agents.data_analyst.caliber import (
    caliber_check,
    infer_baseline_period,
)
from app.core.agents.data_analyst.state import (
    AnalysisResult,
    Comparison,
    ContextModel,
    Finding,
    TimeRange,
)
from app.core.agents.data_analyst.caliber_registry import CaliberSpec


def _ctx(start=None, end=None, ctype=None, cperiod=None) -> ContextModel:
    return ContextModel(
        time_range=TimeRange(start=start, end=end),
        comparison=Comparison(type=ctype, period=cperiod),
    )


def _finding(text: str) -> Finding:
    return Finding(finding=text)


# --------------------------------------------------------------------------- #
# infer_baseline_period — 确定性日期算术
# --------------------------------------------------------------------------- #
def test_mom_baseline_is_previous_equal_length_period():
    """环比：2024-03-01~2024-03-31 → 2024-02-01~2024-02-29。"""
    bs, be, label, days = infer_baseline_period("2024-03-01", "2024-03-31", "环比")
    assert bs == date(2024, 2, 1)
    assert be == date(2024, 2, 29)
    assert days == 29


def test_yoy_baseline_is_same_period_last_year():
    """同比：2024-03-01~2024-03-31 → 2023-03-01~2023-03-31。"""
    bs, be, label, days = infer_baseline_period("2024-03-01", "2024-03-31", "同比")
    assert bs == date(2023, 3, 1)
    assert be == date(2023, 3, 31)


def test_mom_uses_end_minus_start_width():
    """环比按 (end-start) 天数平移，不强求对齐自然月。"""
    bs, be, label, days = infer_baseline_period("2024-01-15", "2024-03-15", "环比")
    # 60 天宽度 → 上一段 2023-11-16 ~ 2024-01-14
    assert (be - bs).days == 60
    assert be == date(2024, 1, 14)


def test_returns_none_when_dates_missing():
    """start/end 缺失 → None（不猜）。"""
    assert infer_baseline_period(None, None, "环比") is None
    assert infer_baseline_period("2024-03-01", "", "环比") is None


def test_returns_none_when_type_missing():
    """comparison_type 空或非环比/同比 → None。"""
    assert infer_baseline_period("2024-03-01", "2024-03-31", "") is None
    assert infer_baseline_period("2024-03-01", "2024-03-31", None) is None


def test_returns_none_when_dates_unparseable():
    """非日期文本 → None。"""
    assert infer_baseline_period("上月", "本月", "环比") is None
    assert infer_baseline_period("not a date", "2024-03-31", "环比") is None


def test_accepts_slash_separated_dates():
    """YYYY/MM/DD 也接受。"""
    bs, be, label, days = infer_baseline_period("2024/03/01", "2024/03/31", "环比")
    assert bs == date(2024, 2, 1)


def test_yoy_aliases():
    """YoY / 去年 同义。"""
    bs, _, _, _ = infer_baseline_period("2024-03-01", "2024-03-31", "YoY")
    assert bs == date(2023, 3, 1)
    bs2, _, _, _ = infer_baseline_period("2024-03-01", "2024-03-31", "去年")
    assert bs2 == date(2023, 3, 1)


# --------------------------------------------------------------------------- #
# caliber_check 联动：baseline_mismatch
# --------------------------------------------------------------------------- #
def test_baseline_mismatch_when_period_missing():
    """声明环比但 comparison.period 空 → baseline_mismatch。"""
    ctx = _ctx("2024-03-01", "2024-03-31", ctype="环比", cperiod="")
    check = caliber_check(AnalysisResult(), ctx, report="营收环比增长 10%")
    assert "baseline_mismatch" in [i.kind for i in check.issues]


def test_baseline_mismatch_when_period_far_off():
    """环比 + period 与推断基线天数差 >20% → baseline_mismatch。"""
    # 推断基线约 29 天；给个 7 天的"上周"
    ctx = _ctx("2024-03-01", "2024-03-31", ctype="环比", cperiod="上周")
    check = caliber_check(AnalysisResult(), ctx, report="营收环比增长 10%")
    assert "baseline_mismatch" in [i.kind for i in check.issues]


def test_no_baseline_mismatch_when_period_aligned():
    """环比 + period 与推断基线等长（差 ≤20%）→ 不报 baseline_mismatch。"""
    # 上一段约 29 天；"上月" parse 为 30 天，差 1 天 << 20%
    ctx = _ctx("2024-03-01", "2024-03-31", ctype="环比", cperiod="上月")
    check = caliber_check(AnalysisResult(), ctx, report="营收环比增长 10%")
    assert "baseline_mismatch" not in [i.kind for i in check.issues]


def test_no_baseline_check_when_no_comparison_type():
    """未声明对比类型 → 不判 baseline。"""
    ctx = _ctx("2024-03-01", "2024-03-31", ctype=None)
    check = caliber_check(AnalysisResult(), ctx, report="营收 1.2 亿元")
    assert "baseline_mismatch" not in [i.kind for i in check.issues]


# --------------------------------------------------------------------------- #
# caliber_check 联动：caliber_deviation（依赖注册表）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def registered(monkeypatch, tmp_path):
    """登记一条「营收」基准口径：万元 / 不含退货。"""
    from app.core.agents.data_analyst import caliber_registry as mod

    reg = mod.CaliberRegistry(path=tmp_path / "caliber_registry.json")
    reg.register(CaliberSpec(metric="营收", filters=["不含退货"],
                             unit="万元", period_type="月"))
    monkeypatch.setattr(mod, "_default_registry", lambda: reg)
    return reg


def test_caliber_deviation_on_unit_conflict(registered):
    """报告用亿元、登记万元 → caliber_deviation。"""
    ctx = _ctx("2024-03-01", "2024-03-31")
    check = caliber_check(AnalysisResult(), ctx, report="营收 1.2 亿元")
    assert "caliber_deviation" in [i.kind for i in check.issues]


def test_caliber_deviation_on_filter_polarity_conflict(registered):
    """报告含退货、登记不含退货 → caliber_deviation。"""
    ctx = _ctx("2024-03-01", "2024-03-31")
    check = caliber_check(AnalysisResult(), ctx, report="含退货口径的营收 1.2 万元")
    assert "caliber_deviation" in [i.kind for i in check.issues]


def test_no_deviation_when_report_matches_registry(registered):
    """报告口径与登记一致 → 不报 caliber_deviation。"""
    ctx = _ctx("2024-03-01", "2024-03-31")
    check = caliber_check(AnalysisResult(), ctx, report="营收 1200 万元，不含退货")
    assert "caliber_deviation" not in [i.kind for i in check.issues]


def test_no_deviation_for_unregistered_metric(registered):
    """未登记的指标 → 不判 caliber_deviation。"""
    ctx = _ctx("2024-03-01", "2024-03-31")
    check = caliber_check(AnalysisResult(), ctx, report="毛利率 25%，含退货")
    assert "caliber_deviation" not in [i.kind for i in check.issues]


# --------------------------------------------------------------------------- #
# 畸形输入不抛
# --------------------------------------------------------------------------- #
def test_malformed_input_does_not_raise():
    assert caliber_check(None, None).comparable in (True, False)
    assert caliber_check(AnalysisResult(), None, report="").comparable in (True, False)
