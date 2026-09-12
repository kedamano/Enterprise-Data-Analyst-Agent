"""P0-4 降级链路可见化：分类 / 聚合 / 渲染。

这批测试锁定的行为：

1. 403 region 限制能被识别成 ``region_blocked``（案例一的真实错误）
2. 各种常见错误码归类正确，且**拿不准时归 unknown 并保留原文**（不吞信息）
3. 阶段聚合去重、影响范围按阶段给出人话解释
4. 渲染出的 Markdown 区块含「阶段 + 原因 + 影响」，无降级时为空
"""

from __future__ import annotations

from app.infrastructure.llm.degradation import (
    classify_error,
    degradation_footer,
    render_degradation_block,
    summarize_degradation,
)

# 本次 sleep.csv 复现中的真实错误串（截断版）
REGION_ERR = (
    "Error code: 403 - {'error': {'message': 'This model is not available in your "
    "region.', 'code': 403, 'metadata': {'routing_funnel': [{'step': 'Initial "
    "Endpoints', 'endpoint_count': 3}]}}}"
)


def _events(*pairs):
    return [{"stage": s, "error": e, "run_id": "r1", "ts": "2026-09-10T00:00:00Z"} for s, e in pairs]


# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
def test_classify_region_blocked():
    c = classify_error(REGION_ERR)
    assert c["kind"] == "region_blocked"
    assert c["code"] == "403"
    assert "区域" in c["summary"]


def test_classify_auth_failed():
    assert classify_error("Error code: 401 - Incorrect API key provided")["kind"] == "auth_failed"


def test_classify_quota_exhausted():
    assert classify_error("Error code: 402 - Insufficient quota")["kind"] == "quota_exhausted"


def test_classify_rate_limited():
    assert classify_error("Error code: 429 - rate limit exceeded")["kind"] == "rate_limited"


def test_classify_timeout():
    assert classify_error("ReadTimeout: HTTPSConnectionPool read timed out")["kind"] == "timeout"


def test_classify_connection():
    assert classify_error("Connection refused by proxy at 127.0.0.1:7890")["kind"] == "connection"


def test_classify_unknown_keeps_raw():
    c = classify_error("something utterly unparseable happened")
    assert c["kind"] == "unknown"
    assert c["raw"] == "something utterly unparseable happened"  # 不吞信息
    assert c["action"]  # 仍给出兜底动作


def test_classify_handles_none():
    c = classify_error(None)
    assert c["kind"] == "unknown"
    assert c["raw"] == ""


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def test_summarize_empty_is_not_degraded():
    s = summarize_degradation([])
    assert s["degraded"] is False
    assert s["severity"] == "none"
    assert render_degradation_block(s) == ""


def test_summarize_dedupes_stages_and_counts_reasons():
    s = summarize_degradation(_events(
        ("context", REGION_ERR),
        ("planner", REGION_ERR),
        ("analyst", REGION_ERR),
        ("reflection", REGION_ERR),
        ("reporter", REGION_ERR),
    ))
    assert s["degraded"] is True
    assert s["count"] == 5
    assert len(s["stages"]) == 5            # 5 个不同阶段
    assert len(s["reasons"]) == 1           # 同一个原因只留一条
    assert s["reasons"][0]["count"] == 5
    assert s["severity"] == "total"


def test_summarize_partial_severity():
    s = summarize_degradation(_events(("reporter", REGION_ERR)))
    assert s["severity"] == "partial"
    assert len(s["stages"]) == 1


def test_summarize_impacts_are_human_readable():
    s = summarize_degradation(_events(("analyst", REGION_ERR), ("reporter", REGION_ERR)))
    impacts = " ".join(s["impacts"])
    assert "统计推断" in impacts or "因果" in impacts
    assert "模板" in impacts or "洞察" in impacts


def test_summarize_same_stage_twice_counts_once():
    """同阶段重试降级多次 —— 阶段只列一次，原因计数累加。"""
    s = summarize_degradation(_events(("planner", REGION_ERR), ("planner", REGION_ERR)))
    assert len(s["stages"]) == 1
    assert s["count"] == 2


