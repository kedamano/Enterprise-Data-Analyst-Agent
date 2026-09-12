"""E4/04 质量门禁：profile 基元必须变成决策与披露，而不是躺在 tool_results 里。

Spec: docs/specs/E4/04-quality-gate.md
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.gate import (
    GateIssue,
    apply_gate,
    is_blocked,
    join_amplification_facts,
    profile_gate,
)
from app.core.agents.data_analyst.state import (
    AnalysisResult,
    Evidence,
    Finding,
    ReflectionDecision,
    ReflectionResult,
    ToolResult,
)
from app.infrastructure.llm.router import reset_llm

@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 构造助手
# --------------------------------------------------------------------------- #
def _schema(row_count: int = 3120, table: str = "fact_sales") -> ToolResult:
    return ToolResult(step_id="s1", tool="schema_search", status="SUCCESS",
                      output={"ok": True, "tables": [
                          {"table": table, "columns": [{"name": "sale_id", "type": "INTEGER"}],
                           "row_count": row_count}]})


def _sql(step_id: str, sql: str, row_count: int) -> ToolResult:
    return ToolResult(step_id=step_id, tool="freeform", status="SUCCESS",
                      input={"sql": sql}, output={"ok": True, "row_count": row_count, "rows": []})


def _profile(step_id: str = "s2", *, key_unique=None, dup_ratio=None, grain="row",
             sparse=None, columns=None, join_amp=None) -> ToolResult:
    out = {
        "ok": True, "row_count": 3120,
        "columns": columns if columns is not None else {"sale_id": {"null_count": 0, "null_ratio": 0.0, "distinct": 3120}},
        "key_uniqueness": {"candidate_keys": ["sale_id"], "likely_key": "sale_id",
                           "declared_key": ["sale_id"] if key_unique is not None else [],
                           "is_unique": key_unique, "duplicate_rows": None,
                           "duplicate_ratio": dup_ratio, "grain": grain},
        "join_amplification": join_amp,
        "date_continuity": (None if sparse is None else
                            {"column": "sale_date", "min": "2024-01-01", "max": "2024-12-23",
                             "distinct_days": 52, "expected_days": 358, "missing_days": 306,
                             "coverage_ratio": 0.1453, "sparse": sparse, "gap_samples": []}),
    }
    return ToolResult(step_id=step_id, tool="dataset_profile", status="SUCCESS", output=out)


def _finding(text: str, *, value=None, sql_id=None) -> Finding:
    ev = Evidence(source="sql_query", value=value, sql_id=sql_id) if value is not None else []
    return Finding(finding=text, evidence=ev if isinstance(ev, list) else [ev], confidence=0.8)


def _analysis(*findings: Finding) -> AnalysisResult:
    return AnalysisResult(findings=list(findings))


CROSS_JOIN = "SELECT f.sale_id, r.region_name FROM fact_sales f CROSS JOIN dim_region r"
DIM_JOIN = ("SELECT f.sale_id, r.region_name FROM fact_sales f "
            "JOIN dim_region r ON f.region_id = r.region_id")


# --------------------------------------------------------------------------- #
# 1. join 放大
# --------------------------------------------------------------------------- #
def test_amplified_join_used_in_conclusion_is_blocked():
    results = [_schema(), _sql("s3", CROSS_JOIN, 15600)]
    issues = profile_gate(results, _analysis(_finding("合计营收 1.2 亿", value=120000000, sql_id="s3")))

    hit = [i for i in issues if i.code == "join_amplified_used"]
    assert hit and hit[0].severity == "BLOCK", issues
    assert "15600" in hit[0].detail or "5" in hit[0].detail


def test_amplified_join_unused_is_annotated():
    results = [_schema(), _sql("s3", CROSS_JOIN, 15600)]
    issues = profile_gate(results, _analysis(_finding("整体经营稳定")))

    hit = [i for i in issues if i.code == "join_amplified_unused"]
    assert hit and hit[0].severity == "ANNOTATE", issues


def test_normal_dimension_join_not_flagged():
    """维表 join 事实表（N:1）行数不变 → 不得误报。"""
    results = [_schema(), _sql("s3", DIM_JOIN, 3120)]
    issues = profile_gate(results, _analysis(_finding("各区域营收合计", value=100, sql_id="s3")))
    assert [i for i in issues if i.code.startswith("join_amp")] == []


# 逗号连接：`FROM a, b`。这是"忘写 join 条件"最常见的写法，
# 早期实现只认 `join` 关键字（`_JOIN_RE`），导致最该被抓的笛卡尔积反而逃过检测。
COMMA_CROSS = "SELECT f.sale_id, r.region_name FROM fact_sales f, dim_region r"


def test_comma_join_amplification_used_is_blocked():
    """逗号连接的笛卡尔积进了结论 → 必须 BLOCK（修复前该形态被整体跳过）。"""
    results = [_schema(), _sql("s3", COMMA_CROSS, 15600)]
    issues = profile_gate(results, _analysis(_finding("合计营收 1.2 亿", value=120000000, sql_id="s3")))
    hit = [i for i in issues if i.code == "join_amplified_used"]
    assert hit and hit[0].severity == "BLOCK", issues


def test_comma_join_amplification_unused_is_annotated():
    results = [_schema(), _sql("s3", COMMA_CROSS, 15600)]
    issues = profile_gate(results, _analysis(_finding("整体经营稳定")))
    hit = [i for i in issues if i.code == "join_amplified_unused"]
    assert hit and hit[0].severity == "ANNOTATE", issues


@pytest.mark.parametrize("sql", [
    "SELECT sale_id, region_id FROM fact_sales",                              # 选择列逗号
    "SELECT COUNT(*) FROM fact_sales WHERE region_id IN (1, 2)",              # IN 列表逗号
    "SELECT region_id FROM fact_sales GROUP BY region_id, sale_id",           # GROUP BY 逗号
    "SELECT region_id FROM fact_sales ORDER BY region_id, sale_id",           # ORDER BY 逗号
    "SELECT * FROM fact_sales WHERE region_id = 1",                           # 单表无条件
])
def test_comma_elsewhere_not_treated_as_join(sql):
    """FROM 子句**以外**的逗号（选择列 / IN / GROUP BY / ORDER BY）不得被误判成多表连接。"""
    results = [_schema(), _sql("s3", sql, 3120)]
    issues = profile_gate(results, _analysis(_finding("合计营收 1.2 亿", value=120000000, sql_id="s3")))
    assert [i for i in issues if i.code.startswith("join_amp")] == [], sql


def test_comma_join_with_condition_not_flagged():
    """逗号连接的写法 + 正确条件（行数不变）→ 不得误报。"""
    sql = ("SELECT f.sale_id, r.region_name FROM fact_sales f, dim_region r "
           "WHERE f.region_id = r.region_id")
    results = [_schema(), _sql("s3", sql, 3120)]
    issues = profile_gate(results, _analysis(_finding("各区域营收合计", value=100, sql_id="s3")))
    assert [i for i in issues if i.code.startswith("join_amp")] == []


def test_without_schema_row_count_check_is_skipped():
    """拿不到基线就宁缺勿滥，不判。"""
    results = [_sql("s3", CROSS_JOIN, 15600)]
    assert join_amplification_facts(results) == []


def test_declared_amplification_is_honoured():
    """E4/01 的声明式放大也要被消费。"""
    amp = {"base_table": "fact_sales", "base_rows": 3120, "result_rows": 15600,
           "factor": 5.0, "amplified": True, "threshold": 1.5}
    results = [_schema(), _profile("s2", join_amp=amp)]
    issues = profile_gate(results, _analysis(_finding("合计", value=1, sql_id="s2")))
    assert [i for i in issues if i.code == "join_amplified_used"]


# --------------------------------------------------------------------------- #
# 2. 主键唯一性（双条件）
# --------------------------------------------------------------------------- #
def test_non_unique_key_with_aggregate_claim_replans():
    results = [_profile("s2", key_unique=False, dup_ratio=0.5)]
    issues = profile_gate(results, _analysis(_finding("按 region_id 汇总合计营收", value=1)))

    hit = [i for i in issues if i.code == "key_not_unique"]
    assert hit and hit[0].severity == "REPLAN" and hit[0].fixable is True, issues


def test_non_unique_key_without_aggregate_claim_passes():
    results = [_profile("s2", key_unique=False, dup_ratio=0.5)]
    issues = profile_gate(results, _analysis(_finding("数据覆盖 5 个区域")))
    assert [i for i in issues if i.code == "key_not_unique"] == []


def test_near_unique_key_is_only_annotated():
    results = [_profile("s2", key_unique=False, dup_ratio=0.001)]
    issues = profile_gate(results, _analysis(_finding("合计营收", value=1)))
    hit = [i for i in issues if i.code == "key_not_unique"]
    assert hit and hit[0].severity == "ANNOTATE"


# --------------------------------------------------------------------------- #
# 3. 日期稀疏 / 粒度 / null（必须有主张才触发）
# --------------------------------------------------------------------------- #
def test_sparse_dates_with_trend_claim_annotated():
    results = [_profile("s2", sparse=True)]
    issues = profile_gate(results, _analysis(_finding("营收呈逐周下降趋势")))
    hit = [i for i in issues if i.code == "date_sparse_claimed"]
    assert hit and hit[0].severity == "ANNOTATE", issues


def test_sparse_dates_without_claim_passes():
    """样例库日期天然稀疏（52/358 天）——没有连续性主张就不得报警，否则每个用例都亮。"""
    results = [_profile("s2", sparse=True)]
    issues = profile_gate(results, _analysis(_finding("华东营收最高", value=1)))
    assert [i for i in issues if i.code == "date_sparse_claimed"] == []


def test_grain_misread_detected():
    results = [_profile("s2", grain="aggregated", key_unique=None)]
    issues = profile_gate(results, _analysis(_finding("逐条明细显示华东最好")))
    assert [i for i in issues if i.code == "grain_misread"]


def test_high_null_on_mentioned_column():
    results = [_profile("s2", columns={"channel_id": {"null_count": 1500, "null_ratio": 0.48, "distinct": 3}})]
    issues = profile_gate(results, _analysis(_finding("channel_id 渠道差异明显")))
    assert [i for i in issues if i.code == "null_high_on_group"]


# --------------------------------------------------------------------------- #
# 4. 放行条件
# --------------------------------------------------------------------------- #
def test_empty_table_and_missing_primitives_pass():
    empty = ToolResult(step_id="s2", tool="dataset_profile", status="SUCCESS",
                       output={"ok": True, "row_count": 0,
                               "key_uniqueness": {"candidate_keys": [], "likely_key": None,
                                                  "declared_key": [], "is_unique": None,
                                                  "duplicate_rows": None, "duplicate_ratio": None,
                                                  "grain": "unknown"},
                               "join_amplification": None, "date_continuity": None, "columns": {}})
    assert profile_gate([empty], _analysis(_finding("无数据，合计为 0"))) == []


def test_failed_profile_step_is_ignored():
    bad = ToolResult(step_id="s2", tool="dataset_profile", status="FAILED",
                     output={"ok": False, "error": "x"})
    assert profile_gate([bad], _analysis(_finding("合计", value=1))) == []


# --------------------------------------------------------------------------- #
# 5. apply_gate 只收紧
# --------------------------------------------------------------------------- #
def _refl(decision: ReflectionDecision) -> ReflectionResult:
    return ReflectionResult(decision=decision, confidence=0.9)


def test_apply_gate_only_tightens():
    """BLOCK/REPLAN 都抬到 REPLAN（可补救）；ANNOTATE 不改决策；LLM 的 FAIL 永不被放松。"""
    block = [GateIssue(code="join_amplified_used", severity="BLOCK", detail="d", fixable=True)]
    replan = [GateIssue(code="key_not_unique", severity="REPLAN", detail="d", fixable=True)]
    annot = [GateIssue(code="date_sparse_claimed", severity="ANNOTATE", detail="d")]

    assert apply_gate(block, _refl(ReflectionDecision.PASS))[0] == ReflectionDecision.REPLAN
    assert apply_gate(block, _refl(ReflectionDecision.REPLAN))[0] == ReflectionDecision.REPLAN
    assert apply_gate(block, _refl(ReflectionDecision.FAIL))[0] == ReflectionDecision.FAIL
    assert apply_gate(replan, _refl(ReflectionDecision.PASS))[0] == ReflectionDecision.REPLAN
    assert apply_gate(annot, _refl(ReflectionDecision.PASS))[0] == ReflectionDecision.PASS
    assert apply_gate(annot, _refl(ReflectionDecision.REPLAN))[0] == ReflectionDecision.REPLAN
    assert apply_gate(annot, _refl(ReflectionDecision.FAIL))[0] == ReflectionDecision.FAIL
    assert apply_gate([], _refl(ReflectionDecision.PASS)) == (ReflectionDecision.PASS, [])


def test_block_is_distinguished_for_exhaustion():
    """BLOCK 与 REPLAN 的差别在"重试额度用尽时"：BLOCK 必须 FAILED，不得 best-effort 出报告。"""
    block = [GateIssue(code="join_amplified_used", severity="BLOCK", detail="d")]
    replan = [GateIssue(code="key_not_unique", severity="REPLAN", detail="d")]
    annot = [GateIssue(code="date_sparse_claimed", severity="ANNOTATE", detail="d")]

    assert is_blocked(block) is True
    assert is_blocked(replan) is False
    assert is_blocked(annot) is False
    assert is_blocked([]) is False


def test_apply_gate_returns_disclosure_text():
    annot = [GateIssue(code="date_sparse_claimed", severity="ANNOTATE", detail="日期稀疏")]
    decision, notes = apply_gate(annot, _refl(ReflectionDecision.PASS))
    assert decision == ReflectionDecision.PASS
    assert notes and "日期稀疏" in notes[0]


# --------------------------------------------------------------------------- #
# 6. 节点级集成：门禁真的改了决策 / 披露
# --------------------------------------------------------------------------- #
import json  # noqa: E402

from app.core.agents.data_analyst.state import AgentState  # noqa: E402

_PASS_JSON = json.dumps({
    "decision": "PASS", "confidence": 0.9, "summary": "证据充分",
    "data_quality": {"score": 0.9, "issues": []}, "metric_quality": {"score": 0.9, "issues": []},
    "evidence_coverage": {"score": 0.9, "issues": []}, "logical_validity": {"score": 0.9, "issues": []},
    "completeness": {"score": 0.9, "issues": []}, "business_relevance": {"score": 0.9, "issues": []},
    "missing_evidence": [], "replan_objectives": [],
})


def _agg_state(session_id="gate_node") -> AgentState:
    from app.core.agents.data_analyst.state import Evidence, Finding
    st = AgentState(session_id=session_id, user_query="各区域营收合计是多少")
    st.analysis = AnalysisResult(findings=[
        Finding(finding="合计营收 1 亿", confidence=0.9,
                evidence=[Evidence(source="sql_query", value=100000000, sql_id="s3")])])
    st.tool_results = [_schema(), _sql("s3", CROSS_JOIN, 15600)]
    return st


def test_reflection_gate_tightens_pass_and_records_issues(monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = run_reflection(_agg_state())

    assert st.reflection.decision == "REPLAN", "LLM 说 PASS，但结论用了放大后的结果 → 必须收紧"
    assert st.status == "REPLAN"
    assert st.metadata["gate_issues"] and st.metadata["gate_block"] is True
    assert st.analysis.quality_notes, "披露文本要落 analysis.quality_notes"
    assert any("放大" in o for o in st.reflection.replan_objectives), "缺口要喂给 planner"


def test_reflection_without_issues_keeps_llm_decision(monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = AgentState(session_id="gate_clean", user_query="各区域营收")
    st.analysis = AnalysisResult(findings=[_finding("华东营收最高")])
    st.tool_results = [_schema(), _sql("s3", DIM_JOIN, 3120)]  # 正常维表 join
    st = run_reflection(st)

    assert st.reflection.decision == "PASS" and st.status == "REPORT"
    assert st.metadata.get("gate_issues") == []
    assert st.analysis.quality_notes == []
    assert "数据质量与限制" not in _report_of(st)


def test_block_with_exhausted_replans_fails_instead_of_reporting(monkeypatch):
    """BLOCK 与 REPLAN 的差别：额度用尽时不得 best-effort 出报告。"""
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _PASS_JSON)
    st = _agg_state("gate_block_exhausted")
    st.max_replans = 0
    st.replan_count = 0
    st = run_reflection(st)

    assert st.reflection.decision == "REPLAN"
    assert st.status == "FAILED", "数学上错的结论不得降级为 best-effort 报告"
    assert st.error and "质量门禁" in st.error


def test_replan_without_block_still_reports_best_effort(monkeypatch):
    """既有行为不回退：非 BLOCK 的 REPLAN 在额度用尽时仍出 best-effort 报告。"""
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_reflection

    replan_json = json.loads(_PASS_JSON)
    replan_json["decision"] = "REPLAN"
    monkeypatch.setattr(nodes, "_llm",
                        lambda stage, user, json_mode=True: json.dumps(replan_json))
    st = AgentState(session_id="gate_replan_exhausted", user_query="分析营收")
    st.analysis = AnalysisResult(findings=[_finding("证据不足的结论")])
    st.tool_results = []
    st.max_replans = 0
    st = run_reflection(st)

    assert st.status == "REPORT"
    assert st.metadata.get("gate_block") is False


def _report_of(state) -> str:
    from app.core.tools.report_tool import run as report_run
    return report_run({"analysis": state.analysis.model_dump(),
                       "reflection": state.reflection.model_dump() if state.reflection else None,
                       "objective": state.context.objective}).get("report", "")


def test_report_shows_quality_section_only_when_issues():
    from app.core.tools.report_tool import run as report_run

    with_notes = AnalysisResult(findings=[_finding("合计 1 亿")],
                                quality_notes=["[join_amplified_used] 步骤 s3 放大 5×"])
    out = report_run({"analysis": with_notes.model_dump(), "objective": "营收分析"})
    assert "数据质量与限制" in out["report"] and "放大 5×" in out["report"]
    assert out["report"].startswith("# 数据分析报告：营收分析"), "不得破坏既有 H1 行"

    clean = report_run({"analysis": AnalysisResult().model_dump(), "objective": "营收分析"})
    assert "数据质量与限制" not in clean["report"], "没有问题就不出现该段"


# --------------------------------------------------------------------------- #
# 7. 编排：门禁 REPLAN 不得撞上"无进展检测"
# --------------------------------------------------------------------------- #
def test_gate_replan_does_not_trip_stall_detector(monkeypatch, mock_llm_env):
    """同目标连发 REPLAN 会被 graph 判空转 FAILED；门禁的 REPLAN 必须豁免。"""
    import app.core.agents.data_analyst.graph as g
    from app.core.agents.data_analyst.state import PlanModel

    calls = {"n": 0}

    def _plan(state):
        state.status = "PLAN"
        state.plan = PlanModel(goal="g", steps=[])
        state.current_step_index = 0
        return state

    def _refl(state):
        calls["n"] += 1
        state.reflection = ReflectionResult(decision="REPLAN", confidence=0.5,
                                            replan_objectives=["同一条缺口"])
        state.metadata["gate_issues"] = [{"code": "key_not_unique", "severity": "REPLAN"}]
        state.status = "REPLAN"
        state.replan_count += 1
        if calls["n"] >= 4:
            state.reflection = ReflectionResult(decision="PASS", confidence=0.9)
            state.status = "REPORT"
        return state

    monkeypatch.setattr(g, "run_planner", _plan)
    monkeypatch.setattr(g, "run_analyst", lambda s: s)
    monkeypatch.setattr(g, "run_reflection", _refl)
    monkeypatch.setattr(g, "run_reporter", lambda s: (setattr(s, "status", "FINISH"), s)[1])

    st = g._drive_sync(AgentState(session_id="stall_gate", user_query="q"))
    assert calls["n"] >= 4, "门禁 REPLAN 不应在第 2 轮就被无进展检测掐断"
    assert st.status == "FINISH"


# --------------------------------------------------------------------------- #
# 8. 对外契约：SSE 透出 + sync/stream 一致性
# --------------------------------------------------------------------------- #
def test_sse_event_carries_quality_issues(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.routes.chat as chat
    import app.main as main

    snap = AgentState(session_id="sse_q", user_query="q", status="FINISH")
    snap.metadata["gate_issues"] = [{"code": "date_sparse_claimed", "severity": "ANNOTATE",
                                     "detail": "日期稀疏"}]
    monkeypatch.setattr(chat, "stream_analysis", lambda *a, **kw: iter([snap]))

    with TestClient(main.app).stream("POST", "/api/v1/chat/analyze/stream",
                                     json={"query": "q"}) as r:
        text = "".join(r.iter_text())
    assert "date_sparse_claimed" in text


def test_analyze_response_schema_has_quality_issues():
    from app.models.schemas import AnalyzeResponse

    assert AnalyzeResponse(session_id="s", status="FINISH").quality_issues == []


# --------------------------------------------------------------------------- #
# 9. BLOCK 的对外可见性（全流程 status 仍是 FINISH，见 spec §1.1 如实说明）
# --------------------------------------------------------------------------- #
def test_block_is_visible_to_caller_end_to_end(monkeypatch, mock_llm_env):
    """BLOCK 必须三处可见：error / quality_issues / 报告披露段。"""
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.state import PlanModel, PlanStep

    # 只替换 reflection 阶段：其他阶段要保留 mock 行为，否则 analyst 不产 evidence、
    # BLOCK 会退化成本 ANNOTATE（本用例就测不到 BLOCK 了）
    real_llm = nodes._llm
    monkeypatch.setattr(nodes, "_llm",
                        lambda stage, user, json_mode=True:
                        _PASS_JSON if stage == "reflection" else real_llm(stage, user, json_mode))

    def _plan(state):
        state.status = "PLAN"
        state.plan = PlanModel(goal="g", steps=[
            PlanStep(id="s1", objective="探查", action="搜索", tool="schema_search"),
            PlanStep(id="s2", objective="取数", action="SQL", tool="freeform", dependencies=["s1"],
                     input={"sql": "SELECT f.sale_id, r.region_name FROM fact_sales f "
                                   "CROSS JOIN dim_region r"}),
        ])
        state.current_step_index = 0
        return state

    monkeypatch.setattr(g, "run_planner", _plan)
    st = g.run_analysis("gate_e2e_block", "统计各区域营收合计")

    codes = [i["code"] for i in st.metadata.get("gate_issues", [])]
    assert "join_amplified_used" in codes, codes
    assert any(i["severity"] == "BLOCK" for i in st.metadata["gate_issues"])
    assert "数据质量与限制" in (st.report or ""), "报告必须带披露段"
    assert "放大" in (st.report or "")
    # 既有行为：reporter 兜底会把失败态置成 FINISH —— 不声称 FAILED，但 error 必须留痕
    assert st.status == "FINISH"
    if st.metadata.get("gate_block"):
        assert st.error and "质量门禁" in st.error


def test_sync_stream_parity_on_stall(monkeypatch, mock_llm_env):
    """同一"无进展"场景下，同步与流式必须同样终态（此前流式无停滞检测）。"""
    import app.core.agents.data_analyst.graph as g
    from app.core.agents.data_analyst.state import PlanModel

    def _plan(state):
        state.status = "PLAN"
        state.plan = PlanModel(goal="g", steps=[])
        state.current_step_index = 0
        return state

    def _refl(state):
        state.reflection = ReflectionResult(decision="REPLAN", confidence=0.4,
                                            replan_objectives=["同一条缺口"])
        state.status = "REPLAN"
        state.replan_count += 1
        return state

    monkeypatch.setattr(g, "run_planner", _plan)
    monkeypatch.setattr(g, "run_analyst", lambda s: s)
    monkeypatch.setattr(g, "run_reflection", _refl)
    monkeypatch.setattr(g, "run_reporter", lambda s: (setattr(s, "status", "FINISH"), s)[1])

    sync_state = g.run_analysis("parity_sync", "q")
    snaps = list(g.stream_analysis("parity_stream", "q"))

    assert sync_state.status == snaps[-1].status, "两条路径终态必须一致"
    assert "无进展循环" in (sync_state.error or "")
    assert "无进展循环" in (snaps[-1].error or ""), "流式也必须做无进展检测，而不是空转到 safety 上限"


def test_gate_actually_ran(monkeypatch, mock_llm_env):
    """守卫：门禁在真实流水线里**必须真的跑过**。

    起因：`rigor.py` 曾因字符串转义写成语法错误 → `from .rigor import ...` 抛 ImportError
    → 被 `except Exception` 静默吞掉（只留个 `gate_error` 没人看）→ 门禁整条成了摆设。
    这条断言让"门禁静默失效"立刻可见。
    """
    import app.core.agents.data_analyst.graph as g

    st = g.run_analysis("gate_ran", "分析各区域营收")
    assert "gate_error" not in st.metadata, f"门禁未正常执行: {st.metadata.get('gate_error')}"
    assert "gate_issues" in st.metadata, "门禁没有留下任何结果（可能根本没跑）"
