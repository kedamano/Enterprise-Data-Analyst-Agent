"""CLARIFY/01 澄清回路：模糊请求应当反问，而不是 ERROR 终止。

Spec: docs/specs/CLARIFY/01-clarification-loop.md
来源：基准里 38/300 涉口径澄清、19 条是"该不该"的决策题——真实分析师会反问一句；
而旧实现 `status=ERROR` + `error="需要澄清: …"` 直接死掉，问题还没留在结构化字段里。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.state import AgentState, ContextModel
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

_CLARIFY_CTX = json.dumps({
    "objective": "分析营收变化",
    "metrics": ["营收"],
    "clarification_required": True,
    "clarification_questions": ["对比的是去年同期还是上月？", "营收是否含退款？"],
    "assumptions": [],
})
_RESOLVED_CTX = json.dumps({
    "objective": "分析营收变化",
    "metrics": ["营收"],
    "clarification_required": False,
    "clarification_questions": [],
    "assumptions": ["营收不含退款", "对比去年同期"],
})


@pytest.fixture
def clarify_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


# --------------------------------------------------------------------------- #
# 1. 节点：CLARIFY 而不是 ERROR
# --------------------------------------------------------------------------- #
def test_context_returns_clarify_state(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_context

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    st = run_context(AgentState(session_id="cl_node", user_query="营收怎么样"))

    assert st.status == "CLARIFY", st.status
    assert st.error is None, "澄清不是错误"
    assert st.context.objective, "结构化 context 必须先落盘（旧实现在赋值前就 return 了）"
    assert st.metadata["clarification"]["questions"] == ["对比的是去年同期还是上月？", "营收是否含退款？"]


def test_pending_clarification_is_persisted(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_context

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    run_context(AgentState(session_id="cl_pending", user_query="营收怎么样"))

    pending = short_term.get("cl_pending", "pending_clarification")
    assert pending and pending["questions"], pending
    assert pending["original_query"] == "营收怎么样"


# --------------------------------------------------------------------------- #
# 2. 编排：早退（不进 planner）
# --------------------------------------------------------------------------- #
def test_sync_does_not_reach_planner(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    called = {"planner": 0}
    monkeypatch.setattr(g, "run_planner",
                        lambda s: (called.__setitem__("planner", called["planner"] + 1), s)[1])

    st = g.run_analysis("cl_early", "营收怎么样")
    assert st.status == "CLARIFY"
    assert called["planner"] == 0, "澄清必须早退，不该继续规划"


def test_stream_last_frame_is_clarify_with_degrade_field(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    snaps = list(g.stream_analysis("cl_stream", "营收怎么样"))
    last = snaps[-1]
    assert last.status == "CLARIFY"
    # DEGRADE/01 的 attach 不能在 CLARIFY 早退时被跳过
    assert "degraded" in last.metadata


# --------------------------------------------------------------------------- #
# 3. 续跑：不循环、答完能跑完
# --------------------------------------------------------------------------- #
def test_answer_resumes_and_clears_pending(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    calls = {"n": 0}

    def _llm(stage, user, json_mode=True):
        if stage != "context":
            return nodes.get_llm().complete("", user, stage=stage, json_mode=json_mode)
        calls["n"] += 1
        return _CLARIFY_CTX if calls["n"] == 1 else _RESOLVED_CTX

    monkeypatch.setattr(nodes, "_llm", _llm)
    first = g.run_analysis("cl_resume", "营收怎么样")
    assert first.status == "CLARIFY"

    second = g.run_analysis("cl_resume", "和去年同期比，不含退款")
    assert second.status == "FINISH", (second.status, second.error)
    assert short_term.get("cl_resume", "pending_clarification") is None, "答完后必须清除 pending"
    assert second.context.assumptions, "回答应被合并进上下文"


def test_context_payload_carries_pending(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.nodes as nodes
    from app.core.agents.data_analyst.nodes import run_context

    seen: list[str] = []

    def _llm(stage, user, json_mode=True):
        seen.append(user)
        return _CLARIFY_CTX if len(seen) == 1 else _RESOLVED_CTX

    monkeypatch.setattr(nodes, "_llm", _llm)
    run_context(AgentState(session_id="cl_payload", user_query="营收怎么样"))
    run_context(AgentState(session_id="cl_payload", user_query="和去年同期比"))

    assert "pending_clarification" in seen[1], "续跑时必须把待澄清问题回注给 context"


def test_three_rounds_force_progress(monkeypatch, clarify_env):
    """连续要求澄清要有上限，绝不无限反问。"""
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    for i in range(3):
        st = g.run_analysis("cl_rounds", f"营收怎么样 {i}")
    assert st.status != "CLARIFY", "第 3 轮必须按已有信息推进，而不是继续反问"
    assert st.metadata.get("clarify_rounds", 0) >= 3


# --------------------------------------------------------------------------- #
# 4. resume_analysis 的 CLARIFY 语义（旧实现会死循环）
# --------------------------------------------------------------------------- #
def test_resume_returns_clarify_without_new_query(monkeypatch, clarify_env):
    import app.core.agents.data_analyst.graph as g
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    g.run_analysis("cl_resume2", "营收怎么样")

    again = g.resume_analysis("cl_resume2")
    assert again.status == "CLARIFY", "无新信息时应原样返回澄清状态，而不是重跑再反问一遍"
    assert again.metadata["clarification"]["questions"]


# --------------------------------------------------------------------------- #
# 5. API / SSE 契约
# --------------------------------------------------------------------------- #
def test_api_exposes_clarification(monkeypatch, clarify_env):
    from fastapi.testclient import TestClient

    import app.core.agents.data_analyst.nodes as nodes
    import app.main as main

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: _CLARIFY_CTX)
    body = TestClient(main.app).post(
        "/api/v1/chat/analyze",
        json={"query": "营收怎么样", "session_id": "cl_api"}).json()

    assert body["status"] == "CLARIFY"
    assert body["clarification"]["questions"], body
    assert body["error"] is None


def test_sse_frame_carries_clarification(monkeypatch, clarify_env):
    from fastapi.testclient import TestClient

    import app.api.routes.chat as chat
    import app.main as main

    st = AgentState(session_id="cl_sse", user_query="q", status="CLARIFY")
    st.metadata["clarification"] = {"questions": ["含退款吗？"], "assumptions": []}
    monkeypatch.setattr(chat, "stream_analysis", lambda *a, **kw: iter([st]))

    with TestClient(main.app).stream("POST", "/api/v1/chat/analyze/stream",
                                     json={"query": "q"}) as r:
        text = "".join(r.iter_text())
    assert "含退款吗" in text and "需要澄清" in text


def test_analyze_request_accepts_clarification_answer():
    from app.models.schemas import AnalyzeRequest, AnalyzeResponse

    assert AnalyzeRequest(query="q").clarification_answer is None
    assert AnalyzeRequest(query="q", clarification_answer="不含退款").clarification_answer == "不含退款"
    assert AnalyzeResponse(session_id="s", status="CLARIFY").clarification is None


# --------------------------------------------------------------------------- #
# 6. 前端契约（源码级——防"只改后端漏前端"，尤其是 isTerminal）
# --------------------------------------------------------------------------- #
_WEB = __import__("pathlib").Path("web/src")


def _read(rel: str) -> str:
    return (_WEB / rel).read_text(encoding="utf-8")


def test_frontend_marks_clarify_as_terminal():
    """最易漏的一处：isTerminal 不含 CLARIFY → SSE 结束后界面永久转圈。"""
    api = _read("lib/api.ts")
    block = api.split("export function isTerminal", 1)[1].split("}", 1)[0]
    assert "CLARIFY" in block, "isTerminal 必须把 CLARIFY 当作终止态"


def test_frontend_labels_and_card_exist():
    api = _read("lib/api.ts")
    assert "CLARIFY:" in api or 'CLARIFY":' in api, "stageLabel 需要 CLARIFY 文案"
    assert "clarification" in api, "AgentEvent 需要 clarification 字段"

    assert (_WEB / "components/ClarifyCard.tsx").exists(), "缺少澄清卡片组件"
    chat = _read("components/ChatMessage.tsx")
    assert "ClarifyCard" in chat, "卡片必须真的被渲染"

    app = _read("App.tsx")
    assert "clarification" in app and "CLARIFY" in app, "App 需要消费 CLARIFY 事件"


def test_frontend_does_not_render_clarify_as_error():
    app = _read("App.tsx")
    block = app.split("error:", 1)[1].split("text:", 1)[0]
    assert 'ev.status === "ERROR" || ev.status === "FAILED"' in block, \
        "澄清不是错误，不得写进 error 字段"


def test_clarification_answer_is_consumed():
    from fastapi.testclient import TestClient

    from app.api.routes.chat import _effective_query
    from app.models.schemas import AnalyzeRequest

    assert _effective_query(AnalyzeRequest(query="q")) == "q"
    merged = _effective_query(AnalyzeRequest(query="q", clarification_answer="不含退款"))
    assert "不含退款" in merged
    assert TestClient.__name__  # 保持导入（路由层同源）
