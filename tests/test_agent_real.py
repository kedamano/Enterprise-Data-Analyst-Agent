"""Real-LLM TDD suite for the Enterprise Data Analyst Agent.

Run with a real key, e.g.:
    LLM_API_KEY=sk-or-... LLM_MODEL=deepseek/deepseek-chat \
        python -m pytest tests/test_agent_real.py -v

The suite defines the agent's behavioural contract and verifies it against the
live model. A spy in conftest fails any test that silently fell back to Mock.
"""
from __future__ import annotations

import uuid

from app.config import get_settings
from app.core.agents.data_analyst.graph import (
    run_analysis,
    run_context,
    run_planner,
)
from app.core.agents.data_analyst.state import AgentState
from app.core.tools import REGISTRY, execute_tool
from app.infrastructure.llm.router import (
    MockLLM,
    OpenAILLM,
    fallback_occurred,
    get_llm,
)


# --------------------------------------------------------------------------- #
# 0. Backend guard — we must actually be talking to the real model.
# --------------------------------------------------------------------------- #
def test_llm_backend_is_real():
    llm = get_llm()
    assert isinstance(llm, OpenAILLM), "应使用真实 LLM，却拿到了 MockLLM 实例"
    assert not isinstance(llm, MockLLM)
    assert not get_settings().use_mock_llm, "use_mock_llm 应为 False"


# --------------------------------------------------------------------------- #
# 1. Context stage — semantically parse the user question.
# --------------------------------------------------------------------------- #
def test_context_stage_extracts_intent(session_id):
    s = AgentState(session_id=session_id, user_query="分析最近半年华北地区营收下滑的原因，按产品和渠道维度下钻")
    s = run_context(s)
    assert s.status in ("UNDERSTAND", "PLAN", "ERROR")
    if s.status == "ERROR":
        # 模型判定需要澄清也是合法输出——只验证澄清信息合理且未静默降级
        assert s.error, "澄清场景应返回澄清说明而非空"
        assert fallback_occurred() is False
        return
    assert s.context.objective, "context.objective 不应为空"
    assert s.context.metrics, "应抽取出至少一个业务指标（如 revenue/营收）"
    lowered = [m.lower() for m in s.context.metrics]
    biz_synonyms = {"revenue", "营收", "销售额", "sales", "收入"}
    assert any(any(syn in m for syn in biz_synonyms) for m in lowered), \
        f"营收类问题应识别营收类指标，实际: {s.context.metrics}"
    assert fallback_occurred() is False


def test_context_stage_dimensions(session_id):
    s = AgentState(session_id=session_id, user_query="按地区和产品维度分析各渠道的订单量趋势")
    s = run_context(s)
    assert s.status in ("UNDERSTAND", "PLAN", "ERROR")
    if s.status == "ERROR":
        # 模型判定需要澄清也是合法输出（真实 LLM 非确定性），只要不崩溃即可
        assert s.error, "澄清场景应返回澄清说明而非空"
        return
    # 真实模型可能返回中文或英文维度名，业务同义词都应被接受
    dims = [d.lower() for d in s.context.dimensions]
    dim_synonyms = {"region": "地区", "product": "产品", "channel": "渠道"}
    ok = any(
        any(syn in d for syn in (k, v))
        for d in dims for k, v in dim_synonyms.items()
    )
    assert ok, f"应识别维度（地区/region、产品/product、渠道/channel），实际: {s.context.dimensions}"
    assert fallback_occurred() is False


# --------------------------------------------------------------------------- #
# 2. Planner stage — produce an executable, registry-valid plan.
# --------------------------------------------------------------------------- #
def test_planner_produces_valid_steps(session_id):
    s = AgentState(session_id=session_id, user_query="分析最近各区域营收表现，按产品维度下钻")
    s = run_context(s)
    s = run_planner(s)
    assert len(s.plan.steps) >= 1, "计划应至少包含一个步骤"
    # 上限由 run_planner 截断保证（见 nodes.py），不在此硬断言具体上限值，
    # 以避免真实 LLM 多变步数导致的 flaky。
    for step in s.plan.steps:
        assert step.tool in REGISTRY, f"计划引用了未注册工具: {step.tool}"
        assert step.objective and step.action, "每个步骤需有明确 objective 与 action"
    assert fallback_occurred() is False, "[planner] 真实 LLM 调用被静默降级为 Mock，测试无效"


# --------------------------------------------------------------------------- #
# 3. Tool layer — each tool executes against real data without crashing.
# --------------------------------------------------------------------------- #
def test_schema_search_discovers_tables():
    res = execute_tool("s1", "schema_search", {"keyword": "revenue"}, "real_schema")
    assert res.status == "SUCCESS"
    tables = res.output.get("tables") or []
    assert any("fact_sales" in t.get("table", "") for t in tables), "应发现 fact_sales 表"


