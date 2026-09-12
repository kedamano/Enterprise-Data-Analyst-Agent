"""Sandbox data hand-off: python_analysis must actually load the SQL-produced CSV.

Regression for a bug found on D17: the CSV path handed to the sandbox is
relative (``data/artifacts/<session>/s1.csv``), but the subprocess runs with
``cwd=<session workdir>`` — so ``pd.read_csv`` silently failed and ``df`` became
``None``. The Mock LLM's script guards with ``if df is not None``, which is why
the whole suite stayed green while real models would have crashed on ``df``.
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.tools import execute_tool, session_workdir
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def subprocess_sandbox(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("PYTHON_SANDBOX_MODE", "subprocess")  # 不走 docker
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _write_source_csv(session_id: str):
    import pandas as pd

    path = session_workdir(session_id) / "s1.csv"   # 与 sql_query 物化路径同形（相对）
    pd.DataFrame({"region_id": [1, 2], "revenue": [10, 20]}).to_csv(path, index=False)
    return path


def test_sandbox_loads_relative_source_csv(subprocess_sandbox):
    sid = "sandbox_handoff"
    path = _write_source_csv(sid)
    code = "print(_json.dumps({'rows': None if df is None else int(df.shape[0])}))"

    res = execute_tool("s1", "python_analysis", {"code": code, "data_csv": str(path)}, sid)

    assert res.status == "SUCCESS", (res.error, (res.output or {}).get("stderr"))
    assert (res.output or {}).get("parsed") == {"rows": 2}, res.output


def test_sandbox_can_write_csv_artifact(subprocess_sandbox):
    """增量/写码产物落盘：相对路径的产物必须写进 session workdir。"""
    sid = "sandbox_artifact"
    path = _write_source_csv(sid)
    code = ("df.groupby('region_id', as_index=False)['revenue'].sum()"
            ".to_csv('derived.csv', index=False)\nprint('ok')")

    res = execute_tool("s1", "python_analysis", {"code": code, "data_csv": str(path)}, sid)

    assert res.status == "SUCCESS", (res.error, (res.output or {}).get("stderr"))
    assert (session_workdir(sid) / "derived.csv").exists()


def test_sandbox_failure_reports_stderr_in_error(subprocess_sandbox):
    """失败必须带可读原因（自纠错回注依赖 ToolResult.error，不能是 None）。"""
    sid = "sandbox_fail"
    path = _write_source_csv(sid)
    code = "raise ValueError('boom-boom')"

    res = execute_tool("s1", "python_analysis", {"code": code, "data_csv": str(path)}, sid)

    assert res.status == "FAILED"
    assert "boom-boom" in (res.error or ""), (res.error, res.output)
