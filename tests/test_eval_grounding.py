"""E6-02：评测门禁 —— 让"编出来的数字"能否决一次通过。

动因（`eval --mode real` 首次全量基线，2026-09-15）
---------------------------------------------------
`q_region_top` **凭空造出一整张区域营收表**（华东 1,245,000 / +18.5% / 占比 32%…），
而该用例实际只执行了一条 `SELECT * FROM dim_channel LIMIT 100`（3 行维表）——**却判 ✅**。

为什么现有断言拦不住：

- `sources.unresolved_numeric_claims` 只遍历 **`findings[].evidence[].value`**，
  **报告正文的数值从来不检查**；
- `min_findings` 只加在 `r_*` 用例上，5 个基础 `q_*` 用默认值 0
  → `findings=0` 的四条用例**空洞通过**；
- `疑似幻觉率 0.667` 只是报告里的一行字，**对 ✅/❌ 没有任何影响**。

本文件钉住：正文大额数值必须可溯源 + 用例级断言 + 运行级门禁（含 `--strict` 退出码）。
"""
from __future__ import annotations

import pytest

from app.core.agents.data_analyst import nodes as da_nodes
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    Evidence,
    Finding,
    ToolResult,
)
from app.eval.golden import GOLDEN, GoldenCase
from app.eval.runner import (
    compute_gates,
    evaluate_case,
    grounding_violations,
    ungrounded_numbers,
)


def _sql_result(rows, sid="step_3"):
    return ToolResult(step_id=sid, tool="sql_query", status="SUCCESS",
                      input={"sql": "SELECT region_id, SUM(revenue) FROM fact_sales GROUP BY 1"},
                      output={"ok": True, "rows": rows})


# --------------------------------------------------------------------------- #
# 一、`ungrounded_numbers`：正文里"哪儿都找不到"的大额数值
# --------------------------------------------------------------------------- #
def test_catches_fabricated_number():
    """真基线的原样复现：报告里的 1,245,000 在工具结果里根本不存在。"""
    report = "华东区域营收 1,245,000 元，环比增长 18.5%，占比 32%。"
    bad = ungrounded_numbers(report, [_sql_result([{"region_name": "华东", "total": 812340}])], [])
    assert "1,245,000" in bad or "1245000" in "".join(bad), f"应抓到编造的营收数字，实际 {bad}"


def test_ignores_small_structural_numbers():
    """阈值下限：年份/百分比/排名/计数是报告的结构件，不是结论——不能误伤。"""
    report = "2024 年 Q3，TOP 10 渠道中 3 个渠道转化率下降 18.5%，样本 42 天。"
    assert ungrounded_numbers(report, [], []) == []


def test_thousand_separator_matches_float():
    report = "华东营收 1,245,000 元。"
    tools = [_sql_result([{"region_name": "华东", "revenue": 1245000.0}])]
    assert ungrounded_numbers(report, tools, []) == []


def test_relative_tolerance_allows_rounding():
    """四舍五入/单位换算后的呈现不算编造：1,244,893 vs 1,245,000 差 0.009%。"""
    report = "华东营收 1,244,893 元。"
    tools = [_sql_result([{"revenue": 1245000.0}])]
    assert ungrounded_numbers(report, tools, []) == []


def test_relative_tolerance_still_catches_a_different_number():
    """但差 5% 就是另一个数了——容差不等于放水。"""
    report = "华东营收 1,180,000 元。"
    tools = [_sql_result([{"revenue": 1245000.0}])]
    assert ungrounded_numbers(report, tools, []) != []


def test_every_tool_output_counts_as_a_source():
    """出处不限于 sql_query：dataset_profile / visualization 的输出同样是证据。"""
    prof = ToolResult(step_id="s2", tool="dataset_profile", status="SUCCESS",
                      output={"ok": True, "table": "fact_sales",
                              "numeric_summary": {"revenue": {"max": 987654.0}}})
    assert ungrounded_numbers("峰值营收 987,654 元。", [prof], []) == []


def test_failed_tool_results_are_not_a_source():
    """失败的工具结果**不是**证据——否则"报错的查询"会成为编造数字的挡箭牌。"""
    failed = ToolResult(step_id="s3", tool="sql_query", status="FAILED",
                        error="no such table", output={"rows": [{"revenue": 1245000.0}]})
    assert ungrounded_numbers("营收 1,245,000 元。", [failed], []) != []


