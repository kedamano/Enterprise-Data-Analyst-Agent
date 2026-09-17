"""D41：幻觉率监控 + **离线-在线一致性**。

"疑似幻觉"的定义（沿用 E1）
--------------------------
报告/发现里的**数值结论若无源可溯**（拿不到对应 SQL 步骤），即计为疑似编造的数字。

真问题：**"已溯源"此前有两个定义**
---------------------------------
| 位置 | 判据 |
|---|---|
| `trace_counts`（**离线** eval 用） | `sql_id` 能 resolve 到**真实 SQL step** |
| `trace_manifest`（**在线** `/trace` API + SSE FINISH 用） | `sql_id` 非空 **且 `sql_of_step` 有返回** |

两个定义各算各的、**没有任何测试比对**——离线说覆盖率 0.9、在线说 0.6 也没人知道。
本日引入**唯一口径** `trace_coverage()`，两边都走它，并用测试钉死"同 state 同结果"。

一条同样重要的语义
------------------
**零数值 claim 时，覆盖率与幻觉率都是 `None`（未定义），不是 0。**
把"没测到"报成"0 幻觉"是最典型的自欺——D38 的 `溯源 0/0` 就是这样全程判过的。
"""
from __future__ import annotations

from app.core.agents.data_analyst.sources import trace_coverage, trace_manifest
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    Evidence,
    Finding,
    ToolResult,
)
from app.eval.runner import hallucination_rate


def _sql_step(step_id: str = "s1") -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status="SUCCESS",
                      input={"sql": "SELECT region_id, SUM(revenue) FROM fact_sales GROUP BY region_id"},
                      output={"ok": True, "row_count": 5})


def _finding(text: str, *, value, sql_id) -> Finding:
    return Finding(finding=text, confidence=0.8,
                   evidence=[Evidence(source="sql_query", metric="revenue",
                                      value=value, sql_id=sql_id)])


def _state(*findings: Finding, results: list[ToolResult] | None = None) -> AgentState:
    s = AgentState(session_id="hal", user_query="分析各区域营收")
    s.context = ContextModel(objective="分析各区域营收")
    s.analysis = AnalysisResult(findings=list(findings))
    s.tool_results = results if results is not None else [_sql_step()]
    return s


# --------------------------------------------------------------------------- #
# 一、零 claim ≠ 零幻觉（语义边界）
# --------------------------------------------------------------------------- #
def test_no_numeric_claims_means_undefined_not_zero():
    cov = trace_coverage([], [])
    assert cov["numeric_claims"] == 0
    assert cov["rate"] is None
    assert cov["hallucination_rate"] is None, "没测到 ≠ 没幻觉"


def test_non_numeric_findings_do_not_count():
    """纯文字结论不参与统计（否则分母被稀释，覆盖率永远好看）。"""
    f = Finding(finding="数据不支持该结论", confidence=0.5, evidence=[])
    cov = trace_coverage([f], [_sql_step()])
    assert cov["numeric_claims"] == 0 and cov["rate"] is None


# --------------------------------------------------------------------------- #
# 二、正/负样例
# --------------------------------------------------------------------------- #
def test_fully_traced_is_zero_hallucination():
    state = _state(_finding("华东营收 1.2 亿", value=120000000, sql_id="s1"))
    cov = trace_coverage(state.analysis.findings, state.tool_results)
    assert cov == {"numeric_claims": 1, "traced_claims": 1,
                   "rate": 1.0, "hallucination_rate": 0.0}


def test_fabricated_number_is_flagged():
    """**负样例**：一个数字没有 sql_id（凭空写的）→ 幻觉率 > 0。"""
    state = _state(_finding("华东营收 1.2 亿", value=120000000, sql_id="s1"),
                   _finding("华北营收 9.9 亿", value=990000000, sql_id=None))
    cov = trace_coverage(state.analysis.findings, state.tool_results)
    assert cov["numeric_claims"] == 2 and cov["traced_claims"] == 1
    assert cov["hallucination_rate"] == 0.5


