"""LLM-judge 评测测试（#5）。

验证：离线 rubric 可微分（关键片段命中 → 高分、缺失 → 低分）、局限声明缺失被扣分、
`judge_case` 在 LLM 不可用时不崩溃（降级 rubric）、runner 能采集 judge 指标。
"""
from __future__ import annotations

import os

from app.eval.golden import GoldenCase
from app.eval.judge import judge_case, judge_offline_rubric


def test_offline_rubric_rewards_must_find_coverage():
    case = GoldenCase(id="x", query="q", must_find=("营收", "区域"))
    good = judge_offline_rubric(case, "各区域营收表现如下", "")
    bad = judge_offline_rubric(case, "报告里完全没有要求的关键词", "")
    assert good.score > bad.score
    assert good.score >= 0.5  # 两个关键片段都命中 → 该项满分 0.5 + 其余


def test_offline_rubric_penalizes_missing_limitations():
    case = GoldenCase(id="x", query="q", must_have_limitations=True)
    with_limit = judge_offline_rubric(case, "结论成立，但需注意口径局限与样本量", "")
    without = judge_offline_rubric(case, "结论成立", "")
    assert with_limit.score > without.score


def test_offline_rubric_refusal_discipline():
    case = GoldenCase(id="x", query="q", expect_refusal=True)
    with_refusal = judge_offline_rubric(case, "数据不足，无法确保该结论可靠", "")
    without = judge_offline_rubric(case, "结论完全成立", "")
    assert with_refusal.score > without.score


def test_judge_case_offline_method_default():
    case = GoldenCase(id="x", query="q", must_find=("A",))
    # 默认（无 JUDGE_USE_LLM）→ rubric-offline，且不抛异常
    res = judge_case(case, "包含 A 的分析", "")
    assert res.method == "rubric-offline"
    assert 0.0 <= res.score <= 1.0


def test_judge_case_falls_back_when_llm_unavailable():
    # 即便要求走 LLM（JUDGE_USE_LLM=1），无 key / openai 缺失也应降级到 rubric，绝不崩溃
    old = os.environ.get("JUDGE_USE_LLM")
    os.environ["JUDGE_USE_LLM"] = "1"
    try:
        case = GoldenCase(id="x", query="q", must_find=("营收",))
        res = judge_case(case, "营收分析包含营收数据", "")
        assert res.method == "rubric-offline"
        assert 0.0 <= res.score <= 1.0
    finally:
        if old is None:
            os.environ.pop("JUDGE_USE_LLM", None)
        else:
            os.environ["JUDGE_USE_LLM"] = old


def test_runner_collects_judge_metric():
    """runner 能跑通并采集 judge 指标（mock 模式，离线 rubric）。"""
    from app.eval.runner import evaluate

    os.environ["MOCK_LLM"] = "true"
    report = evaluate(mode="mock", ids=["a_normal_query_no_adversarial"])
    detail = report["cases_detail"][0]
    assert detail["judge_score"] is not None
    assert detail["judge_method"] == "rubric-offline"
    assert detail["judge_score"] >= 0.5  # 该用例设了 judge_min_score=0.5，必须达标
    assert report["metrics"]["judge_avg_score"] is not None
