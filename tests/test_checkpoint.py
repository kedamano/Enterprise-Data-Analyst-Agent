"""Checkpoint: run persistence & session resume."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.checkpoint import load, save
from app.core.agents.data_analyst.graph import resume_analysis, run_analysis
from app.core.agents.data_analyst.state import AgentState
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def cp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ckpt"))
    get_settings.cache_clear()
    reset_llm()
    yield tmp_path / "ckpt"
    get_settings.cache_clear()
    reset_llm()


def test_run_analysis_persists_checkpoint(cp_env):
    state = run_analysis("cp_run1", "分析最近营收变化的原因，按地区维度下钻")
    assert state.status == "FINISH"
    target = cp_env / "cp_run1.json"
    assert target.exists(), "运行结束应落 checkpoint"
    loaded = load("cp_run1")
    assert loaded is not None
    assert loaded.session_id == "cp_run1"
    assert loaded.user_query == "分析最近营收变化的原因，按地区维度下钻"
    assert (loaded.report or "") == (state.report or "")


def test_save_load_roundtrip(cp_env):
    s = AgentState(session_id="cp_rt", user_query="q", status="REPORT")
    save(s)
    loaded = load("cp_rt")
    assert loaded is not None and loaded.status == "REPORT"
    assert loaded.user_query == "q"


def test_resume_finish_short_circuits(cp_env):
    s1 = resume_analysis("cp_res_ok", "分析各区域营收表现")
    assert s1.status == "FINISH"
    # FINISH 的会话再次 resume 直接复用，不再重跑
    s2 = resume_analysis("cp_res_ok")
    assert s2.status == "FINISH" and s2.session_id == "cp_res_ok"


def test_resume_retries_unfinished_session_with_stored_goal(cp_env):
    s = AgentState(session_id="cp_broken", user_query="分析营收下滑", status="ERROR",
                   error="上游超时")
    save(s)
    state = resume_analysis("cp_broken")  # 不带 query → 用 checkpoint 目标续跑
    assert state.status == "FINISH", state.error
    assert state.user_query == "分析营收下滑"
