"""E4/03 v1.2：`unit_mismatch` / `filter_mismatch` 的确定性判定。

Spec: docs/specs/E4/03-caliber-comparability.md §7

这两个枚举值此前**只有名字没有判定**（规格原文："枚举值保留但未实现"），
本轮把可结构化判定的子集落地。设计原则与 `gate.py` 一致——**双条件 + 宁缺勿滥**：

- `unit_mismatch`：只判"**同一指标**出现不同数量级单位"。不同指标用不同量级单位是正常写法，
  全文见两种单位就报会把正常报告全部点亮。
- `filter_mismatch`：只判"同一限定对象出现**相反极性**"。更广的"单侧过滤"结构性判据不足，
  硬做必然误报 → 明确不判，留给 LLM 维度（仍标 `[待真实验证]`）。
"""
from __future__ import annotations

from app.core.agents.data_analyst.caliber import (
    apply_caliber,
    caliber_check,
)
from app.core.agents.data_analyst.state import AnalysisResult, ReflectionDecision, ReflectionResult


def _kinds(report: str) -> list[str]:
    return [i.kind for i in caliber_check(AnalysisResult(), None, report=report).issues]


# --------------------------------------------------------------------------- #
# unit_mismatch
# --------------------------------------------------------------------------- #
def test_same_metric_in_different_magnitudes_is_flagged():
    """同一指标（营收）一会儿亿元、一会儿万元 → 数量级混用。"""
    kinds = _kinds("营收 1.2 亿元，上年同期营收 1500 万元，同比增长 7 倍")
    assert "unit_mismatch" in kinds


def test_different_metrics_in_different_magnitudes_not_flagged():
    """不同指标用不同量级单位是**正常写法**，不得误报。"""
    kinds = _kinds("营收 1.2 亿元，成本 3000 万元，毛利率 25%")
    assert "unit_mismatch" not in kinds


def test_same_metric_same_unit_not_flagged():
    kinds = _kinds("营收 1.2 亿元，上年同期营收 1.1 亿元，同比增长 9%")
    assert "unit_mismatch" not in kinds


def test_single_amount_not_flagged():
    kinds = _kinds("营收 1.2 亿元")
    assert "unit_mismatch" not in kinds


# --------------------------------------------------------------------------- #
# filter_mismatch
# --------------------------------------------------------------------------- #
def test_opposite_polarity_on_same_qualifier_is_flagged():
    """同一限定对象（退款）同时出现"含"与"不含" → 口径冲突。"""
    kinds = _kinds("含退款口径的营收 1.2 亿，而不含退款的营收 1.1 亿")
    assert "filter_mismatch" in kinds


def test_opposite_polarity_on_tax_is_flagged():
    kinds = _kinds("含税营收 1.2 亿元，不含税营收 1.05 亿元")
    assert "filter_mismatch" in kinds


def test_one_sided_qualifier_not_flagged():
    """**明确的边界**：只有一处限定词（单侧过滤）不判——结构性判据不足，留给 LLM 维度。"""
    kinds = _kinds("不含退款后营收 1.1 亿元，环比增长 3%")
    assert "filter_mismatch" not in kinds


def test_single_polarity_repeated_not_flagged():
    kinds = _kinds("不含退款营收 1.1 亿元，不含退款订单 3 万单")
    assert "filter_mismatch" not in kinds


# --------------------------------------------------------------------------- #
# 决策影响：两条新规则都是**披露级**，不得收紧决策
# --------------------------------------------------------------------------- #
def test_new_kinds_do_not_tighten_decision():
    """与 period_mismatch 同级：提示口径，但不推翻结论（唯一抬 REPLAN 的仍是 iteration_drift）。"""
    check = caliber_check(AnalysisResult(), None,
                          report="含退款营收 1.2 亿元，不含退款营收 1.1 亿元；"
                                 "该指标 1.2 亿，上年 1500 万")
    assert check.issues, "本用例前置：应至少命中一条口径问题"
    assert check.comparable is False
    base = ReflectionResult(decision=ReflectionDecision.PASS)
    assert apply_caliber(check, base) == ReflectionDecision.PASS