def test_summarize_unknown_stage_gets_placeholder_label():
    s = summarize_degradation(_events(("weird_stage_xyz", "boom")))
    assert s["stages"][0]["label"] == "未知阶段"


def test_summarize_matches_stage_variants():
    """阶段名带后缀（planner_v2）也能认出来。"""
    s = summarize_degradation(_events(("planner_v2", REGION_ERR)))
    assert s["stages"][0]["label"] == "计划制定"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def test_render_block_lists_stages_reasons_impacts():
    s = summarize_degradation(_events(("analyst", REGION_ERR), ("reporter", REGION_ERR)))
    md = render_degradation_block(s)
    assert "运行健康度" in md
    assert "证据分析" in md
    assert "报告撰写" in md
    assert "403" in md
    assert "对本次结论的影响" in md


def test_render_block_names_the_region_problem():
    """案例一的真实场景：必须能看出是区域限制，而不是笼统的"降级"。"""
    s = summarize_degradation(_events(("planner", REGION_ERR)))
    md = render_degradation_block(s)
    assert "区域" in md
    assert "更换可用区域" in md


def test_degradation_footer_is_one_line():
    s = summarize_degradation(_events(("analyst", REGION_ERR)))
    footer = degradation_footer(s)
    assert "\n" not in footer
    assert "证据分析" in footer


def test_degradation_footer_empty_when_healthy():
    assert degradation_footer(summarize_degradation([])) == ""


# --------------------------------------------------------------------------- #
# 集成：区块真的进了报告
#
# 这里刻意**不**依赖 state.metadata["llm_fallbacks"]——因为 graph 里的
# _attach_llm_fallbacks 是在 run_reporter **之后**才调用的。
# 如果实现只用 metadata，报告里就永远不会有这个区块（隐形的时序 bug）。
# --------------------------------------------------------------------------- #
REGION_ERR_FULL = (
    "Error code: 403 - {'error': {'message': 'This model is not available in your "
    "region.', 'code': 403}}"
)


def test_report_is_prefixed_with_degradation_block(monkeypatch):
    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst.nodes import _prepend_degradation_block

    st = AgentState()
    st.session_id = "degrade-test-session"
    st.metadata = {}
    # 模拟：本轮发生 2 个阶段降级
    monkeypatch.setattr(
        "app.infrastructure.llm.router.fallback_events",
        lambda run_id=None: [
            {"stage": "analyst", "error": REGION_ERR_FULL, "run_id": run_id},
            {"stage": "reporter", "error": REGION_ERR_FULL, "run_id": run_id},
        ],
    )

    report = "# 数据分析报告：这份文件反应了什么数据规律\n\n## Key Findings\n\n- 发现 1"
    out = _prepend_degradation_block(st, report)

    assert "运行健康度" in out
    assert "证据分析" in out          # analyst 的人话名
    assert "报告撰写" in out          # reporter 的人话名
    assert "区域" in out              # 原因说清楚了
    # 标题保持在最上方，区块紧随其后
    assert out.startswith("# 数据分析报告：")
    assert out.index("运行健康度") < out.index("Key Findings")
    # 摘要回写 metadata
    assert st.metadata["degradation_summary"]["degraded"] is True


def test_report_untouched_when_no_degradation(monkeypatch):
    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst.nodes import _prepend_degradation_block

    st = AgentState()
    st.session_id = "healthy-session"
    st.metadata = {}
    monkeypatch.setattr(
        "app.infrastructure.llm.router.fallback_events", lambda run_id=None: []
    )
    report = "# 数据分析报告：x\n\n## Key Findings\n\n- 发现 1"
    assert _prepend_degradation_block(st, report) == report


def test_report_block_survives_missing_fallback_events(monkeypatch):
    """取事件失败时不能让整条报告链崩掉（best-effort）。"""
    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst.nodes import _prepend_degradation_block

    def boom(run_id=None):
        raise RuntimeError("router unavailable")

    st = AgentState()
    st.session_id = "boom-session"
    st.metadata = {}
    monkeypatch.setattr("app.infrastructure.llm.router.fallback_events", boom)
    report = "# 报告\n\n正文"
    assert _prepend_degradation_block(st, report) == report
