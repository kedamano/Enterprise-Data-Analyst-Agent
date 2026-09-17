"""流式路径异常兜网回归（与同步 run_analysis 对齐）。

背景（真实缺陷）：同步路径 ``run_analysis`` 用 try/except 把编排异常收敛成
``status=ERROR``，并在注释里写明「否则一次调用就把调用方整体打挂」。
但 UI 唯一入口 ``POST /chat/analyze/stream`` 走的 ``stream_analysis`` **没有**这层网：

    planner 抛 ModelOutputError（推理模型 thinking 吃满 max_tokens → 空正文）
      → 异常穿透生成器 → ASGI "Exception in ASGI application"
      → SSE 连接直接死掉，前端收不到任何错误帧，只能表现成"卡住/断连"

修复后：流式路径捕获异常、落一帧 ``status=ERROR`` 再正常结束，
调用方（SSE）能把这个错误渲染给用户。

另注：``GeneratorExit``（客户端断连）是 ``BaseException``，不会被这层网吞掉——
断连时应当安静退出，而不是再往一个已关闭的连接写帧。
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


def _boom(*_a, **_k):
    """复刻 planner 的真实故障：推理模型空正文 → ModelOutputError。"""
    from app.core.agents.data_analyst.state import ModelOutputError

    raise ModelOutputError("planner 输出不可用（已重试 1 次）: 模型返回空内容")


def test_stream_yields_error_frame_instead_of_raising(stream_env, monkeypatch):
    """核心回归：编排节点抛异常时，流式**产出 ERROR 帧**，而不是把异常抛给 ASGI。"""
    from app.core.agents.data_analyst import graph

    monkeypatch.setattr(graph, "run_planner", _boom)

    snaps = list(graph.stream_analysis("en_planner_boom", "统计各区域营收"))

    assert snaps, "至少要产出快照（含 ERROR 帧）"
    assert snaps[-1].status == "ERROR", (
        "异常必须收敛成 status=ERROR 的最后一帧，而不是让生成器抛出去"
    )
    assert "ModelOutputError" in (snaps[-1].error or ""), snaps[-1].error
    assert snaps[-1].metadata.get("aborted_by_exception") == "ModelOutputError"


def test_stream_error_frame_is_terminal_and_survives_checkpoint(stream_env, monkeypatch):
    """ERROR 属终端态：应照常落 checkpoint，让 export/trace 可用（对齐同步路径）。"""
    from app.core.agents.data_analyst import checkpoint, graph

    monkeypatch.setattr(graph, "run_planner", _boom)

    snaps = list(graph.stream_analysis("en_ck", "统计各区域营收"))

    assert snaps[-1].status == "ERROR"
    assert checkpoint.exists("en_ck"), (
        "ERROR 是终端态，checkpoint 必须落盘（否则用户无法导出/溯源这次失败）"
    )
    assert checkpoint.load("en_ck").status == "ERROR"


def test_stream_does_not_swallow_generator_exit(stream_env, monkeypatch):
    """客户端断连（GeneratorExit）是 BaseException，不能被兜网吞掉。

    吞掉会导致：连接已断，代码还试图往里 yield 一帧 → RuntimeError
    「generator ignored GeneratorExit」，把正常断连变成一条假错误日志。
    """
    from app.core.agents.data_analyst import graph

    monkeypatch.setattr(graph, "run_planner", _boom)

    gen = graph.stream_analysis("en_exit", "统计各区域营收")
    next(gen)  # 推进到 INIT 之后的第一个 yield
    with pytest.raises(GeneratorExit):
        gen.throw(GeneratorExit)
