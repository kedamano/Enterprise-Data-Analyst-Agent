"""P1-1 并行化编排：依赖无关的计划步骤按「波次」并发执行。

把原来的逐步骤 while 循环换成 ``run_executor_all``（拓扑分波 + 线程池）。
锁死：
1. 同一波内互相独立的步骤**真正并发**（峰值并发 >= 2）。
2. 依赖顺序严格保持（s1 → s2/s3 → s4）。
3. ``parallel_executor_workers <= 1`` 时退化为严格顺序（与原行为一致）。
4. 某步失败后，其后续步骤得到 FAILED 占位，不会无限循环。
5. 真实 sleep.csv 端到端：并行执行器仍能命中用户上传表并 FINISH。
"""
from __future__ import annotations

import threading
import time

from app.core.agents.data_analyst.state import (
    AgentState,
    PlanModel,
    PlanStep,
    ToolResult,
)


def _make_plan() -> PlanModel:
    """s1(无依赖) → s2,s3(依赖s1) → s4(依赖s2,s3)。s2/s3 互为独立，可并发。"""
    return PlanModel(goal="g", steps=[
        PlanStep(id="s1", objective="o", action="a", tool="sql_query", dependencies=[]),
        PlanStep(id="s2", objective="o", action="a", tool="sql_query", dependencies=["s1"]),
        PlanStep(id="s3", objective="o", action="a", tool="sql_query", dependencies=["s1"]),
        PlanStep(id="s4", objective="o", action="a", tool="sql_query",
                 dependencies=["s2", "s3"]),
    ])


def test_independent_steps_run_in_parallel(monkeypatch):
    import app.core.agents.data_analyst.nodes as nd

    active: list[str] = []
    lock = threading.Lock()
    peak = {"v": 0}

    def fake_execute(step_id, tool, params, session_id):
        with lock:
            active.append(step_id)
            peak["v"] = max(peak["v"], len(active))
        time.sleep(0.05)  # 让并发窗口可被观测
        with lock:
            active.remove(step_id)
        return ToolResult(step_id=step_id, tool=tool, status="SUCCESS", result={"ok": True})

    monkeypatch.setattr(nd, "execute_tool", fake_execute)
    monkeypatch.setattr(nd, "build_executor_params", lambda state, step: {})

    st = AgentState(session_id="p11", user_query="q", plan=_make_plan())
    nd.run_executor_all(st)

    ids = {r.step_id for r in st.tool_results if r.status == "SUCCESS"}
    assert ids == {"s1", "s2", "s3", "s4"}, ids
    # s2/s3 同波并发 → 峰值并发达到 2
    assert peak["v"] >= 2, f"并行未发生，峰值并发={peak['v']}"
    order = [r.step_id for r in st.tool_results if r.status == "SUCCESS"]
    assert order.index("s1") < order.index("s2")
    assert order.index("s2") < order.index("s4")
    assert order.index("s3") < order.index("s4")


def test_sequential_fallback_when_workers_le(monkeypatch):
    import app.core.agents.data_analyst.nodes as nd

    order: list[str] = []

    def fake_execute(step_id, tool, params, session_id):
        order.append(step_id)
        return ToolResult(step_id=step_id, tool=tool, status="SUCCESS", result={})

    class _S:
        parallel_executor_workers = 1  # 关闭并行

    monkeypatch.setattr(nd, "get_settings", lambda: _S())
    monkeypatch.setattr(nd, "execute_tool", fake_execute)
    monkeypatch.setattr(nd, "build_executor_params", lambda state, step: {})

    st = AgentState(session_id="p11b", user_query="q", plan=_make_plan())
    nd.run_executor_all(st)

    assert order == ["s1", "s2", "s3", "s4"], order  # 严格顺序，无并发波次
    assert len(st.tool_results) == 4


def test_failed_dependency_blocks_dependents(monkeypatch):
    """s1 成功，s2 失败 → s3(依赖s1) 仍可跑；s4(依赖s2,s3) 因 s2 失败被占位 FAILED。"""
    import app.core.agents.data_analyst.nodes as nd

    def fake_execute(step_id, tool, params, session_id):
        status = "FAILED" if step_id == "s2" else "SUCCESS"
        return ToolResult(step_id=step_id, tool=tool, status=status,
                          error="boom" if status == "FAILED" else None)

    monkeypatch.setattr(nd, "execute_tool", fake_execute)
    monkeypatch.setattr(nd, "build_executor_params", lambda state, step: {})

    st = AgentState(session_id="p11c", user_query="q", plan=_make_plan())
    nd.run_executor_all(st)

    by_id = {r.step_id: r for r in st.tool_results}
    assert by_id["s1"].status == "SUCCESS"
    assert by_id["s2"].status == "FAILED"
    assert by_id["s3"].status == "SUCCESS"      # 依赖 s1（成功）→ 仍执行
    assert by_id["s4"].status == "FAILED"       # 依赖 s2（失败）→ 占位，不卡死
    assert "依赖步骤未完成" in (by_id["s4"].error or "")


def test_end_to_end_sleep_with_parallel_executor():
    """真实 sidecar + 上传数据，经并行执行器后各步骤成功命中 upload.<t>。

    不走完整 run_analysis（依赖 LLM/凭证，环境不稳定），而是直接构造确定性
    的「附件回退计划」并用并行执行器跑——这正是 P1-1 的职责边界。
    """
    import uuid

    from app.core.agents.data_analyst.nodes import (
        _attachment_plan,
        run_executor_all,
    )
    from app.core.attachments import (
        attached_tables,
        build_preview,
        get_attachment_store,
    )

    sid = "p11-e2e-" + uuid.uuid4().hex[:10]
    store = get_attachment_store()
    store.clear(sid)

    csv = (
        b"person_id,gender,occupation,sleep_quality,stress,sleep_disorder\n"
        b"1,1,Office Worker,6,7,None\n"
        b"2,2,Student,7,4,Insomnia\n"
        b"3,1,Retired,8,3,None\n"
        b"4,2,Office Worker,5,8,Insomnia\n"
        b"5,1,Student,6.5,6,None\n"
        b"6,2,Office Worker,4,9,Insomnia\n"
    )
    store.put(sid, build_preview(csv, "sleep.csv"))
    try:
        uploaded = attached_tables(sid)
        assert uploaded, "上传表应已可查询"
        st = AgentState(session_id=sid, user_query="这份文件反应了什么数据规律")
        st.plan = _attachment_plan(st, uploaded)
        # s1_rows → {s2_by_dim, s2b_by_dim2, s3_profile} 互相独立，应同波并发
        assert [s.id for s in st.plan.steps][:1] == ["s1_rows"]

        run_executor_all(st)

        by_id = {r.step_id: r for r in st.tool_results}
        # 各步骤成功 == 并行执行器正确命中 upload.<t> 边车库（否则 SQL 会失败）
        assert by_id["s1_rows"].status == "SUCCESS"
        assert by_id["s2_by_dim"].status == "SUCCESS"
        assert by_id["s3_profile"].status == "SUCCESS"
        # 第二维度交叉验证步骤存在且成功（独立步骤，验证并发波次非空）
        assert by_id["s2b_by_dim2"].status == "SUCCESS"
    finally:
        store.clear(sid)
