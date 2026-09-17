"""E6/01 golden 断言方向守门：**断言必须能被正确行为满足**。

背景（本轮修掉的真缺陷）
------------------------
`r_join_amplification_guard` 原本期望 `expect_quality_codes=("join_amplified_used",)`。
而该 code 只在 **Agent 写错 SQL** 时才由 `gate.profile_gate` 产生：
结果行数 ≥ 1.5× 最大输入表行数（笛卡尔放大）**且**该结果被引用进结论。

正确实现（`JOIN dim_product ON product_id = product_id` + `GROUP BY category`）结果只有 4 行，
`factor ≈ 4 / 22767 ≈ 0.0002`，**永远不触发**该 code。

→ 这条 golden 变成了"只有 Agent 犯错才可能通过"，即**惩罚正确行为**的测试。
   它比"缺一条测试"更危险：正确实现会被报红，而写错的实现反而绿。

修法
----
改成**负向**断言 `must_not_have_quality_codes=("join_amplified_used",)`：
正确行为的两条要求——① 不产生"放大结果已进结论"的告警；② 真的答出各品类营收。

本文件同时钉住三件事，避免修复本身变成"把断言删掉"：
1. golden 不再索取一个正确行为无法产生的 code；
2. 该 code 的**检测能力没被削弱**（写错仍必被抓）——否则负向断言是空洞的；
3. runner 真的会执行负向断言（纯函数 `quality_code_violations`）。
"""
from __future__ import annotations

from app.core.agents.data_analyst.gate import (
    join_amplification_facts,
    profile_gate,
)
from app.core.agents.data_analyst.state import (
    AnalysisResult,
    Evidence,
    Finding,
    ToolResult,
)
from app.eval.golden import GOLDEN, GoldenCase

CASE_ID = "r_join_amplification_guard"


def _case(case_id: str = CASE_ID) -> GoldenCase:
    hit = [c for c in GOLDEN if c.id == case_id]
    assert hit, f"golden 里找不到用例 {case_id}"
    return hit[0]


# --------------------------------------------------------------------------- #
# 构造助手
# --------------------------------------------------------------------------- #
def _schema(*tables: tuple[str, int]) -> ToolResult:
    return ToolResult(step_id="s1", tool="schema_search", status="SUCCESS",
                      output={"ok": True, "tables": [
                          {"table": t, "row_count": n,
                           "columns": [{"name": "x", "type": "INTEGER"}]}
                          for t, n in tables]})


def _sql(step_id: str, sql: str, row_count: int) -> ToolResult:
    return ToolResult(step_id=step_id, tool="freeform", status="SUCCESS",
                      input={"sql": sql},
                      output={"ok": True, "row_count": row_count, "rows": []})


def _finding(text: str, *, sql_id: str | None = None) -> Finding:
    ev = Evidence(source="sql_query", value=1, sql_id=sql_id) if sql_id else []
    return Finding(finding=text, evidence=ev if isinstance(ev, list) else [ev], confidence=0.8)


def _codes(results: list[ToolResult], analysis: AnalysisResult) -> list[str]:
    return [i.code for i in profile_gate(results, analysis)]


# 正确写法：N:1 维表 join + 聚合到品类（4 行）
CORRECT_JOIN = ("SELECT p.category, SUM(o.gmv) AS gmv FROM fact_orders o "
                "JOIN dim_product p ON o.product_id = p.product_id GROUP BY p.category")
# 错误写法：忘写连接条件（逗号连接笛卡尔积），且不做聚合
CARTESIAN_JOIN = "SELECT o.gmv, p.category FROM fact_orders o, dim_product p"


# --------------------------------------------------------------------------- #
# 1. golden 不再索取"正确行为无法产生"的 code
# --------------------------------------------------------------------------- #
def test_join_golden_does_not_demand_amplification_code():
    """回归钉死：不得再要求 `join_amplified_used`（只有写错才产生）。"""
    case = _case()
    assert "join_amplified_used" not in case.expect_quality_codes, (
        "该 code 仅在 Agent 写错 SQL 时产生，正确实现永远无法满足 → 会惩罚正确行为"
    )


def test_join_golden_asserts_absence_of_amplification_alarm():
    """改成负向断言：正确行为必须**不**产生"放大结果已进结论"的告警。"""
    case = _case()
    assert "join_amplified_used" in case.must_not_have_quality_codes


def test_join_golden_still_requires_a_substantive_answer():
    """负向断言不能是唯一断言——否则"什么都不做"也能过。"""
    case = _case()
    assert case.must_find, "该用例必须仍要求报告答出业务内容（各品类营收）"


# --------------------------------------------------------------------------- #
# 2. 关键：新期望**可被正确行为满足**（修复前这条会因为"永不触发"而必然失败）
# --------------------------------------------------------------------------- #
def test_correct_join_produces_no_amplification_fact():
    results = [_schema(("fact_orders", 22767), ("dim_product", 8)),
               _sql("s3", CORRECT_JOIN, 4)]
    assert join_amplification_facts(results) == []


def test_correct_join_trips_no_amplification_code_even_if_cited():
    """正确 join 的结果被引用进结论，也不得产生任何 join_amp* 告警（无假阳性）。"""
    results = [_schema(("fact_orders", 22767), ("dim_product", 8)),
               _sql("s3", CORRECT_JOIN, 4)]
    analysis = AnalysisResult(findings=[_finding("Software 品类营收最高", sql_id="s3")])
    assert [c for c in _codes(results, analysis) if c.startswith("join_amp")] == []


def test_negative_expectation_is_not_vacuous_cartesian_still_caught():
    """检测能力未被削弱：相同的负向断言下，写错的写法仍必须产生 `join_amplified_used`。"""
    results = [_schema(("fact_orders", 22767), ("dim_product", 8)),
               _sql("s3", CARTESIAN_JOIN, 182136)]
    analysis = AnalysisResult(findings=[_finding("合计 GMV 约 1.2 亿", sql_id="s3")])
    assert "join_amplified_used" in _codes(results, analysis)


# --------------------------------------------------------------------------- #
# 3. runner 真的会执行负向断言
# --------------------------------------------------------------------------- #
def test_quality_code_violations_report_banned_code():
    from app.eval.runner import quality_code_violations

    case = GoldenCase(id="t", query="q",
                      must_not_have_quality_codes=("join_amplified_used",))
    bad = quality_code_violations(case, ["join_amplified_used", "date_sparse_claimed"])
    assert bad and "join_amplified_used" in bad[0]


def test_quality_code_violations_silent_when_absent():
    from app.eval.runner import quality_code_violations

    case = GoldenCase(id="t", query="q",
                      must_not_have_quality_codes=("join_amplified_used",))
    assert quality_code_violations(case, ["date_sparse_claimed"]) == []


def test_quality_code_violations_still_enforces_positive_expectation():
    """正向断言未被回退：缺 code 仍要报。"""
    from app.eval.runner import quality_code_violations

    case = GoldenCase(id="t", query="q", expect_quality_codes=("untested_comparison",))
    bad = quality_code_violations(case, [])
    assert bad and "untested_comparison" in bad[0]
