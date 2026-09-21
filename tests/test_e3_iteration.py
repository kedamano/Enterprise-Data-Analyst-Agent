"""E3 session-dataset iteration: act on previous result instead of full re-run."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.iteration import (
    classify_followup,
    guard_iteration,
    is_followup,
    iteration_plan,
    load_last_dataset,
    save_last_dataset,
)
from app.core.agents.data_analyst.state import AgentState, ContextModel, PlanModel, PlanStep
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mock_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    # 清缓存：响应缓存（_MEM）+ 语义缓存（SQLite）都是跨测试持久化的，
    # 不清理会因命中旧 session 的缓存增量结果导致后续测试误判为 iteration。
    from app.core.agents.data_analyst.response_cache import clear as _clear_resp
    from app.core.agents.data_analyst.semantic_cache import _reset_connection, clear_semantic

    _clear_resp()
    clear_semantic(None)
    _reset_connection()
    get_settings.cache_clear()
    reset_llm()
    yield
    _clear_resp()
    clear_semantic(None)
    _reset_connection()
    get_settings.cache_clear()
    reset_llm()


def test_is_followup_matrix():
    assert is_followup("基于上一结果，改为只看 region 1")
    assert is_followup("在上一结果基础上按渠道下钻到华东")
    assert not is_followup("为什么最近营收下降")            # 普通问题
    assert not is_followup("重新完整分析一遍各区域营收")      # FORCE 否定
    assert not is_followup("基于上一结果，重新完整分析")      # FORCE 优先


def test_save_and_load_dataset(mock_env):
    from app.core.agents.data_analyst.state import ToolResult

    st = AgentState(session_id="e3_ds", user_query="q")
    st.tool_results = [ToolResult(step_id="s1", tool="sql_query", status="SUCCESS",
                                  input={"sql": "SELECT 1"},
                                  output={"csv_path": "data/x.csv", "rows": [{"a": 1}]})]
    ds = save_last_dataset(st)
    assert ds and ds["columns"] == ["a"] and ds["csv"].endswith(".csv")
    assert load_last_dataset("e3_ds")["step_id"] == "s1"


def _patch_planner(monkeypatch, sql: str, spy: dict):
    import app.core.agents.data_analyst.graph as g

    def _plan(state):
        spy["n"] = spy.get("n", 0) + 1
        state.status = "PLAN"
        state.plan = PlanModel(goal="g", steps=[
            PlanStep(id="s1", objective="取数", action="自由SQL", tool="freeform",
                     input={"sql": sql})])
        state.current_step_index = 0
        return state

    monkeypatch.setattr(g, "run_planner", _plan)


def test_iteration_skips_full_chain(mock_env, monkeypatch):
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_iter"

    s1 = run_analysis(sid, "看看各区域营收")   # 第一轮：产 CSV 并落数据集
    assert s1.status == "FINISH"
    assert load_last_dataset(sid), "第一轮应落会话数据集"

    s2 = run_analysis(sid, "基于上一结果，改为只看 region 1 的营收")  # 第二轮：增量
    assert s2.mode == "iteration", s2.mode
    assert s2.status == "FINISH"
    assert "增量分析" in (s2.report or "")
    tools2 = {r.tool for r in s2.tool_results}
    assert "schema_search" not in tools2, "增量不应重跑发现链"
    assert "python_analysis" in tools2


def test_force_full_rerun_uses_planner(mock_env, monkeypatch):
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_force"
    run_analysis(sid, "看看各区域营收")
    before = spy.get("n", 0)
    s = run_analysis(sid, "基于上一结果，重新完整分析一遍各区域营收")
    assert s.mode != "iteration"
    assert spy.get("n", 0) > before, "FORCE 应回到全链（调用 planner）"


# --------------------------------------------------------------------------- #
# D15：增量意图分类 → 只执行目标阶段 + force_full_rerun 开关
# --------------------------------------------------------------------------- #
def test_classify_followup_matrix():
    assert classify_followup("基于上一结果，下钻到城市看营收") == "drilldown"
    assert classify_followup("在上一结果基础上按渠道钻取") == "drilldown"
    assert classify_followup("基于上一结果，改为按月看营收") == "granularity"
    assert classify_followup("基于上一结果，汇总到季度粒度") == "granularity"
    assert classify_followup("基于上一结果，改为 2024年3月 的营收") == "date_change"
    assert classify_followup("基于上一结果，换成最近30天") == "date_change"
    assert classify_followup("基于上一结果，只看 region 1 的营收") == "filter"
    assert classify_followup("基于上一结果，排除掉测试渠道") == "filter"
    assert classify_followup("基于上一结果，再说说你的看法") == "generic"
    # 优先级：期间证据 > 粒度 > 下钻 > 筛选
    assert classify_followup("基于上一结果，改为按月的下钻") == "granularity"
    assert classify_followup("基于上一结果，下钻后只看华东") == "drilldown"


def test_iteration_plan_skips_discovery_chain():
    p = iteration_plan("drilldown")
    assert p["kind"] == "drilldown"
    assert p["stages"], "目标阶段不能为空"
    for k in ("planner", "schema_search", "sql_query"):
        assert k in p["skips"], f"发现链 {k} 应被跳过"
    assert iteration_plan("generic")["stages"]


def test_guard_date_change_out_of_dataset_range():
    ds = {"csv": "x.csv", "columns": ["sale_date", "revenue"],
          "date_col": "sale_date", "date_min": "2024-01-01", "date_max": "2024-12-23"}
    ok, guard = guard_iteration(ds, "基于上一结果，改为 2024年3月 的营收", "date_change")
    assert ok and guard["reason"] is None
    bad, guard2 = guard_iteration(ds, "基于上一结果，改为 2025年1月 的营收", "date_change")
    assert not bad and guard2["reason"]


def test_guard_granularity_needs_date_column():
    ds = {"csv": "x.csv", "columns": ["region_id", "revenue"]}
    ok, guard = guard_iteration(ds, "基于上一结果，改为按月看营收", "granularity")
    assert not ok and "日期" in (guard["reason"] or "")
    ok2, _ = guard_iteration(ds, "基于上一结果，只看 region 1", "filter")
    assert ok2, "筛选类不需要守卫"


def test_drilldown_runs_only_target_stage(mock_env, monkeypatch):
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT sale_date, region_id, revenue FROM fact_sales LIMIT 60", spy)
    sid = "e3_drill"
    run_analysis(sid, "看看各区域营收")

    s2 = run_analysis(sid, "基于上一结果，下钻到区域看营收")
    assert s2.mode == "iteration", s2.mode
    assert (s2.iteration or {}).get("kind") == "drilldown", s2.iteration
    assert "planner" in (s2.iteration or {}).get("skips", [])
    tools2 = {r.tool for r in s2.tool_results}
    assert "schema_search" not in tools2, "增量不应重跑发现链"
    assert tools2 == {"python_analysis"}, f"只应执行目标阶段：{tools2}"


def test_date_change_out_of_range_falls_back_to_full_chain(mock_env, monkeypatch):
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT sale_date, region_id, revenue FROM fact_sales LIMIT 60", spy)
    sid = "e3_date"
    run_analysis(sid, "看看各区域营收")
    before = spy.get("n", 0)

    s = run_analysis(sid, "基于上一结果，改为 2025年1月 的营收")
    assert s.mode != "iteration", "越界改期不应走增量（数据集无该期间数据）"
    assert spy.get("n", 0) > before, "越界改期应回退全链（调用 planner）"
    assert s.metadata.get("iteration_skip"), "回退原因应可审计"

    s2 = run_analysis(sid, "基于上一结果，改为 2024年3月 的营收")
    assert s2.mode == "iteration" and s2.iteration["kind"] == "date_change"


def test_force_full_rerun_flag_skips_iteration(mock_env, monkeypatch):
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_flag"
    run_analysis(sid, "看看各区域营收")
    before = spy.get("n", 0)

    s = run_analysis(sid, "基于上一结果，改为只看 region 1 的营收", force_full_rerun=True)
    assert s.mode != "iteration", "显式 force_full_rerun 应压过增量"
    assert spy.get("n", 0) > before, "force_full_rerun 应回全链（调用 planner）"


def test_analyze_request_exposes_force_full_rerun():
    from app.models.schemas import AnalyzeRequest

    assert AnalyzeRequest(query="q").force_full_rerun is False
    assert AnalyzeRequest(query="q", force_full_rerun=True).force_full_rerun is True


# --------------------------------------------------------------------------- #
# D16：口径切换 / 维度可用性 / 未指代 —— 负路径（不该命中的绝不命中）
# --------------------------------------------------------------------------- #
def test_no_anaphora_still_runs_full_chain(mock_env, monkeypatch):
    """N1：会话有数据集，但本轮是全新问题（无指代词）→ 必须回全链。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_noana"
    run_analysis(sid, "看看各区域营收")
    assert load_last_dataset(sid), "第一轮应落数据集"
    before = spy.get("n", 0)

    s = run_analysis(sid, "哪个区域的营收最高？")
    assert s.mode != "iteration", "未指代不得走增量"
    assert spy.get("n", 0) > before, "未指代应回全链（调用 planner）"


