"""INTERVIEW/01 ④ 请求级缓存：同问重复问直接命中（八股文 08.2）。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §4
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst import response_cache
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    monkeypatch.setenv("RESPONSE_CACHE_ENABLED", "true")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    response_cache.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    response_cache.clear()


@pytest.fixture
def spy_context(monkeypatch):
    """统计 run_context 被调用次数 = 是否真的重跑了整条链。"""
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    calls = {"n": 0}
    real = nodes.run_context

    def _counting(state):
        calls["n"] += 1
        return real(state)

    monkeypatch.setattr(g, "run_context", _counting)
    return calls


# --------------------------------------------------------------------------- #
# 1. 命中与不命中
# --------------------------------------------------------------------------- #
def test_same_query_in_same_session_hits_cache(cache_env, spy_context):
    from app.core.agents.data_analyst.graph import run_analysis

    first = run_analysis("cache_s1", "分析各区域营收")
    assert first.status == "FINISH"
    assert spy_context["n"] == 1
    assert not first.metadata.get("cache_hit")

    second = run_analysis("cache_s1", "分析各区域营收")
    assert spy_context["n"] == 1, "二次相同提问不应再跑整条链"
    assert second.metadata.get("cache_hit") is True, "命中必须显式标记（不许假装是新分析）"
    assert second.report == first.report, "命中应返回同样的报告"


def test_same_query_different_session_misses(cache_env, spy_context):
    """**按会话隔离**：跨会话复用会泄漏数据（多租户下不可接受）。"""
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("cache_a", "分析各区域营收")
    run_analysis("cache_b", "分析各区域营收")
    assert spy_context["n"] == 2, "不同会话必须各自计算"


def test_different_query_misses(cache_env, spy_context):
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("cache_q", "分析各区域营收")
    run_analysis("cache_q", "分析各渠道订单量")
    assert spy_context["n"] == 2


def test_whitespace_and_case_are_normalized(cache_env, spy_context):
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("cache_n", "分析各区域营收")
    run_analysis("cache_n", "  分析各区域营收  ")
    assert spy_context["n"] == 1, "空白差异不该造成重复计算"


# --------------------------------------------------------------------------- #
# 2. 不该缓存的
# --------------------------------------------------------------------------- #
def test_failed_run_is_not_cached(cache_env, monkeypatch):
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.graph import run_analysis
    from app.core.agents.data_analyst.state import AgentState

    real_context = nodes.run_context

    def _boom(state: AgentState) -> AgentState:
        state.status = "ERROR"
        state.error = "模拟失败"
        return state

    monkeypatch.setattr(g, "run_context", _boom)
    run_analysis("cache_fail", "会失败的问题")
    assert response_cache.get_cached("cache_fail", "会失败的问题") is None, \
        "失败的运行没有可复用结论，不得缓存"

    # 恢复真实 context：这次必须真跑（说明上次失败没留下"假命中"）
    monkeypatch.setattr(g, "run_context", real_context)
    again = run_analysis("cache_fail", "会失败的问题")
    assert again.status == "FINISH"
    assert not again.metadata.get("cache_hit")
    assert again.report, "重跑应产出报告"


def test_force_full_rerun_bypasses_and_refreshes(cache_env, spy_context):
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("cache_f", "分析各区域营收")
    run_analysis("cache_f", "分析各区域营收")           # 命中
    assert spy_context["n"] == 1

    again = run_analysis("cache_f", "分析各区域营收", force_full_rerun=True)
    assert spy_context["n"] == 2, "force_full_rerun 必须真的重算"
    assert not again.metadata.get("cache_hit")


def test_cache_can_be_disabled(cache_env, spy_context, monkeypatch):
    monkeypatch.setenv("RESPONSE_CACHE_ENABLED", "false")
    get_settings.cache_clear()

    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("cache_off", "分析各区域营收")
    run_analysis("cache_off", "分析各区域营收")
    assert spy_context["n"] == 2, "关闭缓存后必须每次真算"


# --------------------------------------------------------------------------- #
# 3. 对外契约
# --------------------------------------------------------------------------- #
def test_api_exposes_cache_hit(cache_env):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    payload = {"query": "分析各区域营收", "session_id": "cache_api"}
    assert c.post("/api/v1/chat/analyze", json=payload).json()["cache_hit"] is False
    assert c.post("/api/v1/chat/analyze", json=payload).json()["cache_hit"] is True


def test_cache_key_is_session_scoped_and_stable():
    a = response_cache.cache_key("s1", "问题")
    b = response_cache.cache_key("s1", " 问题 ")
    c = response_cache.cache_key("s2", "问题")
    assert a == b, "归一化后同 key"
    assert a != c, "不同会话必须不同 key"
    assert a.startswith("resp:")


def test_degraded_result_is_not_cached(cache_env, spy_context):
    """**降级结果不入缓存**：LLM 不可用时兜底出的模板报告，
    缓存它等于"真模型恢复了也照旧返回模板"（DEGRADE/01 的教训）。"""
    from app.core.agents.data_analyst.response_cache import put_cached
    from app.core.agents.data_analyst.state import AgentState

    st = AgentState(session_id="cache_deg", user_query="q", status="FINISH")
    st.report = "模板报告"
    st.metadata["degraded"] = True
    put_cached("cache_deg", "q", st)
    assert response_cache.get_cached("cache_deg", "q") is None

    st.metadata["degraded"] = False
    put_cached("cache_deg", "q", st)
    assert response_cache.get_cached("cache_deg", "q") is not None, "未降级才可缓存"