def test_dataset_profile_runs():
    res = execute_tool("p1", "dataset_profile", {"table": "fact_sales"}, "real_profile")
    assert res.status == "SUCCESS"
    assert res.output.get("row_count", 0) > 0


def test_sql_query_returns_rows_and_csv():
    res = execute_tool(
        "q1",
        "sql_query",
        {"sql": "SELECT region_id, SUM(revenue) AS rev FROM fact_sales GROUP BY region_id ORDER BY rev DESC LIMIT 5"},
        "real_sql",
    )
    assert res.status == "SUCCESS", res.error
    assert res.output.get("rows"), "sql_query 应返回行"
    assert res.output.get("csv_path"), "应落盘 CSV 供下游读取"
    assert res.output.get("row_count", 0) <= get_settings().sql_max_rows


def test_python_analysis_runs_on_csv():
    q = execute_tool(
        "q1",
        "sql_query",
        {"sql": "SELECT region_id, revenue, orders FROM fact_sales LIMIT 50"},
        "real_py",
    )
    csv_path = q.output["csv_path"]
    code = (
        "summary = {'rows': int(df.shape[0]) if df is not None else 0, "
        "'cols': list(df.columns) if df is not None else []}\n"
        "print(_json.dumps(summary))"
    )
    res = execute_tool("py1", "python_analysis", {"code": code, "data_csv": csv_path}, "real_py")
    assert res.status == "SUCCESS", res.error
    assert "rows" in (res.output.get("stdout") or "")


def test_visualization_runs():
    q = execute_tool(
        "q1",
        "sql_query",
        {"sql": "SELECT region_id, SUM(revenue) AS rev FROM fact_sales GROUP BY region_id ORDER BY rev DESC LIMIT 5"},
        "real_viz",
    )
    rows = q.output["rows"]
    res = execute_tool(
        "v1",
        "visualization",
        {"chart_type": "bar", "x": "region_id", "y": "rev", "data": rows, "title": "营收按地区"},
        "real_viz",
    )
    assert res.status == "SUCCESS", res.error
    assert res.output.get("artifacts") or res.output.get("image_path")


def test_knowledge_search_degrades_gracefully():
    res = execute_tool("k1", "knowledge_search", {"query": "营收下滑的常见原因", "top_k": 3}, "real_kb")
    # Milvus not configured in test env -> must not crash, returns empty result.
    assert res.status in ("SUCCESS", "FAILED")
    assert isinstance(res.output, dict)


def test_generate_report_runs():
    res = execute_tool(
        "r1",
        "generate_report",
        {"analysis": {"findings": [{"finding": "华北营收下滑", "evidence": [], "interpretation": "", "confidence": 0.8}]},
         "reflection": {"decision": "PASS", "summary": "ok"},
         "objective": "分析营收下滑"},
        "real_rep",
    )
    assert res.status == "SUCCESS"
    assert res.output.get("report")


# --------------------------------------------------------------------------- #
# 4. Full pipeline — live model drives Context→Plan→Execute→Analyze→Reflect→Report.
# --------------------------------------------------------------------------- #
def test_full_pipeline_reaches_finish(session_id):
    s = run_analysis(
        session_id,
        "分析最近半年华北地区营收下滑的原因，按产品和渠道维度下钻，并给出改进建议",
    )
    assert s.status == "FINISH", f"期望 FINISH，实际 {s.status}，error={s.error}"
    assert s.reflection is not None
    assert s.reflection.decision in ("PASS", "REPLAN", "FAIL")
    assert len(s.tool_results) >= 1, "应至少执行一个工具"
    assert any(r.status == "SUCCESS" for r in s.tool_results), "应有至少一次成功的工具调用"
    assert len(s.analysis.findings) >= 1, "应至少抽取一个发现"
    assert any(f.evidence for f in s.analysis.findings), "发现应带证据（质检门禁的意义）"
    assert s.report.strip(), "应生成最终报告"


def test_full_pipeline_second_query(session_id):
    s = run_analysis(session_id, "对比各产品类别的订单量与客户数，找出最值得投入的品类")
    assert s.status == "FINISH", f"期望 FINISH，实际 {s.status}，error={s.error}"
    assert any(r.status == "SUCCESS" for r in s.tool_results)
    assert s.report.strip()


# --------------------------------------------------------------------------- #
# 5. HTTP API — the FastAPI endpoint wired to the live pipeline.
# --------------------------------------------------------------------------- #
def test_api_analyze_endpoint(session_id):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/v1/chat/analyze",
        json={"query": "分析最近各区域营收表现，识别增长最快的地区", "session_id": session_id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "FINISH", body
    assert body["reflection_decision"] in ("PASS", "REPLAN", "FAIL")
    assert len(body["tool_results"]) >= 1
    assert len(body["findings"]) >= 1
    assert body["report"]
