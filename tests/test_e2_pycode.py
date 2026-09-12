"""E2 free-form Python: model-authored script → sandbox-validated → delivered."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.graph import run_analysis
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mock_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def test_python_code_delivered_and_sandbox_validated(mock_env):
    state = run_analysis("py_deliver", "帮我写个 Python 脚本分析各区域营收")
    assert state.status == "FINISH", state.error
    rep = state.report or ""
    assert "```python" in rep, "应交付 Python 代码"
    assert "沙箱验证通过" in rep, f"mock 下应沙箱验证通过：{rep[-160:]}"
    tools = {r.tool for r in state.tool_results}
    assert "python_analysis" in tools and "sql_query" in tools
    val = [r for r in state.tool_results if r.step_id == "pycode_val"]
    assert val and val[0].status == "SUCCESS"


def test_python_code_negative_guardrail_still_holds(mock_env):
    """模型若给出危险代码，沙箱应拒绝（回归 python_tool 守卫）。"""
    from app.core.tools import execute_tool

    res = execute_tool("neg1", "python_analysis", {"code": "import os\nos.getcwd()"}, "s")
    assert res.status == "FAILED" and "禁止导入" in (res.error or "")


class _FlakyGen:
    """第 1、2 次返回坏代码（引用不存在列），第 3 次返回可跑代码。"""
    def __init__(self):
        self.calls = []
        self.n = 0

    def complete(self, system, user, stage="", json_mode=False, temperature=None):
        self.n += 1
        self.calls.append(user)
        if self.n <= 2:
            return "print(df['no_such_col_xyz'])\n"
        return ("import pandas as pd\n"
                "summary = {'rows': int(df.shape[0]) if df is not None else 0}\n"
                "print(_json.dumps(summary))")

    def __call__(self, settings=None):
        return self


def test_python_self_corrects_after_failures(mock_env, monkeypatch):
    from app.core.agents.data_analyst.pycode import deliver_python_code
    from app.core.agents.data_analyst.state import AgentState, ContextModel
    import app.infrastructure.llm.router as router_mod

    gen = _FlakyGen()
    monkeypatch.setattr(router_mod, "get_llm", gen)

    state = AgentState(session_id="py_retry", user_query="分析各区域营收")
    state.context = ContextModel(objective="按区域分析营收")
    state = deliver_python_code(state, max_attempts=3)

    assert state.status == "FINISH"
    rep = state.report or ""
    assert "第 3 次尝试" in rep, f"应第三次通过：{rep[-160:]}"
    # 至少三次生成，且后两次都收到失败回注
    assert gen.n >= 3
    assert any("上次执行失败" in u and "[第 1 次]" in u for u in gen.calls[1:])
    # 验证执行轨迹里有 3 次 python 沙箱尝试
    pv = [r for r in state.tool_results if r.step_id == "pycode_val"]
    assert len(pv) == 3 and pv[-1].status == "SUCCESS"


def test_artifacts_endpoint_lists_sources(mock_env):
    from fastapi.testclient import TestClient
    from app.main import app

    state = run_analysis("py_art", "帮我写个 Python 脚本分析营收")
    with TestClient(app) as c:
        r = c.get(f"/api/v1/chat/analyze/artifacts/{state.session_id}")
    assert r.status_code == 200, r.text
    items = r.json()["artifacts"]
    assert any(it["step_id"] == "pycode_src" and it["artifacts"] for it in items), \
        "应有 sql 数据源产物(CSV)"


def test_sse_python_carries_custom_summary(mock_env):
    import json as _json
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        resp = c.post("/api/v1/chat/analyze/stream",
                      json={"query": "帮我写个 Python 脚本分析营收", "session_id": "py_sum"})
    fin = None
    for ln in resp.text.splitlines():
        if ln.startswith("data: ") and ln[6:] != "[DONE]":
            ev = _json.loads(ln[6:])
            if ev["status"] == "FINISH":
                fin = ev
    assert fin and fin.get("custom_summary")
    cs = fin["custom_summary"]
    assert cs["mode"] == "python_code" and cs["validated"] is True and cs["attempts"] >= 1
    assert cs["data_source"] == "sql"