def test_measure_switch_falls_back_to_full_chain(mock_env, monkeypatch):
    """N2：上一结果是 region/revenue 投影，切到「订单数」需重新取数。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_metric"
    run_analysis(sid, "看看各区域营收")
    before = spy.get("n", 0)

    s = run_analysis(sid, "基于上一结果，改成按订单数看各区域")
    assert s.mode != "iteration", "口径切换（度量不在上一结果）不得走增量"
    assert spy.get("n", 0) > before, "口径切换应回全链"
    assert "订单数" in (s.metadata.get("iteration_skip") or ""), s.metadata


def test_dimension_not_in_dataset_falls_back(mock_env, monkeypatch):
    """N3：上一结果只按 region 聚合，下钻到城市需 join 维表重取。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_dim"
    run_analysis(sid, "看看各区域营收")
    before = spy.get("n", 0)

    s = run_analysis(sid, "基于上一结果，下钻到城市看营收")
    assert s.mode != "iteration", "维度不在上一结果不得走增量"
    assert spy.get("n", 0) > before, "维度缺失应回全链"
    assert "城市" in (s.metadata.get("iteration_skip") or ""), s.metadata


def test_guard_reports_missing_measures_and_dims():
    ds = {"csv": "x.csv", "columns": ["region_id", "revenue"]}
    ok, g = guard_iteration(ds, "基于上一结果，只看 region 1 的营收", "filter")
    assert ok and g["reason"] is None
    bad, g2 = guard_iteration(ds, "基于上一结果，改成按订单数看", "generic")
    assert not bad and g2.get("missing") == ["订单数"], g2
    bad2, g3 = guard_iteration(ds, "基于上一结果，下钻到城市", "drilldown")
    assert not bad2 and g3.get("missing") == ["城市"], g3
    # 值级词（区域名/值）不受守卫约束
    ok2, _ = guard_iteration(ds, "基于上一结果，只看华东的营收", "filter")
    assert ok2


