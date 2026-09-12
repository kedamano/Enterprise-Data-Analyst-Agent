"""SEMANTIC/01 业务语义层：让 Agent 看懂列的业务含义（region_id=1 → 华东）。

Spec: docs/specs/SEMANTIC/01-business-semantics.md
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.core.semantics import (
    Semantics,
    collect_semantics,
    describe_semantics,
    infer_relationships,
    is_pii_column,
)
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def sem_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


# --------------------------------------------------------------------------- #
# 1. PII 跳过（本模块自带，因为 E4/02 尚未实现）
# --------------------------------------------------------------------------- #
def test_pii_column_detection():
    for name in ("phone", "mobile_no", "email", "id_card", "user_name", "customer_name",
                 "身份证", "手机号", "住址"):
        assert is_pii_column(name) is True, name
    # 维表标签列必须**不**被判 PII，否则整层语义会被跳过
    for name in ("region_id", "revenue", "sale_date", "channel_name", "region_name", "orders"):
        assert is_pii_column(name) is False, name


# --------------------------------------------------------------------------- #
# 2. 关系与维表推断
# --------------------------------------------------------------------------- #
def test_infer_relationships_by_naming_convention():
    tables = [
        {"table": "dim_region", "columns": [{"name": "region_id"}, {"name": "region_name"}]},
        {"table": "dim_product", "columns": [{"name": "product_id"}, {"name": "product_name"}]},
        {"table": "fact_sales", "columns": [{"name": "sale_id"}, {"name": "region_id"},
                                            {"name": "product_id"}, {"name": "revenue"}]},
    ]
    rels = infer_relationships(tables)
    pairs = {(r["from_table"], r["from_col"], r["to_table"], r["to_col"]) for r in rels}
    assert ("fact_sales", "region_id", "dim_region", "region_id") in pairs
    assert ("fact_sales", "product_id", "dim_product", "product_id") in pairs
    assert all(r["kind"] == "many_to_one" for r in rels)
    assert all(r["confidence"] == "naming_convention" for r in rels), \
        "命名约定推断必须如实标注置信度，不得当事实"


def test_plain_table_is_not_treated_as_dimension():
    tables = [{"table": "events", "columns": [{"name": "event_id"}, {"name": "ts"}]}]
    assert infer_relationships(tables) == []


# --------------------------------------------------------------------------- #
# 3. 真实样例库采集
# --------------------------------------------------------------------------- #
def test_collect_real_semantics(sem_env):
    sem = collect_semantics("sem_real", use_cache=False)
    assert not sem.is_empty(), "样例库有 dim_* 维表，必须采到"
    region = sem.dimensions.get("region_id")
    assert region and region["table"] == "dim_region"
    assert region["values"].get("1") == "华东", region["values"]
    assert "华东" in describe_semantics(sem)


def test_describe_is_bounded(sem_env):
    sem = collect_semantics("sem_bound", use_cache=False)
    text = describe_semantics(sem, max_dims=1, max_values=2)
    dim_lines = [ln for ln in text.splitlines() if ln.startswith("- dim")]
    assert len(dim_lines) <= 1, text
    assert text.count("…共") <= 1 and "直销/合作伙伴" in text


def test_collect_is_cached(sem_env, monkeypatch):
    import app.core.semantics as sem_mod

    calls = {"n": 0}
    real = sem_mod._collect_uncached

    def _counting(session_id=""):
        calls["n"] += 1
        return real(session_id)

    monkeypatch.setattr(sem_mod, "_collect_uncached", _counting)
    first = collect_semantics("sem_cache")
    second = collect_semantics("sem_cache")
    assert calls["n"] == 1, "第二次应命中缓存"
    assert second.dimensions == first.dimensions


def test_collect_failure_is_not_fatal(sem_env, monkeypatch):
    monkeypatch.setenv("DATA_DB_URL", "sqlite:///./data/__missing__.db")
    get_settings.cache_clear()
    sem = collect_semantics("sem_missing", use_cache=False)
    assert sem.is_empty(), "采集失败必须退化为空语义，绝不抛"
    assert getattr(sem, "error", ""), "失败原因要可观测"


def test_describe_empty_is_blank():
    assert describe_semantics(None) == ""
    assert describe_semantics(Semantics()) == ""


# --------------------------------------------------------------------------- #
# 4. 注入编排（planner 首次获得真实表结构）
# --------------------------------------------------------------------------- #
def test_planner_payload_carries_business_semantics(sem_env, monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.state import AgentState

    seen: list[str] = []

    def _llm(stage, user, json_mode=True):
        seen.append(user)
        # 注意：planner 的输出必须是**可用计划**（≥1 个有 objective/action 的步骤）。
        # 空计划会被 `_llm_model(ok=...)` 判为不可用并重试/报错——假数据要贴近真实契约。
        return ('{"goal":"g","steps":[{"id":"s1","objective":"取数","action":"查询",'
                '"tool":"sql_query"}]}' if stage == "planner"
                else '{"objective":"o","metrics":[]}')

    monkeypatch.setattr(nodes, "_llm", _llm)
    st = AgentState(session_id="sem_plan", user_query="各区域营收")
    st.context.objective = "各区域营收"
    nodes.run_planner(st)

    assert "business_semantics" in seen[-1], "planner 必须拿到业务语义"
    assert "华东" in seen[-1], "语义里应含维表取值（华东）"


def test_context_payload_carries_business_semantics(sem_env, monkeypatch):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.state import AgentState

    seen: list[str] = []
    monkeypatch.setattr(nodes, "_llm",
                        lambda stage, user, json_mode=True: seen.append(user) or '{"objective":"o"}')
    nodes.run_context(AgentState(session_id="sem_ctx", user_query="各区域营收"))
    assert "business_semantics" in seen[-1]


# --------------------------------------------------------------------------- #
# 5. dataset_profile 的 enums（低基数 + PII 跳过）
# --------------------------------------------------------------------------- #
def test_profile_emits_enums_and_skips_pii(sem_env):
    from app.core.tools import profile_tool

    out = profile_tool.run({"table": "dim_region"})
    assert out["ok"], out.get("error")
    assert "华东" in out["enums"].get("region_name", []), out.get("enums")
    assert "enums_skipped" in out, "必须能审计被跳过的列"
    # 高基数列（如 sale_id）不应出现在枚举里
    out2 = profile_tool.run({"table": "fact_sales"})
    assert "sale_id" not in (out2.get("enums") or {}), "高基数列不该采枚举"


def test_profile_enum_pii_column_skipped(sem_env, monkeypatch, tmp_path):
    """含 PII 列的表：该列不进 enums，且记入 enums_skipped。"""
    import sqlite3

    db = tmp_path / "pii.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (customer_id INTEGER, customer_name TEXT)")
    con.executemany("INSERT INTO customers VALUES (?,?)", [(1, "张三"), (2, "李四")])
    con.commit()
    con.close()

    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{db.as_posix()}")
    get_settings.cache_clear()
    from app.core.tools import profile_tool

    out = profile_tool.run({"table": "customers"})
    assert out["ok"], out.get("error")
    assert "customer_name" not in (out.get("enums") or {}), "PII 列的值绝不进上下文"
    assert "customer_name" in out.get("enums_skipped", [])


# --------------------------------------------------------------------------- #
# 6. schema_search 的 ToolSpec 契约校正
# --------------------------------------------------------------------------- #
def test_schema_search_toolspec_no_longer_requires_query():
    from app.core.tools.specs import TOOL_SPECS

    spec = TOOL_SPECS["schema_search"]
    assert spec.input_schema.get("required") == [], \
        "实现只按 keyword 过滤，契约不该强制 query（曾经的死参数）"
    assert "keyword" in spec.input_schema["properties"]


def test_schema_search_accepts_query_as_alias(sem_env):
    from app.core.tools import schema_tool

    by_kw = schema_tool.run({"keyword": "region"})
    by_query = schema_tool.run({"query": "region"})
    assert by_kw["ok"] and by_query["ok"]
    assert [t["table"] for t in by_kw["tables"]] == [t["table"] for t in by_query["tables"]]