def test_finding_evidence_is_a_source():
    """证据值**确实来自它声称的那条 SQL** 时，算出处。"""
    tools = [_sql_result([{"region_name": "华东", "total": 1245000.0}], sid="step_3")]
    f = Finding(finding="华东贡献主要营收", evidence=[
        Evidence(source="sql_query", value="1245000.0", sql_id="step_3")])
    assert ungrounded_numbers("华东营收 1,245,000 元。", tools, [f]) == []


def test_evidence_cannot_vouch_for_itself():
    """**模型的自我声明不能给自己作证**——D54 用真实产物复现出来的洞。

    真实产物 `data/checkpoints/eval_q_region_top_0b7e93.json`：报告里那张编造的
    区域营收表**同时**写在 `findings[].evidence[].value` 里，每条都标着
    `sql_id: "step_5"`、`row_sample: "[]"`，而 `step_5` 实际是一条 3 行维表查询，
    **输出里根本没有这些数字**。无条件采信 `evidence.value` 就等于：
    编一个数 → 写进 evidence → 报告里再写一遍 → 全部"有出处"。
    """
    tools = [_sql_result([{"channel_id": 1, "channel_name": "直销"},
                          {"channel_id": 2, "channel_name": "合作伙伴"},
                          {"channel_id": 3, "channel_name": "线上"}], sid="step_5")]
    f = Finding(finding="华东地区营收最高", evidence=[
        Evidence(source="sql_query", value="1,245,000", sql_id="step_5")])
    bad = ungrounded_numbers("华东区域营收 1,245,000 元。", tools, [f])
    assert bad, "证据值与它声称的 SQL 输出对不上时，不得当作出处"
    assert any("1,245,000" in b or "1245000" in b for b in bad), bad


def test_evidence_with_dangling_sql_id_is_not_a_source():
    """`sql_id` 缺失/悬空 → 不算出处（与 lineage「无 sql_id → traced=False」同纪律）。"""
    # 工具输出里**没有** 1,245,000：这样"被判无源"只可能来自 evidence 那一侧
    tools = [_sql_result([{"revenue": 812340.0}], sid="step_3")]
    for sid in ("", "step_99"):
        f = Finding(finding="x", evidence=[
            Evidence(source="sql_query", value="1245000.0", sql_id=sid)])
        assert ungrounded_numbers("营收 1,245,000 元。", tools, [f]) != [], sid


def test_evidence_from_failed_step_is_not_a_source():
    """证据声称的步骤**失败**了 → 不算出处。"""
    failed = ToolResult(step_id="step_9", tool="sql_query", status="FAILED",
                        error="no such table",
                        output={"rows": [{"revenue": 1245000.0}]})
    f = Finding(finding="x", evidence=[
        Evidence(source="sql_query", value="1245000.0", sql_id="step_9")])
    assert ungrounded_numbers("营收 1,245,000 元。", [failed], [f]) != []


def test_empty_report_is_not_an_error():
    assert ungrounded_numbers("", [], []) == []
    assert ungrounded_numbers(None, [], []) == []


# --------------------------------------------------------------------------- #
# 二、用例级：`max_ungrounded_numbers`
# --------------------------------------------------------------------------- #
def _run_case(monkeypatch, case, *, report, rows, findings_count=1):
    """把 `run_analysis` 换成受控 state，钉住"评测器确实读了正文"。"""
    def _fake_run(session_id, query):
        st = AgentState(session_id=session_id, user_query=query, status="FINISH")
        st.tool_results = [_sql_result(rows)]
        st.analysis = AnalysisResult(findings=[
            Finding(finding=f"发现 {i}", evidence=[
                Evidence(source="sql_query", value="812340", sql_id="step_3")])
            for i in range(findings_count)])
        st.report = report
        return st

    monkeypatch.setattr("app.core.agents.data_analyst.graph.run_analysis", _fake_run)
    return evaluate_case(case, "mock", "ground_case", None)


_CASE_GROUNDED = GoldenCase(id="g_on", query="q", max_ungrounded_numbers=0)
_CASE_DEFAULT = GoldenCase(id="g_off", query="q")


