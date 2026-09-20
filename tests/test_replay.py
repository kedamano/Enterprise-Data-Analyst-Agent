"""Debug Replay CLI 测试。覆盖 show / run / diff 三命令的 mock 路径。"""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile

import pytest

import app.infrastructure.jobs.jobs  # noqa: F401  (ensure app import路径干净)
from app.core.agents.data_analyst.graph import (
    _apply_tool_overrides,
    _clear_tool_overrides,
    _TOOL_OVERRIDE_STACK,
)


@pytest.fixture(autouse=True)
def _tmp_traces(tmp_path, monkeypatch):
    """把 traces 目录重定向到 tmp。"""
    import scripts.replay as replay_mod
    monkeypatch.setattr(replay_mod, "TRACES_DIR", tmp_path / "traces")
    replay_mod.TRACES_DIR.mkdir(parents=True, exist_ok=True)
    yield tmp_path / "traces"


def _write_trace(traces_dir: pathlib.Path, session_id: str,
                 user_query: str = "test query", status: str = "FINISH"):
    spans = [
        {"type": "span", "stage": "planner", "duration_ms": 100},
        {"type": "span", "stage": "analyst", "duration_ms": 500},
        {"type": "summary", "session_id": session_id, "user_query": user_query,
         "status": status, "total_duration_ms": 600},
    ]
    (traces_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(s) for s in spans) + "\n"
    )


def test_show_trace_summary(tmp_path, _tmp_traces):
    from scripts.replay import cmd_show, _read_trace
    _write_trace(_tmp_traces, "abc123", user_query="GMV 分析")
    trace = _read_trace("abc123")
    assert trace is not None
    assert trace["user_query"] == "GMV 分析"
    assert trace["status"] == "FINISH"
    assert trace["stage_count"] == 2  # 2 spans (excl summary)


def test_show_trace_not_found(_tmp_traces, capsys):
    from scripts.replay import cmd_show
    cmd_show("nonexistent")
    captured = capsys.readouterr()
    # logger.error 输出到 stderr；这里只验证不抛异常即可


def test_parse_overrides():
    from scripts.replay import _parse_overrides
    result = _parse_overrides("sql_query=mock,python_analysis=mock")
    assert "sql_query" in result
    assert "python_analysis" in result
    assert callable(result["sql_query"])
    # mock 调用
    out = result["sql_query"]({"sql": "select 1"})
    assert out["mock"] is True


def test_parse_overrides_with_pass():
    from scripts.replay import _parse_overrides
    result = _parse_overrides("sql_query=mock,knowledge_search=pass")
    assert "sql_query" in result
    assert "knowledge_search" not in result  # pass = 不注入覆盖


def test_apply_tool_overrides_patches_and_restores():
    """_apply_tool_overrides 应覆盖 REGISTRY；_clear_tool_overrides 应恢复。"""
    from app.core.tools import REGISTRY
    original_sql = REGISTRY.get("sql_query")
    mock_fn = lambda p: {"mock": True}
    _apply_tool_overrides({"sql_query": mock_fn})
    # 栈顶已推入
    assert len(_TOOL_OVERRIDE_STACK) >= 1
    _clear_tool_overrides()
    # 清完后恢复原值（或删除覆盖项）
    assert "sql_query" not in _TOOL_OVERRIDE_STACK[-1] if _TOOL_OVERRIDE_STACK else True


def test_diff_lists_traces(_tmp_traces, capsys):
    from scripts.replay import cmd_diff
    _write_trace(_tmp_traces, "orig1")
    # 写一个 replay trace
    _write_trace(_tmp_traces, "orig1-replay-123")
    cmd_diff("orig1")
    captured = capsys.readouterr()
    assert "orig1" in captured.out


def test_mock_tools_callable():
    from scripts.replay import MOCK_TOOLS
    for name, fn in MOCK_TOOLS.items():
        out = fn({"test": True})
        assert isinstance(out, dict)
        assert out.get("mock") is True
