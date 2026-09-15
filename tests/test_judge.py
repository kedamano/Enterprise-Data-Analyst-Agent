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


def test_judge_case_falls_back_when_llm_unavailable(monkeypatch):
    """即便要求走 LLM（`JUDGE_USE_LLM=1`），**调用失败**也必须降级 rubric 且不崩溃。

    **2026-09-15 修正**：本用例原先**没有模拟任何不可用条件**——它靠的是测试环境里
    `LLM_BASE_URL` 被 conftest 指到错误端点、真实调用必然 401 才"通过"的。
    那是**假绿**：一旦端点被修正，它立刻失败（正是它暴露了 conftest 的端点误指）。
    现在改成显式让调用抛错，不再依赖环境恰好是坏的。
    """
    monkeypatch.setenv("JUDGE_USE_LLM", "1")
    monkeypatch.setenv("LLM_API_KEY", "not-a-real-key")  # 绕过"无 key 直接 rubric"分支
    from app.config import get_settings

    get_settings.cache_clear()

    import app.eval.judge as judge_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("上游不可用")

    monkeypatch.setattr(judge_mod, "judge_with_llm", _boom)

    case = GoldenCase(id="x", query="q", must_find=("营收",))
    res = judge_case(case, "营收分析包含营收数据", "")
    assert res.method == "rubric-offline"
    assert 0.0 <= res.score <= 1.0
    assert "上游不可用" in res.rationale or "降级" in res.rationale, res.rationale


def test_judge_case_skips_llm_without_key(monkeypatch):
    """另一个入口：**没有 key** 时压根不尝试 LLM（`use_llm` 为假），直接 rubric。

    与上一条是两条不同的路径，此前都没被真正测到。
    """
    monkeypatch.setenv("JUDGE_USE_LLM", "1")
    monkeypatch.setenv("LLM_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()

    import app.eval.judge as judge_mod

    def _should_not_be_called(*_a, **_kw):  # pragma: no cover
        raise AssertionError("无 key 时不该调用 LLM judge")

    monkeypatch.setattr(judge_mod, "judge_with_llm", _should_not_be_called)

    case = GoldenCase(id="x", query="q", must_find=("营收",))
    res = judge_case(case, "营收分析包含营收数据", "")
    assert res.method == "rubric-offline"


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
