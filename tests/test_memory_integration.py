"""TDD suite for Memory × orchestration integration (spec §20).

Contract under test:

* After an analysis finishes, the terminal node persists:
  - short-term: ``last_analysis`` summary + bounded ``history`` per session
  - long-term (FINISH only, non-sensitive): objective + finding statements
* On the next request (same session), ``run_context`` recalls both stores and
  injects them into the Context Resolver payload (conversation context).
* Memory failures must never break the pipeline.
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.graph import run_analysis
from app.core.agents.data_analyst.nodes import run_context
from app.core.agents.data_analyst.state import AgentState
from app.core.memory import long_term, short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mock_llm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    # 长期记忆写临时文件，避免污染 data/long_term.jsonl
    # （改用配置项而非 monkeypatch 模块常量：路径已可配，见 INTERVIEW/01 §1）
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "long_term.jsonl"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    reset_llm()


def test_memory_persists_after_analysis(mock_llm_env):
    sid = "mem_1"
    state = run_analysis(sid, "分析最近半年华北地区营收下滑的原因，按产品维度下钻")
    assert state.status == "FINISH", state.error

    # 短期记忆：会话摘要
    last = short_term.get(sid, "last_analysis")
    assert last and last["status"] == "FINISH"
    assert last["objective"], "应记录分析目标"
    assert isinstance(last["findings"], list) and last["findings"], "应记录发现"

    # 短期记忆：历史（有界）
    history = short_term.get(sid, "history")
    assert history and history[-1]["query"].startswith("分析最近半年")
    assert len(history) <= 20

    # 长期记忆：非敏感摘要入库（FINISH 才写）
    entries = [json.loads(ln) for ln in long_term._path().read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert entries, "FINISH 后应写入长期记忆"
    e = entries[-1]
    assert e["type"] == "analysis_summary"
    assert e["session_id"] == sid
    assert e["objective"]
    assert all(k not in e for k in ("api_key", "password", "credential", "secret"))


def test_memory_recalled_into_next_context(mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    sid = "mem_2"
    run_analysis(sid, "分析最近半年华北地区营收下滑的原因，按产品维度下钻")

    # 同会话第二次请求：Context Resolver 的输入应包含两份记忆
    captured: dict[str, str] = {}

    def spy_llm(stage: str, user: str, json_mode: bool = True) -> str:
        captured["user"] = user
        return json.dumps({"objective": "继续分析", "metrics": ["revenue"], "dimensions": [],
                           "clarification_required": False})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(nodes, "_llm", spy_llm)
    try:
        state = AgentState(session_id=sid, user_query="继续上次的营收分析")
        run_context(state)
    finally:
        monkeypatch.undo()

    assert "short_term_memory" in captured["user"], "短期记忆应注入 context payload"
    assert "long_term_memory" in captured["user"], "长期记忆召回应注入 context payload"
    # 注入的是结构化数据块，且包含上一轮的目标
    assert "营收下滑" in captured["user"]


def test_new_session_has_empty_memory_not_error(mock_llm_env):
    import app.core.agents.data_analyst.nodes as nodes

    captured: dict[str, str] = {}

    def spy_llm(stage: str, user: str, json_mode: bool = True) -> str:
        captured["user"] = user
        return json.dumps({"objective": "新分析", "metrics": [], "dimensions": [],
                           "clarification_required": False})

    m = pytest.MonkeyPatch()
    m.setattr(nodes, "_llm", spy_llm)
    try:
        state = AgentState(session_id="brand_new_session_xyz", user_query="全新问题")
        run_context(state)
    finally:
        m.undo()
    assert "short_term_memory" in captured["user"]  # 空记忆也注入（空结构），不报错
