"""INTERVIEW/01 ② `build_graph()` 的测试覆盖。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §2

此前 `build_graph()` 只有实现、**零测试**——"声明了 LangGraph 支持"却没验证过，
属于典型的"说了没做"。这里补齐：节点齐全 + 能编译 + 用 mock LLM 真跑到 FINISH。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

langgraph = pytest.importorskip("langgraph", reason="langgraph 未安装 → 显式跳过（不计入通过）")


@pytest.fixture
def graph_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def test_build_graph_compiles_with_expected_nodes(graph_env):
    from app.core.agents.data_analyst.graph import build_graph

    compiled = build_graph()
    assert compiled is not None

    # 节点齐全：与手写主路径一致（context/planner/executor/analyst/reflection/reporter）
    node_names = set(compiled.get_graph().nodes)
    for expected in ("context", "planner", "executor", "analyst", "reflection", "reporter"):
        assert expected in node_names, f"缺节点 {expected}；实际 {sorted(node_names)}"


def test_build_graph_runs_to_finish_with_mock(graph_env):
    """**真跑一遍**：LangGraph 路径也必须能走完并拿到报告（不只是能 compile）。"""
    from app.core.agents.data_analyst.graph import build_graph
    from app.core.agents.data_analyst.state import AgentState

    compiled = build_graph()
    # 手写主路径里 executor 会循环到 current_step_index 走完；图路径依赖状态里的 index
    out = compiled.invoke(AgentState(session_id="lg_run", user_query="分析各区域营收"))

    state = out if isinstance(out, AgentState) else AgentState.model_validate(out)
    assert state.status in ("FINISH", "CLARIFY"), (state.status, state.error)
    if state.status == "FINISH":
        assert state.report, "跑完必须有报告"
        assert state.tool_results, "必须真的执行过工具"


def test_build_graph_raises_readable_error_without_langgraph(monkeypatch, graph_env):
    """缺少 langgraph 时必须给出**可读**错误（而不是 ImportError 裸抛）。"""
    import builtins

    import app.core.agents.data_analyst.graph as g

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.startswith("langgraph"):
            raise ImportError("simulated: langgraph 缺失")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(RuntimeError) as exc:
        g.build_graph()
    assert "langgraph" in str(exc.value) and "run_analysis" in str(exc.value), exc.value