def test_unresolvable_sql_id_counts_as_untraced():
    """sql_id 指向**不存在的步骤**——等于编了个来源，照样算未溯源。"""
    state = _state(_finding("华东营收 1.2 亿", value=120000000, sql_id="s999"))
    cov = trace_coverage(state.analysis.findings, state.tool_results)
    assert cov["traced_claims"] == 0 and cov["hallucination_rate"] == 1.0


# --------------------------------------------------------------------------- #
# 三、离线-在线**同一口径**（本日的核心）
# --------------------------------------------------------------------------- #
def test_manifest_coverage_comes_from_the_same_measurement():
    """在线 `/trace` 的 coverage 必须**逐字等于**离线口径的结果。"""
    state = _state(_finding("华东营收 1.2 亿", value=120000000, sql_id="s1"),
                   _finding("华北营收 9.9 亿", value=990000000, sql_id=None))
    offline = trace_coverage(state.analysis.findings, state.tool_results)
    online = trace_manifest(state)["coverage"]
    for key in ("numeric_claims", "traced_claims", "rate", "hallucination_rate"):
        assert online[key] == offline[key], f"{key} 离线/在线不一致"


def test_manifest_still_carries_claims_detail():
    """统一口径不得顺手把可视化明细弄丢。"""
    state = _state(_finding("华东营收 1.2 亿", value=120000000, sql_id="s1"))
    man = trace_manifest(state)
    assert man["claims"] and man["claims"][0]["evidence"][0]["sql"]


# --------------------------------------------------------------------------- #
# 四、指标换算
# --------------------------------------------------------------------------- #
def test_hallucination_rate_helper():
    assert hallucination_rate(0, 0) is None
    assert hallucination_rate(4, 4) == 0.0
    assert hallucination_rate(4, 3) == 0.25
    assert hallucination_rate(4, 0) == 1.0


def test_runner_report_exposes_hallucination_rate():
    """eval 报告必须带 `hallucination_rate`（与 `traceability_rate` 同源）。"""
    import inspect

    import app.eval.runner as runner

    src = inspect.getsource(runner.evaluate)
    assert '"hallucination_rate"' in src
    assert "trace_coverage" in src or "hallucination_rate" in src


# --------------------------------------------------------------------------- #
# 五、在线监控：/metrics 计数器
# --------------------------------------------------------------------------- #
def test_runtime_counters_are_recorded():
    from app.infrastructure.observability import metrics as m

    before_t = m.metrics.snapshot()["counters"].get("trace_numeric_claims_total", 0)
    before_u = m.metrics.snapshot()["counters"].get("trace_untraced_claims_total", 0)
    m.record_trace_coverage(4, 3)
    assert m.metrics.snapshot()["counters"]["trace_numeric_claims_total"] == before_t + 4
    assert m.metrics.snapshot()["counters"]["trace_untraced_claims_total"] == before_u + 1


def test_zero_claims_records_nothing():
    """零 claim 不记 0 —— 那会造成"系统很干净"的错觉（同 eval：None 而非 0）。"""
    from app.infrastructure.observability import metrics as m

    before = m.metrics.snapshot()["counters"].get("trace_numeric_claims_total", 0)
    m.record_trace_coverage(0, 0)
    assert m.metrics.snapshot()["counters"].get("trace_numeric_claims_total", 0) == before


def test_runtime_wiring_exists_in_reporter():
    """**源码级**钉住接线：报告终点必须计入覆盖，否则在线监控会静默失效。

    隔离：上游测试可能用 ``monkeypatch.setattr(pipeline, "run_reporter", ...)``，
    但 pytest fixture undo 在极端顺序（如 13 h 全量串行跑）下偶有漏网。
    这里强制 reload nodes 拿到最原始源，与运行态解耦——本测试只看"源码里有没有"，
    不看运行态挂的是哪个实现。
    """
    import importlib
    import inspect

    import app.core.agents.data_analyst.nodes as nodes

    importlib.reload(nodes)
    src = inspect.getsource(nodes.run_reporter)
    assert "record_trace_coverage" in src
    assert "trace_coverage" in src
