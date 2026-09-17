"""UI/EXECUTE 帧回归：流式（SSE）路径必须产出逐步 EXECUTE 快照。

背景（真实缺陷）：前端「执行完成 · N 个工具」的 N = SSE 流里
``status=="EXECUTE" 且带 step`` 的帧数。此前执行器 ``run_executor_all``
是黑盒：执行完全部工具后把 status 推进为 ANALYZE 才返回，SSE 只拿到
这一个终态快照 —— 工具明明执行了，前端计数却恒为「0 个工具」，
时间线里也没有任何工具步骤卡片。

修复：``_executor_all_steps``（逐步快照内核）+ 两条路径共用：
  - ``run_executor_all``   同步薄壳（消费内核、丢弃中间快照，@trace 语义不变）
  - ``iter_executor_all``  流式入口（每步一帧，手动维护 executor span）
本文件固化契约，防止回归。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def stream_env(monkeypatch, tmp_path):
    """与 test_stream_checkpoint 相同的确定性离线环境。"""
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


def test_stream_emits_execute_frames_with_steps(stream_env):
    """SSE 快照流必须包含 EXECUTE 帧，且每帧带最后完成的步骤结果。

    前端统计口径：exec = [e for e in events if e.status=="EXECUTE" and e.step]，
    「N 个工具」= len(exec)。此断言与 web/src/components/StageTimeline.tsx 一致。

    ⚠️ 必须逐帧**立即**断言：快照是同一 AgentState 的原地演进（与 SSE 消费端
    逐帧序列化的模式一致），list() 收集引用只会看到最终状态。
    """
    from app.core.agents.data_analyst.graph import stream_analysis

    exec_count = 0
    plan_steps = 0
    final_status = ""
    for snap in stream_analysis("ex_frames", "分析各区域营收"):
        if snap.status == "EXECUTE":
            exec_count += 1
            assert snap.tool_results, "EXECUTE 帧应至少带一个已完成的工具结果"
        if snap.plan and snap.plan.steps:
            plan_steps = len(snap.plan.steps)
        final_status = snap.status

    assert exec_count > 0, "流式路径必须产出 EXECUTE 帧（此前恒为 0 → UI 显示 0 个工具）"
    if plan_steps:
        assert exec_count >= plan_steps, "EXECUTE 帧数应 ≥ 计划步骤数（每步至少一帧）"
    assert final_status in ("FINISH", "FAILED", "ERROR", "CLARIFY")


def test_iter_executor_all_frame_count_matches_tool_results(stream_env):
    """iter_executor_all 的 EXECUTE 帧数 = 最终 tool_results 的步骤数（前端口径）。"""
    from app.core.agents.data_analyst.graph import stream_analysis
    from app.core.agents.data_analyst.nodes import iter_executor_all

    # 用流式跑到执行器前，再单独驱动执行器逐步快照
    gen = stream_analysis("ex_iter", "分析各区域营收")
    state = None
    for snap in gen:
        state = snap
        if state.plan and state.plan.steps:
            break
    gen.close()
    assert state is not None

    exec_count = 0
    last_status = ""
    for snap in iter_executor_all(state):
        last_status = snap.status  # 同一对象原地演进，只记最终值
        if snap.status == "EXECUTE":
            exec_count += 1
    done_steps = {r.step_id for r in state.tool_results}
    assert exec_count >= len(done_steps) > 0
    assert last_status == "ANALYZE", "执行器流式结束必须推进到 ANALYZE"


def test_sync_run_executor_all_semantics_unchanged(stream_env):
    """同步薄壳（run_analysis 主路径用）行为不变：终态 ANALYZE、结果完整。"""
    from app.core.agents.data_analyst.graph import run_analysis

    final = run_analysis("ex_sync", "分析各区域营收")
    # 执行器之后的节点（analyst 等）会继续改 status，这里只验工具结果被完整保留
    assert final.tool_results, "同步路径工具结果不应为空"