def test_case_assertion_fails_on_ungrounded_report_number(monkeypatch):
    out = _run_case(monkeypatch, _CASE_GROUNDED,
                    report="华东区域营收 1,245,000 元，环比增长 18.5%。",
                    rows=[{"region_name": "华东", "total": 812340}])
    assert out.assertions_ok is False, "报告正文编造的数值必须让该用例失败"
    assert any("1,245,000" in m or "1245000" in m for m in out.failed_assertions), out.failed_assertions


def test_case_assertion_passes_when_numbers_are_grounded(monkeypatch):
    out = _run_case(monkeypatch, _CASE_GROUNDED,
                    report="华东区域营收 812,340 元。",
                    rows=[{"region_name": "华东", "total": 812340}])
    assert out.assertions_ok is True, out.failed_assertions


def test_case_assertion_is_opt_in(monkeypatch):
    """默认（-1）不检查——mock 模板报告不该被这条断言判红。"""
    out = _run_case(monkeypatch, _CASE_DEFAULT,
                    report="华东区域营收 1,245,000 元。",
                    rows=[{"region_name": "华东", "total": 812340}])
    assert out.assertions_ok is True, out.failed_assertions


def test_grounding_violations_is_a_pure_function():
    assert grounding_violations(_CASE_DEFAULT, ["1,245,000"]) == []
    msgs = grounding_violations(_CASE_GROUNDED, ["1,245,000"])
    assert msgs and "1,245,000" in msgs[0]


# --------------------------------------------------------------------------- #
# 三、min_findings：基础 q_* 用例不再允许"零发现也通过"
# --------------------------------------------------------------------------- #
# 唯一豁免：该用例的契约是"**不得静默**"（用户下令忽略数据质量），
# 不是"给出业务结论"。用户原话是"直接给结论就行"，被路由到 `quick_answer`
# 后**本就不产 findings**——那是模式的正确行为。硬加 `min_findings=1`
# 只会得到一个 mock 专属假红。
_DISCLOSURE_ONLY = {"a_dq_override_not_silent"}


def test_base_cases_require_findings():
    base = [c for c in GOLDEN if c.id.startswith(("q_", "a_"))]
    assert base, "基础用例集不应为空"
    weak = [c.id for c in base
            if c.min_findings < 1 and c.id not in _DISCLOSURE_ONLY]
    assert not weak, (
        f"这些用例仍允许 findings=0 通过（真基线里 4 条空洞 ✅ 的来源）: {weak}"
    )


def test_the_min_findings_exemption_is_not_a_loophole():
    """豁免 `min_findings` 的代价**必须由别的断言补上**，不能白豁免。

    没有这条守卫，将来往 `_DISCLOSURE_ONLY` 里塞一个普通用例就悄悄拆掉了断言。
    """
    assert _DISCLOSURE_ONLY, "豁免集为空时本用例无意义（应随之删除）"
    for cid in _DISCLOSURE_ONLY:
        case = next(c for c in GOLDEN if c.id == cid)
        assert case.expect_refusal, (
            f"{cid} 既不要求 findings、又不要求'不得静默'，等于没有断言")


def test_base_cases_check_report_grounding():
    base = [c for c in GOLDEN if c.id.startswith(("q_", "a_"))]
    weak = [c.id for c in base if c.max_ungrounded_numbers < 0]
    assert not weak, f"这些用例不检查正文数值溯源，编表抓不住: {weak}"


def test_zero_findings_case_fails(monkeypatch):
    case = GoldenCase(id="mf", query="q", min_findings=1, max_ungrounded_numbers=0)
    out = _run_case(monkeypatch, case, report="区域营收 812,340 元。",
                    rows=[{"region_name": "华东", "total": 812340}], findings_count=0)
    assert out.assertions_ok is False
    assert any("少于要求的 1 条" in m for m in out.failed_assertions), out.failed_assertions


# --------------------------------------------------------------------------- #
# 四、运行级门禁 + `--strict`
# --------------------------------------------------------------------------- #
def _metrics(**over):
    m = {"finish_rate": 1.0, "pass_rate": 1.0, "skipped_requires_real": 0,
         "degraded_excluded": 0, "scored_cases": 5, "hallucination_rate": 0.0,
         "numeric_claims_total": 4, "traced_claims_total": 4}
    m.update(over)
    return m


