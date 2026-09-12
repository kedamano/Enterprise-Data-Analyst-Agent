"""INTERVIEW/01 ③ 工具路由：TF-IDF 余弦选工具（八股文 04.4）。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §3
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.tools.routing import (
    route_tools,
    select_tools_for_planner,
    tool_document,
)
from app.infrastructure.llm.router import reset_llm


class _Spec:
    def __init__(self, description: str, props: dict | None = None, scope: str = ""):
        self.description = description
        self.input_schema = {"properties": props or {}}
        self.data_scope = scope


def _fake_specs(n: int = 30) -> dict:
    specs = {
        "order_query": _Spec("查询订单表 orders 的订单量与金额", {"keyword": {}}, "warehouse"),
        "user_profile": _Spec("查询用户画像 users 的注册与活跃", {"user_id": {}}, "crm"),
        "inventory": _Spec("查询库存 inventory 的周转与缺货", {"sku": {}}, "wms"),
    }
    for i in range(n - len(specs)):
        specs[f"misc_tool_{i}"] = _Spec(f"无关能力编号 {i}", {"arg": {}})
    return specs


# --------------------------------------------------------------------------- #
# 1. 语义命中
# --------------------------------------------------------------------------- #
def test_routes_to_semantically_relevant_tool():
    specs = _fake_specs()
    top = route_tools("统计各区域订单量和金额", specs=specs, top_k=3)
    assert top[0] == "order_query", top


def test_routing_is_deterministic():
    specs = _fake_specs()
    a = route_tools("库存缺货情况", specs=specs, top_k=5)
    b = route_tools("库存缺货情况", specs=list(reversed(list(specs.items()))) and specs, top_k=5)
    assert a == b, "同样的输入必须给同样的路由（可复现）"


def test_irrelevant_query_falls_back_to_all_tools():
    """**关键边界**：查询与任何工具都不沾边时，回退全量而不是给个空列表。"""
    specs = _fake_specs()
    got = route_tools("🦄✨", specs=specs)
    assert set(got) == set(specs.keys()), "命中不足时必须回退全量，否则 planner 无工具可用"


def test_real_registry_routing_is_sane():
    """用真实 8 个工具跑一遍：三个典型意图应各自命中对的工具。"""
    cases = {
        "帮我写个 Python 脚本清洗 CSV": "python_analysis",
        "各区域营收的 SQL 查询": "sql_query",
        "公司口径的定义与业务知识": "knowledge_search",
    }
    for query, expected in cases.items():
        got = route_tools(query, top_k=4)
        assert expected in got, f"{query!r} 未路由到 {expected}：{got}"


def test_tool_document_includes_params_and_scope():
    doc = tool_document("dataset_profile", _Spec("数据画像", {"table": {}, "key": {}}, "metadata"))
    for token in ("dataset_profile", "数据画像", "table", "key", "metadata"):
        assert token in doc


# --------------------------------------------------------------------------- #
# 2. 自适应注入
# --------------------------------------------------------------------------- #
def test_small_toolset_not_routed():
    specs = {"a": _Spec("甲"), "b": _Spec("乙")}
    names, routed = select_tools_for_planner("任意问题", specs=specs)
    assert routed is False and set(names) == {"a", "b"}, "工具少时全给（路由是负收益）"


def test_large_toolset_is_routed():
    specs = _fake_specs(40)
    names, routed = select_tools_for_planner("统计订单量", specs=specs)
    assert routed is True
    assert len(names) < len(specs), "工具多时必须收敛"
    assert "order_query" in names


def test_threshold_count_is_respected():
    specs = _fake_specs(12)
    _, routed = select_tools_for_planner("订单", specs=specs, threshold_count=12)
    assert routed is False, "等于阈值时不路由"
    _, routed2 = select_tools_for_planner("订单", specs=specs, threshold_count=11)
    assert routed2 is True


# --------------------------------------------------------------------------- #
# 3. 接入 planner：可观测
# --------------------------------------------------------------------------- #
@pytest.fixture
def plan_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def test_planner_records_routed_tools_when_large(plan_env, monkeypatch):
    """工具集很大时，planner 只收到路由结果，并在 metadata 里可观测。"""
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.state import AgentState
    from app.core.tools import specs as specs_mod

    big = _fake_specs(40)
    monkeypatch.setattr(specs_mod, "TOOL_SPECS", big, raising=False)
    # 走真实配置路径（而不是塞一个假 settings 对象——那会漏掉 run_planner 用到的其它字段）
    monkeypatch.setenv("TOOL_ROUTING_THRESHOLD", "12")
    get_settings.cache_clear()

    seen: list[str] = []
    # planner 假输出必须含**可用步骤**：空计划会被 `_llm_model(ok=...)` 判为不可用。
    monkeypatch.setattr(
        nodes, "_llm",
        lambda stage, user, json_mode=True: seen.append(user) or
        '{"goal":"g","steps":[{"id":"s1","objective":"统计订单量","action":"查询",'
        '"tool":"sql_query"}]}')

    st = AgentState(session_id="route_plan", user_query="统计订单量")
    st.context.objective = "统计订单量"
    nodes.run_planner(st)

    assert "available_tools" in seen[-1], "大工具集必须把路由结果注入 planner"
    routed = st.metadata.get("routed_tools")
    assert routed and len(routed) < len(big), routed
    assert "order_query" in routed
