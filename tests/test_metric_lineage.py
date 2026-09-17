"""D47：**指标血缘** —— metric → 计算口径 → 源列/表，可查询。

缺口（计划 §8.4 C-1）
--------------------
> 指标语义层（重版）：**指标血缘**、口径注册表、同环比基线自动判定
> —— 当前只做了"维表枚举+键推断"轻版。

分析师问"这个营收数是怎么算出来的"，现在的答案只能到 E1 的 `[src:step_id]`（哪条 SQL），
**再往下就断了**：那条 SQL 读了哪些表、哪些列，没人说得清。

本模块把链子补到列级：`metric → 口径说明 → sql_id → SQL → 表/列`。

两条诚实纪律（沿用 SEMANTIC/01 的 `confidence` 范式）
---------------------------------------------------
1. **解析出来的 ≠ 事实**：表名/列名是从 SQL **文本解析**的，带 `confidence="parsed_from_sql"`；
   上游不得当成权威元数据（真实血缘要读 `information_schema` 或查询计划）。
2. **没有就说没有**：`sql_id` 缺失/解析不到时 → `traced=False` + 可读原因，
   **绝不从别处"借"一个来源**（编血缘比没有血缘更坏）。
"""
from __future__ import annotations

from app.core.agents.data_analyst.lineage import (
    lineage_summary,
    metric_lineage,
    parse_sql_sources,
)
from app.core.agents.data_analyst.state import (
    AgentState,
    AnalysisResult,
    ContextModel,
    ToolResult,
)

SQL = ("SELECT r.region_name, SUM(f.revenue) AS total_revenue "
       "FROM fact_sales f JOIN dim_region r ON f.region_id = r.region_id "
       "WHERE f.sale_date >= '2024-01-01' GROUP BY r.region_name")


def _sql_step(step_id: str = "step_3", sql: str = SQL) -> ToolResult:
    return ToolResult(step_id=step_id, tool="sql_query", status="SUCCESS",
                      input={"sql": sql}, output={"ok": True, "row_count": 5})


def _state(metrics, *, results=None) -> AgentState:
    s = AgentState(session_id="lin", user_query="分析各区域营收")
    s.context = ContextModel(objective="分析各区域营收")
    s.analysis = AnalysisResult(metrics=list(metrics))
    s.tool_results = results if results is not None else [_sql_step()]
    return s


# --------------------------------------------------------------------------- #
# 一、SQL 源解析（纯函数）
# --------------------------------------------------------------------------- #
def test_parse_tables_including_aliases():
    tables, _ = parse_sql_sources(SQL)
    assert set(tables) == {"fact_sales", "dim_region"}


def test_parse_columns_strips_alias_prefix_and_as_alias():
    _, cols = parse_sql_sources(SQL)
    assert "revenue" in cols and "region_name" in cols and "sale_date" in cols
    assert "f.revenue" not in cols, "带表前缀的写法应归一为列名"
    assert "total_revenue" not in cols, "`AS x` 是别名，不是源列"


def test_parse_ignores_sql_keywords_and_functions():
    _, cols = parse_sql_sources(SQL)
    for kw in ("select", "sum", "from", "where", "group", "by", "join", "on"):
        assert kw not in cols, f"{kw} 被当成了列名"


def test_parse_handles_comma_join():
    tables, _ = parse_sql_sources("SELECT a.x FROM t1 a, t2 b WHERE a.id = b.id")
    assert set(tables) == {"t1", "t2"}


def test_parse_empty_sql_is_safe():
    assert parse_sql_sources("") == ([], [])
    assert parse_sql_sources(None) == ([], [])


# --------------------------------------------------------------------------- #
# 二、血缘：解析得到就有，解析不到就**如实说没有**
# --------------------------------------------------------------------------- #
def test_metric_with_resolvable_sql_is_traced():
    state = _state([{"name": "总营收", "definition": "SUM(fact_sales.revenue)", "sql_id": "step_3"}])
    out = metric_lineage(state.analysis, state.tool_results)
    assert len(out) == 1
    lin = out[0]
    assert lin["metric"] == "总营收" and lin["traced"] is True
    assert set(lin["tables"]) == {"fact_sales", "dim_region"}
    assert "revenue" in lin["columns"]
    assert lin["confidence"] == "parsed_from_sql", "解析产物不得冒充权威元数据"


def test_metric_without_sql_id_is_unresolved_not_fabricated():
    """`sql_id=None` 是实测常态——**不许从别处借一个来源**。"""
    state = _state([{"name": "总营收", "value": "UNKNOWN", "unit": "货币单位", "sql_id": None}])
    lin = metric_lineage(state.analysis, state.tool_results)[0]
    assert lin["traced"] is False
    assert lin["tables"] == [] and lin["columns"] == []
    assert lin["confidence"] == "unresolved" and lin["note"]


def test_metric_with_dangling_sql_id_is_unresolved():
    state = _state([{"name": "总营收", "sql_id": "step_999"}])
    lin = metric_lineage(state.analysis, state.tool_results)[0]
    assert lin["traced"] is False and "step_999" in lin["note"]


def test_metric_without_definition_still_resolves():
    """口径说明是模型给的**可选**字段；缺了不影响血缘解析。"""
    state = _state([{"name": "订单数", "sql_id": "step_3"}])
    lin = metric_lineage(state.analysis, state.tool_results)[0]
    assert lin["traced"] is True and lin["definition"] == ""


# --------------------------------------------------------------------------- #
# 三、汇总 + 健壮性
# --------------------------------------------------------------------------- #
def test_summary_counts_traced_and_unresolved():
    state = _state([{"name": "a", "sql_id": "step_3"}, {"name": "b", "sql_id": None}])
    summary = lineage_summary(metric_lineage(state.analysis, state.tool_results))
    assert summary["metrics"] == 2 and summary["traced"] == 1
    assert summary["coverage"] == 0.5


def test_summary_of_empty_is_undefined_not_zero():
    """零指标时覆盖率是**未定义**而非 0（同 E1 溯源与 D41 幻觉率的纪律）。"""
    summary = lineage_summary([])
    assert summary["metrics"] == 0 and summary["coverage"] is None


def test_malformed_metrics_never_raise():
    state = _state([None, "字符串", {"name": ""}, {"sql_id": 123}])
    out = metric_lineage(state.analysis, state.tool_results)
    assert isinstance(out, list), "畸形输入不得抛（血缘是只读辅助，不是关键路径）"


# --------------------------------------------------------------------------- #
# 四、API 端点
# --------------------------------------------------------------------------- #
def test_lineage_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()
    try:
        from fastapi.testclient import TestClient

        from app.core.agents.data_analyst.graph import run_analysis
        from app.main import app

        run_analysis("lin-api", "分析各区域营收")
        with TestClient(app) as c:
            r = c.get("/api/v1/chat/analyze/lineage/lin-api")
            assert r.status_code == 200, r.text[:200]
            body = r.json()
            assert "summary" in body and "metrics" in body
            assert body["summary"]["metrics"] == len(body["metrics"])

            missing = c.get("/api/v1/chat/analyze/lineage/never-ran")
            assert missing.status_code == 404
    finally:
        get_settings.cache_clear()
        reset_llm()
