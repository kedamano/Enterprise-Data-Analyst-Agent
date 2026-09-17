"""E6-03：指标说实话 —— 分母、跳过、以及"未配"≠"免费"。

动因（真实基线 `eval-real-20260915-d54.md`）
--------------------------------------------
`工具成功率 0.228`（18/79）看似暴跌，逐条核对失败原因后发现：

- **44/61 是 `依赖步骤未完成`** —— 它们**从未执行**（上游失败被跳过）；
- 交叉验证：`state.tool_results` **79** 条，而 `data/audit/tool_audit.jsonl` 里
  本次会话只有 **35** 条（审计只记真的进了执行器的调用），**差值恰好 44**。

于是 `成功/(成功+失败)` 这个分母：D53 那版**虚高**（每步都"成功"地查同一张 3 行维表
→ 0.986），D54 这版**虚低**（大半"失败"没执行 → 0.228）。**两个方向都不能直接当质量读。**

同类问题还有成本：`compute_cost_usd` 早就区分了 `None`(没配) 与 `0.0`(免费)，
但 `render_markdown` 直接打印值，`None` 印成 `None`，而 `.env` 里写着 `COST_*=0`
把"没配"表达成了"免费"。报告里那行 `成本 USD 0.0` **是未计，不是零成本**。
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.agents.data_analyst.state import (
    AgentState,
    PlanModel,
    PlanStep,
    ToolResult,
)
from app.eval.runner import CaseOutcome, compute_cost_usd, render_markdown


# --------------------------------------------------------------------------- #
# 一、真实失败 vs 从未执行：两种东西必须能分开
# --------------------------------------------------------------------------- #
def test_dependency_skip_is_flagged_as_skipped():
    from app.core.agents.data_analyst import nodes

    st = AgentState(session_id="s", user_query="q")
    st.plan = PlanModel(goal="g", steps=[
        PlanStep(id="step_1", objective="取数", action="执行 SQL",
                 tool="sql_query", input={"sql": "SELECT 1"}),
        PlanStep(id="step_2", objective="再取数", action="执行 SQL",
                 tool="sql_query", input={"sql": "SELECT 2"},
                 dependencies=["step_1"]),
    ])
    # step_1 失败 → step_2 的依赖没完成
    st.tool_results = [ToolResult(step_id="step_1", tool="sql_query",
                                  status="FAILED", error="no such column: x")]
    st.current_step_index = 1
    nodes.run_executor(st)
    last = st.tool_results[-1]
    assert last.status == "FAILED"
    assert last.skipped is True, "`依赖步骤未完成` 是**未执行**，不是执行失败"


def test_real_failure_is_not_skipped():
    """真失败（DB 报错）**不得**被标成 skipped —— 否则它就白失败了。"""
    r = ToolResult(step_id="x", tool="sql_query", status="FAILED",
                   error="(sqlite3.OperationalError) no such column: f.order_id")
    assert r.skipped is False


def test_skipped_defaults_to_false():
    assert ToolResult(step_id="x", tool="sql_query", status="SUCCESS").skipped is False


# --------------------------------------------------------------------------- #
# 二、指标：分母只算"执行过的"，且跳过数必须可见
# --------------------------------------------------------------------------- #
def _outcome(success=0, fail=0, skipped=0) -> CaseOutcome:
    o = CaseOutcome(case_id="c", status="FINISH", assertions_ok=True)
    o.tool_success, o.tool_fail, o.tool_skipped = success, fail, skipped
    o.tool_calls = success + fail + skipped
    return o


def _metrics_for(outcomes, monkeypatch):
    from app.eval import runner as r

    monkeypatch.setattr(r, "GOLDEN", tuple(
        type("G", (), {"id": o.case_id, "query": "q", "requires_real": False})()
        for o in outcomes))
    monkeypatch.setattr(r, "evaluate_case", lambda case, *a, **k: next(
        o for o in outcomes if o.case_id == case.id))
    return r.evaluate("mock")["metrics"]


def test_skipped_are_excluded_from_the_denominator(monkeypatch):
    """5 成功 / 3 失败 / 10 跳过 → 分母是 8，不是 18。"""
    m = _metrics_for([_outcome(5, 3, 10)], monkeypatch)
    assert m["tool_success_rate"] == pytest.approx(5 / 8), m
    assert m["tool_skipped_total"] == 10
    assert m["tool_calls_total"] == 18, "总调用数口径不变（含 skipped）"


def test_all_success_with_skips_is_one(monkeypatch):
    m = _metrics_for([_outcome(5, 0, 10)], monkeypatch)
    assert m["tool_success_rate"] == 1.0, m


def test_nothing_executed_is_undefined_not_zero(monkeypatch):
    """全是跳过 → 分母 0 → **未定义**（`None`），不是 0。

    与 `hallucination_rate` 零 claim 的处理同一条纪律：
    把"没测到"报成 0 是最典型的自欺。
    """
    m = _metrics_for([_outcome(0, 0, 3)], monkeypatch)
    assert m["tool_success_rate"] is None, m
    assert m["tool_skipped_total"] == 3


def test_skipped_is_visible_in_the_report(monkeypatch):
    from app.eval import runner as r

    outcomes = [_outcome(5, 3, 10)]
    monkeypatch.setattr(r, "GOLDEN", (type("G", (), {
        "id": "c", "query": "q", "requires_real": False})(),))
    monkeypatch.setattr(r, "evaluate_case", lambda *a, **k: outcomes[0])
    md = render_markdown(r.evaluate("mock"))
    assert "跳过" in md, "跳过数必须报出来，否则读者不知道为什么分母变小了"


# --------------------------------------------------------------------------- #
# 三、成本：`None` 是"未计"，`0.0` 是"免费"
# --------------------------------------------------------------------------- #
def test_cost_semantics_are_unchanged():
    """既有契约，不得回退：没配 → None；显式 0 → 0.0。"""
    assert compute_cost_usd(None, None, 100, 100) is None
    assert compute_cost_usd(0, 0, 100, 100) == 0.0


def _report_with_cost(cost) -> dict:
    return {"mode": "mock", "cases": 0, "generated_at": "",
            "cases_detail": [], "gates": {"passed": True},
            "metrics": {"finish_rate": 1.0, "pass_rate": 1.0,
                        "skipped_requires_real": 0, "degraded_excluded": 0,
                        "scored_cases": 0, "tool_calls_total": 0,
                        "avg_tool_calls": 0.0, "tool_success_rate": None,
                        "tool_skipped_total": 0,
                        "llm_calls_total": 0, "avg_llm_calls": 0.0,
                        "prompt_tokens_total": 0, "completion_tokens_total": 0,
                        "tokens_total": 0, "cost_estimate_usd": cost,
                        "numeric_claims_total": 0, "traced_claims_total": 0,
                        "traceability_rate": None, "hallucination_rate": None,
                        "reflect_pass_rate": 0.0, "avg_report_len": 0.0,
                        "avg_duration_s": 0.0, "clarify_accepted": 0}}


def test_unpriced_cost_says_unbilled():
    md = render_markdown(_report_with_cost(None))
    assert "未计" in md, "单价没配时必须说'未计'，不能印 None 或 0.0"
    assert "0.0" not in md.split("成本")[1][:40]


def test_free_cost_says_free():
    md = render_markdown(_report_with_cost(0.0))
    assert "0.0" in md and "免费" in md


def test_priced_cost_shows_the_number():
    md = render_markdown(_report_with_cost(12.5))
    assert "12.5" in md


# --------------------------------------------------------------------------- #
# 四、用例明细表必须可见 per-case 成本（真实基线定位"谁烧的钱"）
# --------------------------------------------------------------------------- #
# 关键点：runner 在 evaluate() 末尾用 get_settings() 的 pi/po 重算每条 outcome 的 cost_usd；
# 所以测试不直接写 cost_usd，而是 monkeypatch get_settings 配已知单价，
# 并传已知 token 数，断言报告算出的数值符合 compute_cost_usd 的契约。
def _detail_outcome(case_id: str, status: str, tokens: int = 0) -> CaseOutcome:
    o = CaseOutcome(case_id=case_id, status=status, assertions_ok=(status == "FINISH"))
    o.prompt_tokens = tokens
    o.completion_tokens = 0
    return o


def _settings_stub(cost_in: float | None, cost_out: float | None):
    """构造一个最小 settings 桩，只回答 cost_input/output_per_mtok。"""
    return type("S", (), {"cost_input_per_mtok": cost_in,
                         "cost_output_per_mtok": cost_out})()


def _md_for(outcomes, *,
            cost_in: float | None = None,
            cost_out: float | None = None,
            patch_prices: bool = False) -> str:
    """构造报告字符串，并在返回前撤销 monkeypatch（避免污染其它测试）。

    默认不配单价（patch_prices=False）→ get_settings 走默认（未配 → 未计）。
    设 patch_prices=True → 用 cost_in/cost_out 替换单价，让 evaluate() 自己算 cost。
    """
    from app.eval import runner as r
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        mp.setattr(r, "GOLDEN", tuple(
            type("G", (), {"id": o.case_id, "query": "q", "requires_real": False})()
            for o in outcomes))
        mp.setattr(r, "evaluate_case", lambda case, *a, **k: next(
            o for o in outcomes if o.case_id == case.id))
        if patch_prices:
            mp.setattr(r, "get_settings",
                       lambda: _settings_stub(cost_in, cost_out))
        return r.render_markdown(r.evaluate("mock"))
    finally:
        mp.undo()


def test_detail_table_has_cost_column_in_header():
    md = _md_for([_detail_outcome("c", "FINISH")])
    # 表头行：| refl | cost | assert |  —— 两侧都是 ｜ 边界
    assert " refl | cost | assert " in md, f"明细表表头必须含 cost 列，实际 markdown: {md!r}"


def test_detail_unpriced_case_shows_unbilled_cost():
    """未配单价（默认）→ 报告印'未计'，绝不印 0 / None。"""
    md = _md_for([_detail_outcome("c", "SKIPPED", tokens=0)])
    row = [ln for ln in md.split("\n") if "| c | SKIPPED" in ln][0]
    assert "未计" in row, f"未配单价用例 cost 单元格应为'未计'，实际行: {row}"


def test_detail_priced_case_shows_numeric_cost():
    # 1M prompt × $2 / Mtok = $2.0；completion 未配（None → 代 0.0）→ 总 $2.0
    out = _detail_outcome("c", "FINISH", tokens=1_000_000)
    md = _md_for([out], patch_prices=True, cost_in=2.0, cost_out=None)
    row = [ln for ln in md.split("\n") if "| c | FINISH" in ln][0]
    assert "2.0" in row, f"有价用例 cost 单元格应显示 2.0，实际行: {row}"
    assert "免费" not in row, f"正数不应标'免费'，实际行: {row}"


def test_detail_free_case_shows_zero_and_free_note():
    # 单价显式 0 → 免费档，报告必须写 0 并标注免费
    out = _detail_outcome("c", "FINISH", tokens=1_000_000)
    md = _md_for([out], patch_prices=True, cost_in=0, cost_out=0)
    row = [ln for ln in md.split("\n") if "| c | FINISH" in ln][0]
    assert "0" in row and "免费" in row, f"免费档 cost 单元格应显示 0 + '免费'，实际行: {row}"
