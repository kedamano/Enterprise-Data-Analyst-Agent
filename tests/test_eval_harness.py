"""TDD for the eval harness: metrics are real and the runner is CI-safe offline.

Contract:
* ``evaluate(mode="mock")`` runs every golden case to FINISH via MockLLM and
  aggregates quality metrics without any API key / network.
* Tool & LLM call counts per case are read from the persisted structured trace
  (real numbers, not guesses); tool_calls >= 1 and llm_calls >= 1 per finished case.
* Assertions honour must_find and expect_finish; the report carries a metrics
  dict and a markdown render.
"""
from __future__ import annotations

import pytest

from app.eval.golden import GOLDEN
from app.eval.runner import evaluate, render_markdown


def test_mock_eval_runs_all_cases_to_finish(tmp_path):
    report = evaluate(mode="mock", trace_dir=tmp_path)
    m = report["metrics"]
    assert report["cases"] == len(GOLDEN)
    assert m["finish_rate"] == 1.0, f"mock 全流水线应全部 FINISH: {report['cases_detail']}"
    assert 0 <= m["pass_rate"] <= 1
    assert m["tool_calls_total"] >= report["cases"]
    assert m["llm_calls_total"] >= report["cases"]


def test_per_case_counts_come_from_real_trace(tmp_path):
    report = evaluate(mode="mock", trace_dir=tmp_path)
    cases = report["cases_detail"]
    # requires_real 的用例在 mock 下合法 SKIPPED（无 key 不得声称已达标）——
    # 但**只有**它们可以跳过，否则"跳过"会变成刷通过率的后门（铁律 6）。
    real_gated = {c.id for c in GOLDEN if c.requires_real}
    skipped = {c["case_id"] for c in cases if c["status"] == "SKIPPED"}
    assert skipped <= real_gated, f"非 requires_real 用例不得被跳过: {skipped - real_gated}"

    for case in cases:
        if case["status"] == "SKIPPED":
            continue
        assert case["status"] == "FINISH", case
        assert case["tool_calls"] >= 1, f"{case['case_id']} 至少执行一次工具"
        assert case["llm_calls"] >= 1, f"{case['case_id']} 至少驱动一次 LLM 阶段"
        # 轻模式（sql_only/quick_answer）合法地不跑 Reflection → None 可接受
        assert case["reflect_decision"] in ("PASS", "REPLAN", "FAIL", None)
        assert case["executed_tools"], f"{case['case_id']} 应记录实际工具集合"
        assert case["tool_success"] >= 1, f"{case['case_id']} 应有成功工具调用"


def test_tool_trajectory_and_ban_assertions(tmp_path):
    """expected_tools 缺失 / must_not_appear 出现都会让该用例失败。"""
    from app.eval.runner import evaluate_case
    from app.eval.golden import GoldenCase

    neg = GoldenCase(id="neg_tools", query="分析最近营收变化的原因",
                     expected_tools=("绝不存在的工具zzz",))
    out = evaluate_case(neg, "mock", "eval_neg_tools", tmp_path)
    assert out.assertions_ok is False
    assert any("期望调用工具" in a for a in out.failed_assertions)

    neg2 = GoldenCase(id="neg_ban", query="分析最近营收变化的原因",
                      must_not_appear=("SELECT 1",))
    out2 = evaluate_case(neg2, "mock", "eval_neg_ban", tmp_path)
    assert out2.assertions_ok is True, out2.failed_assertions  # schema 回退后不应出现退化 SQL


def test_assertion_fails_when_required_token_absent(tmp_path):
    """断言机制本身可被触发（构造必失败用例验证逻辑，而非永远绿灯）。"""
    from app.eval.runner import evaluate_case
    from app.eval.golden import GoldenCase

    case = GoldenCase(id="neg", query="分析最近营收变化的原因",
                      must_find=("绝不可能出现的中文字串xyz",))
    out = evaluate_case(case, "mock", "eval_neg_check", tmp_path)
    assert out.assertions_ok is False
    assert any("未命中" in a for a in out.failed_assertions)


def test_render_markdown_has_sections(tmp_path):
    report = evaluate(mode="mock", trace_dir=tmp_path)
    md = render_markdown(report)
    assert "# Eval 报告" in md
    assert "## 指标" in md
    assert "## 用例明细" in md
    assert "FINISH 率" in md


def test_real_mode_requires_key(tmp_path):
    from app.config import get_settings

    orig = get_settings().llm_api_key
    try:
        import os
        os.environ["LLM_API_KEY"] = ""
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            evaluate(mode="real")
    finally:
        if orig:
            import os
            os.environ["LLM_API_KEY"] = orig
        get_settings.cache_clear()


