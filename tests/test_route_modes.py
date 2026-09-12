"""ROUTE output-intent: detection + stage pruning (sql_only / quick_answer)."""
from __future__ import annotations

from app.core.agents.data_analyst.modes import QUICK, SQL_ONLY, detect_mode
from app.core.agents.data_analyst.state import ContextModel, PlanModel, PlanStep


def test_detect_mode_explicit_hints():
    assert detect_mode("只要SQL：给出各区域营收排序语句") == SQL_ONLY
    assert detect_mode("输出 SQL 就行") == SQL_ONLY
    assert detect_mode("简短回答：哪个区域最高") == QUICK


def test_detect_mode_falls_back_to_full():
    assert detect_mode("分析最近半年华北营收下滑的原因并给建议") == "full"
    ctx = ContextModel(objective="o", output_format="report")
    assert detect_mode("随便聊聊", ctx) == "full"


def test_detect_mode_respects_output_format():
    ctx = ContextModel(objective="o", output_format="sql")
    assert detect_mode("帮我查一下", ctx) == SQL_ONLY


def _freeform_step(sql: str) -> PlanStep:
    return PlanStep(id="r1", objective="取数", action="自由SQL", tool="freeform",
                    input={"sql": sql})


def _patch_planner(monkeypatch, step: PlanStep):
    import app.core.agents.data_analyst.graph as g

    def _plan(state):
        state.status = "PLAN"
        state.plan = PlanModel(goal="g", steps=[step])
        state.current_step_index = 0
        return state

    monkeypatch.setattr(g, "run_planner", _plan)


def test_sql_only_end_to_end(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    sql = ("SELECT r.region_name, SUM(f.revenue) r FROM fact_sales f "
           "JOIN dim_region r ON f.region_id=r.region_id GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    _patch_planner(monkeypatch, _freeform_step(sql))
    try:
        state = run_analysis("rt_sql", "只要SQL：给出现有各区域营收排序的语句")
        assert state.mode == "sql_only"
        assert state.status == "FINISH"
        assert "```sql" in (state.report or "")
        assert sql in (state.report or "")
        assert "Executive Summary" not in (state.report or ""), "不应走重报告链"
        tools = {r.tool for r in state.tool_results}
        assert "freeform" in tools
    finally:
        get_settings.cache_clear(); reset_llm()


def test_quick_answer_end_to_end(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    _patch_planner(monkeypatch, _freeform_step(
        "SELECT region_id, SUM(revenue) r FROM fact_sales GROUP BY 1 ORDER BY 2 DESC LIMIT 3"))
    try:
        state = run_analysis("rt_qa", "简短回答：各区域营收哪个最高？")
        assert state.mode == "quick_answer"
        assert "快速回答" in (state.report or "")
        assert "Executive Summary" not in (state.report or "")
    finally:
        get_settings.cache_clear(); reset_llm()


def test_classify_task_examples():
    from app.core.agents.data_analyst.modes import classify_task
    assert classify_task("帮我写一个统计复购率的 SQL") == "sql"
    assert classify_task("帮我查一下近30天GMV，给SQL") == "sql"
    assert classify_task("为什么最近用户流失增加？") == "business_analysis"
    assert classify_task("帮我做销售周报") == "markdown_report"
    assert classify_task("帮我写个 Python 脚本分析 CSV") == "python"
    assert classify_task("帮我探索一下这个数据集") == "data_exploration"
    assert classify_task("复购率怎么定义？") == "metric_definition"
    assert classify_task("帮我解释这个 SQL 结果") == "data_interpretation"


def test_build_plan_shape_and_requires():
    from app.core.agents.data_analyst.modes import build_plan
    p = build_plan("为什么本月收入下降，给我 SQL 和 Python")
    assert p["task_type"] == "business_analysis"
    assert p["requires_data"] is True and p["requires_report"] is True
    assert {"sql", "python", "markdown"}.issubset(p["deliverable"])
    assert p["workflow"]
    ps = build_plan("只给 SQL")
    assert ps["task_type"] == "sql" and ps["deliverable"] == ["sql"]


def test_intent_set_on_run(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    _patch_planner(monkeypatch, _freeform_step("SELECT 1"))
    try:
        state = run_analysis("rt_intent", "只要SQL：统计各区域营收")
        assert state.intent and state.intent["task_type"] == "sql"
        assert "sql" in state.intent["deliverable"]
    finally:
        get_settings.cache_clear(); reset_llm()


def test_python_code_end_to_end(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    try:
        state = run_analysis("rt_py", "帮我写个 Python 脚本清洗一份 CSV")
        assert state.mode == "python_code"
        assert state.status == "FINISH"
        assert "```python" in (state.report or "")
        assert "Executive Summary" not in (state.report or ""), "不应走重报告链"
    finally:
        get_settings.cache_clear(); reset_llm()


def test_markdown_report_mode_label(monkeypatch, tmp_path):
    """markdown 交付：intent=markdown_report；执行仍走报告链但计划可见。"""
    from app.config import get_settings
    from app.core.agents.data_analyst.graph import run_analysis
    from app.infrastructure.llm.router import reset_llm

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear(); reset_llm()
    try:
        state = run_analysis("rt_md", "帮我做一份销售周报")
        assert state.intent and state.intent["task_type"] == "markdown_report"
        assert "markdown" in state.intent["deliverable"]
        assert state.status == "FINISH"
    finally:
        get_settings.cache_clear(); reset_llm()


def test_stream_emits_intent_and_pruned_output(monkeypatch, tmp_path):
    import json as _json
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm
    from app.main import app

    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear(); reset_llm()
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/chat/analyze/stream",
                       json={"query": "只要SQL：统计各区域营收排序", "session_id": "st_route"})
        evs = [_json.loads(ln[6:]) for ln in r.text.splitlines()
               if ln.startswith("data: ") and ln[6:] != "[DONE]"]
        intent = next((e["intent"] for e in evs if e.get("intent")), None)
        assert intent and intent["task_type"] == "sql"
        fin = [e for e in evs if e["status"] == "FINISH"][-1]
        assert "```sql" in (fin.get("report") or "")
        statuses = [e["status"] for e in evs]
        assert "REPORT" not in statuses and "REFLECT" not in statuses, "应裁剪重链(报告/质检)"
    finally:
        get_settings.cache_clear(); reset_llm()


def test_workflow_progress_is_precise_via_sse(monkeypatch, tmp_path):
    import json as _json
    from fastapi.testclient import TestClient
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm
    from app.main import app

    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear(); reset_llm()
    try:
        with TestClient(app) as c:
            r = c.post("/api/v1/chat/analyze/stream",
                       json={"query": "只要SQL：统计各区域营收排序", "session_id": "st_prog"})
        evs = [_json.loads(l[6:]) for l in r.text.splitlines()
               if l.startswith("data: ") and l[6:] != "[DONE]"]
        have = [e["workflow_progress"] for e in evs if e.get("workflow_progress")]
        assert have, "事件应携带 workflow_progress"
        total = have[-1]["total"]
        assert total == 3, "sql 任务 workflow=3 步"
        # 进度不倒退
        assert all(have[i]["done"] <= have[i + 1]["done"] for i in range(len(have) - 1))
        fin = [e for e in evs if e["status"] == "FINISH"][-1]
        assert fin["workflow_progress"]["done"] == total, "FINISH 应全绿"
    finally:
        get_settings.cache_clear(); reset_llm()