def test_measure_and_dimension_present_stays_iteration(mock_env, monkeypatch):
    """正路径：度量/维度确实在上一结果里 → 仍走增量。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue, orders FROM fact_sales LIMIT 20", spy)
    sid = "e3_present"
    run_analysis(sid, "看看各区域营收")

    s1 = run_analysis(sid, "基于上一结果，改成按订单数看各区域")
    assert s1.mode == "iteration", (s1.mode, s1.metadata)

    s2 = run_analysis(sid, "基于上一结果，下钻到区域看营收")
    assert s2.mode == "iteration" and s2.iteration["kind"] == "drilldown", s2.iteration


# --------------------------------------------------------------------------- #
# D17：增量产物回写为下一轮基线（E3/04）+ 连续下钻链
# --------------------------------------------------------------------------- #
def test_iteration_result_becomes_new_baseline(mock_env, monkeypatch):
    """增量步真沙箱产出 CSV → 成为下一轮「上一结果」（derived_from 可追溯）。"""
    from app.core.agents.data_analyst.graph import run_analysis
    import app.infrastructure.llm.router as llm_router

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_wb"
    run_analysis(sid, "看看各区域营收")
    base = load_last_dataset(sid)
    assert base and base["columns"] == ["region_id", "revenue"]

    # 只在增量轮的 python_code_gen 阶段出码：真在沙箱里落一个 CSV 产物
    real_llm = llm_router.get_llm()

    class _Stub:
        def complete(self, system, user, stage="", json_mode=False):
            if stage == "python_code_gen":
                return ("import pandas as pd\n"
                        "df.groupby('region_id', as_index=False)['revenue'].sum()"
                        ".to_csv('city_level.csv', index=False)\n"
                        "print('written', df.shape)\n")
            return real_llm.complete(system, user, stage=stage, json_mode=json_mode)

    monkeypatch.setattr(llm_router, "get_llm", lambda: _Stub())

    s2 = run_analysis(sid, "基于上一结果，下钻到区域看营收")
    assert s2.mode == "iteration", (s2.mode, s2.metadata)
    assert s2.tool_results[-1].status == "SUCCESS", s2.tool_results[-1].error

    ds2 = load_last_dataset(sid)
    assert ds2["csv"].endswith("city_level.csv"), ds2
    assert ds2["columns"] == ["region_id", "revenue"], ds2
    assert ds2["derived_from"] == base["step_id"], ds2

    # 第三次请求以**新基线**为源（连续的"上一结果"链）
    s3 = run_analysis(sid, "基于上一结果，只看 region 1 的营收")
    assert s3.mode == "iteration", (s3.mode, s3.metadata)
    assert s3.tool_results[-1].input.get("data_csv") == ds2["csv"], "应作用于新基线"


def test_iteration_without_new_csv_keeps_baseline(mock_env, monkeypatch):
    """负：增量步没有新 CSV 产物（mock 只打印 stdout）→ 基线不得前移。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT region_id, revenue FROM fact_sales LIMIT 20", spy)
    sid = "e3_nowb"
    run_analysis(sid, "看看各区域营收")
    base = load_last_dataset(sid)

    s2 = run_analysis(sid, "基于上一结果，下钻到区域看营收")
    assert s2.mode == "iteration", (s2.mode, s2.metadata)
    assert load_last_dataset(sid)["csv"] == base["csv"], "无新 CSV 时基线应保持不变"


def test_iteration_flags_caliber_drift(mock_env, monkeypatch):
    """E4/03 联动：迭代轮改了时间切片后，报告里再做环比 = 跨口径比较，必须提示。"""
    from app.core.agents.data_analyst.graph import run_analysis

    spy: dict = {}
    _patch_planner(monkeypatch, "SELECT sale_date, region_id, revenue FROM fact_sales LIMIT 30", spy)
    sid = "e3_caliber"
    run_analysis(sid, "看看各区域营收")

    s2 = run_analysis(sid, "基于上一结果，改为 2024年3月 的营收并看环比增长")
    assert s2.mode == "iteration", (s2.mode, s2.metadata)
    assert "口径提示" in (s2.report or ""), "迭代后跨口径比较必须提示"
    kinds = [i["kind"] for i in s2.metadata.get("caliber_issues", [])]
    assert "iteration_drift" in kinds, s2.metadata