def test_eval_reports_traceability_dimension(tmp_path):
    """eval 应带溯源分维：每例 numeric/traced 计数 + 全局覆盖率。"""
    report = evaluate(mode="mock", trace_dir=tmp_path)
    m = report["metrics"]
    assert "numeric_claims_total" in m
    assert "traced_claims_total" in m
    assert "traceability_rate" in m
    assert 0 <= m["traceability_rate"] <= 1
    for case in report["cases_detail"]:
        assert "numeric_claims" in case and "traced_claims" in case
        # 无解析失败的溯源：assertions 里不应有"缺 sql_id/无效"类失败
        assert not any("sql_id" in a for a in case["failed_assertions"]), case


# --------------------------------------------------------------------------- #
# E6/01：分析师能力断言的机制本身（构造必失败用例来验证逻辑，而非永远绿灯）
# --------------------------------------------------------------------------- #
def test_quality_code_assertion_is_enforceable(tmp_path):
    from app.eval.golden import GoldenCase
    from app.eval.runner import evaluate_case

    case = GoldenCase(id="neg_qc", query="分析最近营收变化的原因",
                      expect_quality_codes=("绝不存在的问题码zzz",))
    out = evaluate_case(case, "mock", "eval_neg_qc", tmp_path)
    assert out.assertions_ok is False
    assert any("缺少质量门禁 code" in a for a in out.failed_assertions), out.failed_assertions


def test_caliber_kind_assertion_is_enforceable(tmp_path):
    from app.eval.golden import GoldenCase
    from app.eval.runner import evaluate_case

    case = GoldenCase(id="neg_cal", query="分析最近营收变化的原因",
                      expect_caliber_kinds=("绝不存在口径kind",))
    out = evaluate_case(case, "mock", "eval_neg_cal", tmp_path)
    assert out.assertions_ok is False
    assert any("缺少口径问题 kind" in a for a in out.failed_assertions)


def test_refusal_assertion_is_enforceable(tmp_path):
    """正常提问（没有忽略指令）配 expect_refusal=True → 必失败。"""
    from app.eval.golden import GoldenCase
    from app.eval.runner import evaluate_case

    case = GoldenCase(id="neg_refuse", query="分析最近营收变化的原因", expect_refusal=True)
    out = evaluate_case(case, "mock", "eval_neg_refuse", tmp_path)
    assert out.assertions_ok is False
    assert any("静默遵从" in a for a in out.failed_assertions)


def test_requires_real_is_skipped_in_mock_and_excluded_from_denominator(monkeypatch, tmp_path):
    """需真实模型的用例在 mock 下必须显式跳过，且**不计入**通过率分母。"""
    import app.eval.runner as runner
    from app.eval.golden import GoldenCase

    cases = (
        GoldenCase(id="m_ok", query="分析最近营收变化的原因"),
        GoldenCase(id="r_need", query="分析最近营收变化的原因",
                   expect_quality_codes=("untested_comparison",), requires_real=True),
    )
    monkeypatch.setattr(runner, "GOLDEN", cases)
    report = runner.evaluate(mode="mock", trace_dir=tmp_path)

    m = report["metrics"]
    assert m["skipped_requires_real"] == 1
    assert m["pass_rate"] == 1.0, "跳过项不得拉低通过率（分母只算真正跑的）"
    assert m["finish_rate"] == 1.0
    skipped = [c for c in report["cases_detail"] if c["status"] == "SKIPPED"]
    assert skipped and "requires_real" in (skipped[0].get("skipped_reason") or "")


def test_haystack_includes_limitations_and_quality_notes(monkeypatch, tmp_path):
    """披露字段必须可被 must_find 命中。

    构造一个**只在 limitations 里**出现的 token（report 里没有）：若 haystack 没扩到
    limitations，这条必失败——这才是有意义的证明（用 pipeline 自带文案会因 report
    也渲染了 limitations 而"通过得没有信息量"）。
    """
    import app.core.agents.data_analyst.graph as graph
    from app.core.agents.data_analyst.state import AgentState, AnalysisResult
    from app.eval.golden import GoldenCase
    from app.eval.runner import evaluate_case

    state = AgentState(session_id="lim_hit", user_query="q", status="FINISH")
    state.report = "# 报告\n没有任何特殊标记"          # report 里**不含**该 token
    state.analysis = AnalysisResult(limitations=["仅存在于 limitations 的标记ZZZ"])
    monkeypatch.setattr(graph, "run_analysis", lambda *a, **kw: state)

    case = GoldenCase(id="lim_hit", query="q", must_find=("标记zzz",))
    out = evaluate_case(case, "mock", "eval_lim_hit", tmp_path)
    assert out.assertions_ok is True, out.failed_assertions

    state.analysis.limitations = []                   # 拿掉后必须失败 → 证明检查有效
    out2 = evaluate_case(case, "mock", "eval_lim_hit2", tmp_path)
    assert out2.assertions_ok is False
