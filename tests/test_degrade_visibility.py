"""DEGRADE/01 降级可见化：静默降级必须对调用方可见、可归因、可告警。

Spec: docs/specs/DEGRADE/01-visible-degradation.md
来源：D18 dry-run 实测——真实 key 因 402 被拒 → 路由器静默降级为 Mock，
而 /health 报的是配置值、响应无降级字段，对外"一切正常"。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.infrastructure.llm.router import (
    fallback_events,
    reset_fallback_events,
    reset_llm,
)

# 指向 discard 端口：连接必定失败，且不产生真实网络流量
_UNREACHABLE = "http://127.0.0.1:9/v1"



def _patch_openai_failure(monkeypatch, message: str = "Error code: 402 - insufficient credits"):
    """让真实调用**立即**失败：本组测试关心的是"失败→降级是否可见"，
    不关心网络语义。指向不可达端口在 Windows 上每次连接要等数秒（整组 3 分钟），
    且行为依赖平台；stub 掉客户端后每次失败是即时的、确定性的。"""
    import openai

    class _Completions:
        def create(self, *a, **kw):
            raise RuntimeError(message)

    class _StubClient:
        def __init__(self, *a, **kw):
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(openai, "OpenAI", _StubClient)


@pytest.fixture
def real_mode_env(monkeypatch):
    """真实模式 + 不可达端点 + 无重试：让每次调用快速失败并触发降级。"""
    monkeypatch.setenv("MOCK_LLM", "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("LLM_BASE_URL", _UNREACHABLE)
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_TIMEOUT_S", "2")
    monkeypatch.setenv("LLM_NO_FALLBACK", "false")
    _patch_openai_failure(monkeypatch)
    # 关掉未启动的中间件：Redis 未起时每次操作要等 ~2s 连接超时（节点级 ×4s）
    for var in ("REDIS_URL", "POSTGRES_DSN", "MILVUS_HOST"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    reset_llm()
    reset_fallback_events()
    # INTERVIEW/01 ④：本组用例依赖"每次都真跑"，必须隔离请求级缓存
    from app.core.agents.data_analyst import response_cache

    response_cache.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    reset_fallback_events()


@pytest.fixture
def mock_mode_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    # 关掉未启动的中间件：Redis 未起时每次操作要等 ~2s 连接超时（节点级 ×4s）
    for var in ("REDIS_URL", "POSTGRES_DSN", "MILVUS_HOST"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    reset_llm()
    reset_fallback_events()
    yield
    get_settings.cache_clear()
    reset_llm()
    reset_fallback_events()


# --------------------------------------------------------------------------- #
# 1. 归因：降级挂在具体 run 上
# --------------------------------------------------------------------------- #
def test_fallback_events_carry_run_id(real_mode_env):
    from app.core.agents.data_analyst.graph import run_analysis

    state = run_analysis("degrade_run_a", "各区域营收如何")
    events = fallback_events()
    assert events, "不可达端点必须触发降级（否则本测试无效）"
    assert all(e["run_id"] == "degrade_run_a" for e in events), events
    assert state.metadata.get("degraded") is True
    assert state.metadata.get("llm_fallbacks"), state.metadata


def test_fallback_attribution_is_per_run(real_mode_env):
    """运行 A 降级，不应污染运行 B 的归因（B 无降级记录）。"""
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("degrade_run_a", "各区域营收如何")
    a_events = fallback_events(run_id="degrade_run_a")
    assert a_events

    from app.core.agents.data_analyst.state import AgentState
    from app.core.agents.data_analyst import graph as g

    fresh = AgentState(session_id="degrade_run_b", user_query="q")
    g._attach_llm_fallbacks(fresh)  # 未发生任何降级的新 run
    assert fresh.metadata.get("degraded") is False
    assert fresh.metadata.get("llm_fallbacks") == []


def test_error_is_truncated(real_mode_env):
    from app.infrastructure.llm.router import OpenAILLM, record_fallback

    record_fallback("analyst", "x" * 5000)
    evt = fallback_events()[-1]
    assert len(evt["error"]) <= 300


# --------------------------------------------------------------------------- #
# 2. 主动 mock 不是降级
# --------------------------------------------------------------------------- #
def test_mock_mode_is_not_degraded(mock_mode_env):
    from app.core.agents.data_analyst.graph import run_analysis

    state = run_analysis("degrade_mock", "各区域营收如何")
    assert state.status == "FINISH"
    assert state.metadata.get("degraded") is False, "配置就是 mock，不算降级"
    assert state.metadata.get("llm_fallbacks") == []


# --------------------------------------------------------------------------- #
# 3. API 暴露
# --------------------------------------------------------------------------- #
def test_analyze_response_exposes_degradation(real_mode_env):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    body = c.post("/api/v1/chat/analyze",
                  json={"query": "各区域营收如何", "session_id": "degrade_api"}).json()
    assert body["degraded"] is True, body
    assert body["llm_fallbacks"], body
    assert {"stage", "error"} <= set(body["llm_fallbacks"][0])


def test_stream_event_exposes_degradation(real_mode_env):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    with c.stream("POST", "/api/v1/chat/analyze/stream",
                  json={"query": "各区域营收如何", "session_id": "degrade_sse"}) as r:
        text = "".join(r.iter_text())
    assert '"degraded": true' in text, "SSE 事件必须透出降级标志"


def test_health_reports_observed_degradation(real_mode_env):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    c.post("/api/v1/chat/analyze", json={"query": "各区域营收如何", "session_id": "degrade_h"})
    h = c.get("/api/v1/health").json()
    assert h["llm_mode"] == "real", h          # 配置意图
    assert h["llm_degraded"] is True, h        # 观测事实（D18 的坑：以前只看配置）
    assert h["llm_fallbacks_total"] >= 1
    assert h["llm_last_error"]


def test_health_llm_probe_does_not_fallback(real_mode_env):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    probe = c.get("/api/v1/health/llm").json()
    assert probe["reachable"] is False
    assert probe["error"]
    assert fallback_events() == [], "探测自身不得写入降级事件（否则探测被 mock 骗过）"


# --------------------------------------------------------------------------- #
# 4. 运行态守卫（铁律 3 落地到运行时）
# --------------------------------------------------------------------------- #
def test_no_fallback_setting_raises_instead_of_mocking(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "false")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("LLM_BASE_URL", _UNREACHABLE)
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("LLM_TIMEOUT_S", "2")
    monkeypatch.setenv("LLM_NO_FALLBACK", "true")
    _patch_openai_failure(monkeypatch)
    get_settings.cache_clear()
    reset_llm()
    reset_fallback_events()

    from app.infrastructure.llm.router import get_llm

    with pytest.raises(Exception):
        get_llm().complete("s", "u", stage="analyst")

    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 5. schema 契约
# --------------------------------------------------------------------------- #
def test_schema_fields_exist():
    from app.models.schemas import AnalyzeResponse, HealthResponse

    r = AnalyzeResponse(session_id="s", status="FINISH")
    assert r.degraded is False and r.llm_fallbacks == []
    h = HealthResponse()
    assert h.llm_mode in ("mock", "real")
    assert h.llm_degraded is False
    assert h.llm_fallbacks_total == 0
