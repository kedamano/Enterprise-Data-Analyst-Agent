"""E5/03 分析交付物导出：分析师要能把结果**一次拿走**（报告/SQL/数据/溯源）。

Spec: docs/specs/E5/03-export.md
"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def export_env(monkeypatch, tmp_path):
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


def _run(session_id: str):
    from app.core.agents.data_analyst.graph import run_analysis

    return run_analysis(session_id, "分析各区域营收")


def _zip_of(client, session_id: str, fmt: str = "zip") -> dict[str, bytes]:
    r = client.get(f"/api/v1/chat/analyze/export/{session_id}?format={fmt}")
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# --------------------------------------------------------------------------- #
# 1. 交付包内容
# --------------------------------------------------------------------------- #
def test_export_zip_contains_deliverables(export_env):
    from fastapi.testclient import TestClient

    from app.main import app

    _run("exp_ok")
    files = _zip_of(TestClient(app), "exp_ok")

    assert "report.md" in files, files.keys()
    assert "queries.sql" in files
    assert "README.txt" in files
    assert "trace.json" in files
    assert any(n.startswith("data/") and n.endswith(".csv") for n in files), files.keys()

    readme = files["README.txt"].decode("utf-8")
    assert "脱敏" in readme, "README 必须说明导出物不做脱敏（避免误读）"
    assert "原始值" in readme or "不脱敏" in readme


def test_exported_sql_is_readonly(export_env):
    from fastapi.testclient import TestClient

    from app.core.tools.sql_tool import guard_readonly_sql
    from app.main import app

    _run("exp_sql")
    files = _zip_of(TestClient(app), "exp_sql")
    sql_text = files["queries.sql"].decode("utf-8")

    assert "SELECT" in sql_text.upper()
    statements = [ln for ln in sql_text.splitlines()
                  if ln.strip() and not ln.strip().startswith("--")]
    assert statements, sql_text
    for stmt in statements:
        assert guard_readonly_sql(stmt) is None, f"导出物里出现了非只读语句: {stmt}"


def test_exported_csv_keeps_raw_values(export_env, monkeypatch, tmp_path):
    """E4/02 的上下文脱敏不影响导出物（分析师本机产物要可用）。"""
    import sqlite3

    db = tmp_path / "crm.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE c (id INTEGER, phone TEXT)")
    con.executemany("INSERT INTO c VALUES (?,?)", [(1, "13812345678")])
    con.commit()
    con.close()
    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("MASK_LEVEL", "sample")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.core.agents.data_analyst.graph import run_analysis
    from app.main import app

    st = run_analysis("exp_pii", "看看 c 表")
    # 上下文侧已掩码
    rows = [r for r in st.tool_results if (r.output or {}).get("rows")]
    assert rows and rows[-1].output["rows"][0].get("phone") == "138****5678", rows[-1].output

    files = _zip_of(TestClient(app), "exp_pii")
    csv_text = b"".join(v for k, v in files.items() if k.endswith(".csv")).decode("utf-8")
    assert "13812345678" in csv_text, "导出 CSV 必须是原始值"


# --------------------------------------------------------------------------- #
# 2. 单文件格式
# --------------------------------------------------------------------------- #
def test_export_sql_and_report_formats(export_env):
    from fastapi.testclient import TestClient

    from app.main import app

    _run("exp_fmt")
    c = TestClient(app)

    r = c.get("/api/v1/chat/analyze/export/exp_fmt?format=sql")
    assert r.status_code == 200 and "text/plain" in r.headers["content-type"]
    assert "step_" in r.text or "SELECT" in r.text.upper()

    r2 = c.get("/api/v1/chat/analyze/export/exp_fmt?format=report")
    assert r2.status_code == 200 and "markdown" in r2.headers["content-type"]
    assert "报告" in r2.text


def test_export_unknown_session_404(export_env):
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/api/v1/chat/analyze/export/no_such_session")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 3. 安全：路径白名单
# --------------------------------------------------------------------------- #
def test_export_skips_paths_outside_session_workdir(export_env, tmp_path):
    """伪造一个越界 artifact：绝不进包，README 记明跳过。"""
    from fastapi.testclient import TestClient

    from app.core.agents.data_analyst.checkpoint import load as cp_load, save as cp_save
    from app.main import app

    st = _run("exp_traverse")
    outside = tmp_path / "secret.csv"
    outside.write_text("secret\n42\n", encoding="utf-8")

    reloaded = cp_load("exp_traverse") or st
    if reloaded.tool_results:
        reloaded.tool_results[0].artifacts = list(reloaded.tool_results[0].artifacts or []) + [str(outside)]
    cp_save(reloaded)

    files = _zip_of(TestClient(app), "exp_traverse")
    assert "data/secret.csv" not in files
    assert not any(b"secret\n42" == v for v in files.values()), "越界文件内容不得进包"
    assert "跳过" in files["README.txt"].decode("utf-8")


# --------------------------------------------------------------------------- #
# 4. 边界：无产物也要能导出
# --------------------------------------------------------------------------- #
def test_export_without_data_artifacts(export_env):
    from fastapi.testclient import TestClient

    from app.core.agents.data_analyst.checkpoint import save as cp_save
    from app.core.agents.data_analyst.state import AgentState
    from app.main import app

    st = AgentState(session_id="exp_empty", user_query="一句话回答", status="FINISH")
    st.report = "# 数据分析报告：一句话回答\n\n没有查询。"
    cp_save(st)

    files = _zip_of(TestClient(app), "exp_empty")
    assert "report.md" in files, "无数据产物也必须能导出报告"
    assert "无" in files["README.txt"].decode("utf-8")


# --------------------------------------------------------------------------- #
# 5. 前端契约（源码级：防"只做后端漏按钮"）
# --------------------------------------------------------------------------- #
def test_frontend_has_export_button():
    from pathlib import Path

    chat = Path("web/src/components/ChatMessage.tsx").read_text(encoding="utf-8")
    assert "analyze/export/" in chat, "报告卡必须有导出入口（E5/03）"
    assert "format=zip" in chat, "默认导出交付包（zip）"

    app = Path("web/src/App.tsx").read_text(encoding="utf-8")
    assert "sessionId={active.id}" in app, "导出 URL 需要会话 id"
