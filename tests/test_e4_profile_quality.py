"""E4/01 dataset_profile 质量基元：主键唯一 / join 放大 / 粒度 / 日期连续性。

Spec: docs/specs/E4/01-profile-quality.md
D19 = 红（11 failed / 2 passed）→ D20 实现转绿（xfail 标记已撤）。

期望值取自样例库 data/sample_enterprise.db（fact_sales 3120 行）：
  sale_id 3120/3120 唯一；region_id 仅 5 值（重复 3115）；revenue 恰好也唯一（浮点度量，弱信号）
  sale_date 覆盖 2024-01-01..2024-12-23 共 358 天、实际只有 52 天（按周）
  自我 join（on region_id）= 3120 × 624 = 1,946,880 行
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.tools import profile_tool
from app.infrastructure.llm.router import reset_llm

_SELF_JOIN = ("SELECT f.sale_id, f.region_id, g.revenue "
              "FROM fact_sales f JOIN fact_sales g ON f.region_id = g.region_id")
_GROUPED = ("SELECT region_id, product_id, COUNT(*) AS n "
            "FROM fact_sales GROUP BY region_id, product_id")


@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


@pytest.fixture
def profile_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 1. 主键唯一性（D19 红测重点）
# --------------------------------------------------------------------------- #
def test_primary_key_uniqueness_positive(profile_env):
    out = profile_tool.run({"table": "fact_sales"})
    assert out["ok"], out.get("error")

    ku = out["key_uniqueness"]
    assert ku["candidate_keys"] == ["sale_id", "revenue"], ku["candidate_keys"]  # 列序、"唯一"是弱信号
    assert ku["likely_key"] == "sale_id", "应优先选 *_id 且非浮点度量的候选"
    assert ku["declared_key"] == ["sale_id"]
    assert ku["is_unique"] is True
    assert ku["duplicate_rows"] == 0
    assert ku["grain"] == "row"
    assert "region_id" not in ku["candidate_keys"], "5 个不同值的列不是候选键"


def test_primary_key_uniqueness_negative(profile_env):
    out = profile_tool.run({"table": "fact_sales", "key": ["region_id"]})
    assert out["ok"], out.get("error")

    ku = out["key_uniqueness"]
    assert ku["declared_key"] == ["region_id"]
    assert ku["is_unique"] is False
    assert ku["duplicate_rows"] == 3115
    assert ku["duplicate_ratio"] > 0.99
    assert ku["grain"] == "row", "grain 描述结果集粒度，与声明的键无关"


def test_composite_key_is_unique(profile_env):
    key = ["sale_date", "region_id", "product_id", "channel_id"]
    out = profile_tool.run({"table": "fact_sales", "key": key})
    assert out["ok"], out.get("error")

    ku = out["key_uniqueness"]
    assert ku["declared_key"] == key
    assert ku["is_unique"] is True
    assert ku["duplicate_rows"] == 0


def test_aggregated_result_has_no_row_key(profile_env):
    """按 region×product 聚合（各组行数相同）→ 无单列唯一键 → 粒度 aggregated，唯一性无从判定。"""
    out = profile_tool.run({"sql": _GROUPED})
    assert out["ok"], out.get("error")

    ku = out["key_uniqueness"]
    assert out["row_count"] == 20
    assert ku["candidate_keys"] == []
    assert ku["is_unique"] is None, "没有键可判时应为 null，而不是 False"
    assert ku["grain"] == "aggregated"


def test_unique_by_luck_measure_does_not_become_a_key(profile_env):
    """「唯一」是弱信号：20 组的 SUM(revenue) 恰好全不同，但它是度量而非键。

    规格要求 `candidate_keys` 透明列出（不藏），但**不能**据此宣称行粒度或唯一键：
    该列不像业务键 → `likely_key=None` → `declared_key=[]` → `is_unique=None`、`grain=aggregated`。
    """
    lucky = ("SELECT region_id, product_id, SUM(revenue) AS revenue "
             "FROM fact_sales GROUP BY region_id, product_id")
    out = profile_tool.run({"sql": lucky})
    assert out["ok"], out.get("error")

    ku = out["key_uniqueness"]
    assert ku["candidate_keys"] == ["revenue"], "候选要如实列出"
    assert ku["likely_key"] is None, "度量列不该被当成业务键"
    assert ku["declared_key"] == []
    assert ku["is_unique"] is None
    assert ku["grain"] == "aggregated"


# --------------------------------------------------------------------------- #
# 2. join 放大
# --------------------------------------------------------------------------- #
def test_join_amplification_detected(profile_env):
    out = profile_tool.run({"sql": _SELF_JOIN, "base_table": "fact_sales"})
    assert out["ok"], out.get("error")

    amp = out["join_amplification"]
    assert amp["base_rows"] == 3120
    assert amp["result_rows"] == 1946880
    assert amp["factor"] == 624.0
    assert amp["amplified"] is True
    assert amp["threshold"] == 1.5


def test_join_amplification_absent_without_base_table(profile_env):
    out = profile_tool.run({"sql": _SELF_JOIN})
    assert out["ok"], out.get("error")
    assert out["join_amplification"] is None


def test_two_table_join_is_not_amplified(profile_env):
    """正路径：维表 join 事实表（N:1）不放大 → factor 1.0，不能误报。"""
    out = profile_tool.run({
        "sql": "SELECT f.sale_id, p.product_name FROM dim_product p "
               "JOIN fact_sales f ON p.product_id = f.product_id",
        "base_table": "fact_sales"})
    assert out["ok"], out.get("error")

    amp = out["join_amplification"]
    assert amp["result_rows"] == 3120
    assert amp["factor"] == 1.0
    assert amp["amplified"] is False, "维表 join 事实表不该被判放大"


def test_cartesian_join_amplification_detected(profile_env):
    """典型翻车：忘写 join 条件 → 笛卡尔积 5 倍放大（分析师最常见的事故之一）。"""
    out = profile_tool.run({
        "sql": "SELECT f.sale_id, r.region_name FROM fact_sales f CROSS JOIN dim_region r",
        "base_table": "fact_sales"})
    assert out["ok"], out.get("error")

    amp = out["join_amplification"]
    assert amp["result_rows"] == 15600
    assert amp["factor"] == 5.0
    assert amp["amplified"] is True


# --------------------------------------------------------------------------- #
# 3. 日期连续性
# --------------------------------------------------------------------------- #
def test_date_continuity_reports_sparse(profile_env):
    out = profile_tool.run({"table": "fact_sales", "date_column": "sale_date"})
    assert out["ok"], out.get("error")

    dc = out["date_continuity"]
    assert dc["column"] == "sale_date"
    assert dc["min"] == "2024-01-01" and dc["max"] == "2024-12-23"
    assert dc["distinct_days"] == 52
    assert dc["expected_days"] == 358
    assert dc["missing_days"] == 306
    assert dc["sparse"] is True
    assert dc["gap_samples"] == ["2024-01-02", "2024-01-03", "2024-01-04"]


def test_date_continuity_null_when_no_date_column(profile_env):
    out = profile_tool.run({"table": "dim_region"})
    assert out["ok"], out.get("error")
    assert out["date_continuity"] is None


# --------------------------------------------------------------------------- #
# 4. 负路径
# --------------------------------------------------------------------------- #
def test_rejects_illegal_identifier(profile_env):
    out = profile_tool.run({"table": "fact_sales; DROP TABLE dim_region"})
    assert out["ok"] is False
    assert "非法标识符" in out["error"], out["error"]


def test_illegal_key_column_is_rejected(profile_env):
    out = profile_tool.run({"table": "fact_sales", "key": ["sale_id) FROM fact_sales --"]})
    assert out["ok"] is False
    assert "非法标识符" in out["error"], out["error"]


def test_missing_table_fails_readably(profile_env):
    out = profile_tool.run({"table": "no_such_table"})
    assert out["ok"] is False
    assert out["error"], "失败必须带可读原因"


def test_requires_table_or_sql(profile_env):
    out = profile_tool.run({})
    assert out["ok"] is False
    assert "table" in out["error"] and "sql" in out["error"]


# --------------------------------------------------------------------------- #
# 5. 向后兼容
# --------------------------------------------------------------------------- #
def test_existing_fields_preserved(profile_env):
    out = profile_tool.run({"table": "fact_sales"})
    assert out["row_count"] == 3120
    col = out["columns"]["sale_id"]
    assert col["null_count"] == 0
    assert col["null_ratio"] == 0.0
    assert col["distinct"] == 3120


# --------------------------------------------------------------------------- #
# 6. 编排可达性：计划步骤能把新参数传进 dataset_profile
# --------------------------------------------------------------------------- #
def test_plan_step_can_declare_profile_params(mock_llm_env):
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import (AgentState, PlanStep, ToolResult)

    st = AgentState(session_id="e4_params", user_query="q")
    st.tool_results = [ToolResult(step_id="s0", tool="schema_search", status="SUCCESS",
                                  output={"tables": [{"table": "fact_sales", "columns": []}]})]
    step = PlanStep(id="s1", objective="看主键唯一性", action="画像", tool="dataset_profile",
                    input={"key": ["region_id"], "base_table": "fact_sales",
                           "date_column": "sale_date"})
    params = build_executor_params(st, step)
    assert params["table"] == "fact_sales"
    assert params["key"] == ["region_id"]
    assert params["base_table"] == "fact_sales"
    assert params["date_column"] == "sale_date"


def test_plan_step_defaults_to_table_only(mock_llm_env):
    """没有显式声明时行为不变（向后兼容）。"""
    from app.core.agents.data_analyst.nodes import build_executor_params
    from app.core.agents.data_analyst.state import AgentState, PlanStep, ToolResult

    st = AgentState(session_id="e4_params2", user_query="q")
    st.tool_results = [ToolResult(step_id="s0", tool="schema_search", status="SUCCESS",
                                  output={"tables": [{"table": "fact_sales", "columns": []}]})]
    step = PlanStep(id="s1", objective="看数据质量", action="画像", tool="dataset_profile")
    assert build_executor_params(st, step) == {"table": "fact_sales"}