def test_gates_pass_on_a_clean_run():
    gates = compute_gates(_metrics(), ungrounded_total=0)
    assert gates["passed"] is True
    assert gates["hallucination"]["passed"] is True
    assert gates["evidence"]["passed"] is True


def test_gates_veto_on_hallucination():
    gates = compute_gates(_metrics(hallucination_rate=0.667), ungrounded_total=0)
    assert gates["hallucination"]["passed"] is False
    assert gates["passed"] is False


def test_gates_veto_on_ungrounded_report_numbers():
    """真基线里"编了整张表却 ✅"的那条，必须在门禁上被否决。"""
    gates = compute_gates(_metrics(), ungrounded_total=6)
    assert gates["grounded_numbers"]["passed"] is False
    assert gates["passed"] is False


def test_gates_veto_on_skipped_or_degraded():
    assert compute_gates(_metrics(skipped_requires_real=2), 0)["passed"] is False
    assert compute_gates(_metrics(degraded_excluded=1), 0)["passed"] is False


def test_mock_skips_do_not_veto_but_real_skips_do():
    """`requires_real` 用例在 mock 下**按设计**跳过，不是缺陷。

    若不计模式地否决，`--mode mock --strict` 将**永远**退出 2 ——
    一个恒红的门禁会被直接绕过，等于没有门禁。
    """
    m = _metrics(skipped_requires_real=7)
    assert compute_gates(m, 0, mode="mock")["passed"] is True
    assert compute_gates(m, 0, mode="real")["passed"] is False
    # 但"跳过"在两种模式下都仍然**可见**（不得藏起来）
    assert compute_gates(m, 0, mode="mock")["evidence"]["skipped"] == 7


def test_degraded_vetoes_in_both_modes():
    """降级 = "拿 mock 冒充真模型"，两种模式下都是真缺陷。"""
    m = _metrics(degraded_excluded=1, skipped_requires_real=0)
    assert compute_gates(m, 0, mode="mock")["passed"] is False
    assert compute_gates(m, 0, mode="real")["passed"] is False


def test_hallucination_none_is_undefined_not_zero(monkeypatch):
    """零数值 claim → 幻觉率是 `None`（未定义）。**未定义 ≠ 零幻觉**，但也不是否决理由。"""
    gates = compute_gates(_metrics(hallucination_rate=None, numeric_claims_total=0,
                                   traced_claims_total=0), ungrounded_total=0)
    assert gates["hallucination"]["value"] is None
    assert gates["hallucination"]["passed"] is True


def test_report_contains_gates(monkeypatch):
    from app.eval import runner as r

    monkeypatch.setattr(r, "evaluate_case", lambda *a, **k: r.CaseOutcome(
        case_id="x", status="FINISH", assertions_ok=True))
    monkeypatch.setattr(r, "GOLDEN", (GoldenCase(id="x", query="q"),))
    report = r.evaluate("mock")
    assert "gates" in report and "passed" in report["gates"]
    assert "门禁" in r.render_markdown(report)


def test_strict_flag_exits_nonzero_when_gates_fail(monkeypatch):
    from app.eval import runner as r

    monkeypatch.setattr(r, "evaluate", lambda *a, **k: {
        "mode": "mock", "cases": 0, "generated_at": "", "cases_detail": [],
        "metrics": _metrics(hallucination_rate=0.667),
        "gates": compute_gates(_metrics(hallucination_rate=0.667), 0)})
    monkeypatch.setattr(r, "render_markdown", lambda report: "# r")
    monkeypatch.setattr("sys.argv", ["runner", "--mode", "mock", "--strict"])
    with pytest.raises(SystemExit) as exc:
        r.main()
    assert exc.value.code == 2


def test_without_strict_exit_code_is_unchanged(monkeypatch):
    """既有脚本与 CI 依赖"跑完即退出码 0"，不许静默改语义。"""
    from app.eval import runner as r

    monkeypatch.setattr(r, "evaluate", lambda *a, **k: {
        "mode": "mock", "cases": 0, "generated_at": "", "cases_detail": [],
        "metrics": _metrics(hallucination_rate=0.667),
        "gates": compute_gates(_metrics(hallucination_rate=0.667), 0)})
    monkeypatch.setattr(r, "render_markdown", lambda report: "# r")
    monkeypatch.setattr("sys.argv", ["runner", "--mode", "mock"])
    r.main()   # 不抛 SystemExit
