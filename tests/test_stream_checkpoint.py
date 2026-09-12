"""EXPORT/01 回归：流式（SSE）路径必须在终端态落 checkpoint。

背景（真实缺陷）：前端唯一入口是 ``POST /chat/analyze/stream``（走 ``stream_analysis``）。
此前 ``stream_analysis`` 只在生成器里 yield、从不调用 ``checkpoint_save``，
而 ``run_analysis``（同步路径）有保存。后果是流式跑完一次分析后：

  - ``GET /chat/analyze/export/{sid}``   → 404「未找到运行记录」
  - ``GET /chat/analyze/trace/{sid}``    → 404
  - ``GET /chat/analyze/artifacts/{sid}``→ 404
  - ``resume_analysis()`` 无 checkpoint 可续

界面上「导出」按钮仍然渲染（报告在 SSE 载荷里），**点了必失败**——这是主 UI 路径的
功能缺口，由 Playwright E2E（web/e2e/export.spec.ts）真跑时暴露。

本文件把这些契约固化，防止回归。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def stream_env(monkeypatch, tmp_path):
    """确定性离线环境：MOCK_LLM + 独立 checkpoint 目录（不污染 data/）。"""
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


def _drain_stream(session_id: str, query: str = "分析各区域营收") -> list:
    """完整消费流式生成器，返回所有快照。"""
    from app.core.agents.data_analyst.graph import stream_analysis

    return list(stream_analysis(session_id, query))


# --------------------------------------------------------------------------- #
# 1. 核心回归：流式跑完必须落 checkpoint
# --------------------------------------------------------------------------- #
def test_stream_analysis_persists_checkpoint(stream_env):
    """流式路径终端态必须落盘——这正是此前的缺陷点。"""
    from app.core.agents.data_analyst.checkpoint import exists, load

    snaps = _drain_stream("sc_persist")
    assert snaps, "流式应至少产出一个快照"
    assert snaps[-1].status in ("FINISH", "FAILED", "ERROR", "CLARIFY")

    assert exists("sc_persist"), "流式跑完后 checkpoint 必须存在（否则 export/trace 全 404）"
    state = load("sc_persist")
    assert state is not None
    assert state.status == snaps[-1].status


# --------------------------------------------------------------------------- #
# 2. 端到端：流式跑完后导出/溯源/产物端点真的可用
# --------------------------------------------------------------------------- #
def test_export_available_after_stream(stream_env):
    """流式 → GET /analyze/export?format=zip 必须 200 且是真 zip（PK 魔数）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    _drain_stream("sc_export")
    client = TestClient(app)

    r = client.get("/api/v1/chat/analyze/export/sc_export?format=zip")
    assert r.status_code == 200, r.text[:200]
    assert r.content[:2] == b"PK", "导出物应是 zip"
    assert r.headers.get("content-type", "").startswith("application/zip")


def test_trace_and_artifacts_available_after_stream(stream_env):
    from fastapi.testclient import TestClient

    from app.main import app

    _drain_stream("sc_trace")
    client = TestClient(app)

    assert client.get("/api/v1/chat/analyze/trace/sc_trace").status_code == 200
    assert client.get("/api/v1/chat/analyze/artifacts/sc_trace").status_code == 200


def test_export_report_and_sql_formats_after_stream(stream_env):
    from fastapi.testclient import TestClient

    from app.main import app

    _drain_stream("sc_fmt")
    client = TestClient(app)

    rep = client.get("/api/v1/chat/analyze/export/sc_fmt?format=report")
    assert rep.status_code == 200
    assert "markdown" in rep.headers.get("content-type", "")
    assert rep.text.strip(), "报告格式导出不应为空"

    sql = client.get("/api/v1/chat/analyze/export/sc_fmt?format=sql")
    assert sql.status_code == 200


# --------------------------------------------------------------------------- #
# 3. 语义守卫：中途中断（非终端态）不得落盘半截状态
# --------------------------------------------------------------------------- #
def test_partial_stream_does_not_persist(stream_env):
    """只取首帧就关闭生成器 → 状态仍是非终端态 → 不应写 checkpoint。

    否则用户刷新页面/断连会留下一个"半成品"被当成可导出的成品。
    """
    from app.core.agents.data_analyst.checkpoint import exists
    from app.core.agents.data_analyst.graph import stream_analysis

    gen = stream_analysis("sc_partial", "分析各区域营收")
    first = next(gen)
    assert first.status == "INIT", first.status
    gen.close()  # 模拟客户端断连（触发 GeneratorExit → finally）

    assert not exists("sc_partial"), "非终端态不应落 checkpoint"


# --------------------------------------------------------------------------- #
# 4. 与同步路径一致性：两条路径都落盘
# --------------------------------------------------------------------------- #
def test_sync_and_stream_paths_both_persist(stream_env):
    from app.core.agents.data_analyst.checkpoint import exists
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis("sc_sync", "分析各区域营收")
    assert exists("sc_sync"), "同步路径（run_analysis）必须落盘"

    _drain_stream("sc_stream2")
    assert exists("sc_stream2"), "流式路径必须落盘"
