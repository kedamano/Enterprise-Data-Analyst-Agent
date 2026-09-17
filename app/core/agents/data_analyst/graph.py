"""Analysis orchestration driver.

``run_analysis`` is a framework-agnostic state-machine that walks the spec's
pipeline:

    Context → Planner → [Executor*] → Analyst → Reflection
                                    └─ REPLAN ─→ Planner
                       Reflection → PASS → Reporter → FINISH

It is the single entry point used by the API layer. A streaming variant
(``stream_analysis``) yields a snapshot after every node for progressive UIs.
An optional LangGraph ``build_graph`` is provided for teams that want the graph
visualised / deployed on a LangGraph server (requires ``langgraph``).
"""
from __future__ import annotations

import logging
import uuid
from typing import AsyncGenerator, Iterator

from ....config import get_settings
from ...interfaces import trace_run  # A: 经 core.interfaces 中转，解耦 infrastructure.observability
from .checkpoint import load as checkpoint_load, save as checkpoint_save
from .state import AgentState

from .nodes import (  # noqa: E402
    iter_executor_all,
    run_analyst,
    run_context,
    run_executor,
    run_executor_all,
    run_planner,
    run_reflection,
    run_reporter,
)

logger = logging.getLogger("da.graph")


def _new_state(session_id: str, user_query: str, history: list | None = None,
               force_full_rerun: bool = False,
               skill_ids: list[str] | None = None) -> AgentState:
    from ....core.attachments import get_attachment_store
    _sid = session_id or ""
    try:
        attach_ctx = get_attachment_store().describe(_sid)
    except Exception:
        attach_ctx = ""
    # ATTACH/01：把附件清单（文件名/列名/行数/样例）并入用户问题，供 Planner 感知
    effective_query = user_query
    if attach_ctx:
        effective_query = f"{user_query}\n\n{attach_ctx}"
    state = AgentState(
        session_id=_sid or uuid.uuid4().hex,
        user_query=effective_query,
        attachment_context=attach_ctx,
        conversation_history=history or [],
        force_full_rerun=force_full_rerun,
        max_replans=get_settings().max_replans,
    )
    # Skills：把**用户本次勾选**的技能正文渲染进 metadata，由 run_context 注入提示词。
    # 只认"仍存在且已启用"的技能（禁用=不参与），并受 skill_max_per_request 封顶，
    # 防止一次全选把上下文撑爆。技能层任何故障都不打断主流水线。
    _attach_skills(state, skill_ids)
    return state


def _attach_skills(state: AgentState, skill_ids: list[str] | None) -> None:
    if not skill_ids:
        return
    try:
        from ....core.skills import get_skill_store

        store = get_skill_store()
        max_n = int(getattr(get_settings(), "skill_max_per_request", 8) or 8)
        valid: list[str] = []
        for sid in skill_ids:
            if not sid or sid in valid:
                continue
            item = store.get(sid, include_body=False)
            if item is not None and item.get("enabled", True):
                valid.append(sid)
            if len(valid) >= max_n:
                break
        text = store.render(valid)
        if text:
            state.metadata["skill_ids"] = valid
            state.metadata["skills_text"] = text
    except Exception:
        return  # 技能是增强项，坏了不能拖垮分析


# 连续相同 REPLAN（同计划步骤 + 同 replan_objectives）达到此数视为无进展，提前 FAIL
_REPLAN_MAX_STALL = 2


def _replan_key(state: AgentState) -> tuple:
    plan_tools = tuple(s.tool for s in (state.plan.steps if state.plan else []))
    objs = tuple(state.reflection.replan_objectives) if state.reflection else ()
    return (plan_tools, objs)


def _stall_after_replan(state: AgentState, prev_key: tuple | None,
                        stall: int) -> tuple[bool, tuple | None, int]:
    """REPLAN 之后是否判"无进展"。返回 ``(failed, prev_key, stall)``。

    同步与流式**共用**本函数——此前只有同步版有停滞检测，同一场景下两条路径
    结局不同（流式会空转到 safety 上限才 FAIL）。E4/04 的门禁 REPLAN 带的是
    数据事实缺口、且目标文本随事实变化，故**豁免**计数。
    """
    if state.metadata.get("gate_issues"):
        return False, _replan_key(state), stall
    key = _replan_key(state)
    stall = stall + 1 if key == prev_key else 0
    return stall >= _REPLAN_MAX_STALL, key, stall

def _resolve_route(state: AgentState) -> AgentState:
    """ROUTE：输出意图 → mode + ExecutionPlan（供同步/流式共用）。"""
    try:
        from ....core.agents.data_analyst.modes import build_plan, detect_mode
        state.mode = detect_mode(state.user_query, state.context)
        state.intent = build_plan(state.user_query)
    except Exception:
        state.mode = "full"
    return state


def _try_iteration(state: AgentState) -> AgentState | None:
    """E3：命中「基于上一结果」且有会话数据集 → 增量执行（否则 None 回退全链）。

    E3/02：``force_full_rerun`` 为真直接回退；守卫不通过（如改期越界）也回退，
    并把原因写入 ``state.metadata["iteration_skip"]`` 供审计。
    """
    try:
        from ....core.agents.data_analyst.iteration import (
            deliver_iteration, is_followup, load_last_dataset,
        )
        if getattr(state, "force_full_rerun", False):
            state.metadata["iteration_skip"] = "force_full_rerun"
            return None
        if not is_followup(state.user_query):
            return None
        if not (getattr(state, "last_dataset", None) or load_last_dataset(state.session_id)):
            return None
        return deliver_iteration(state)
    except Exception as exc:
        state.metadata["iteration_skip"] = str(exc) or exc.__class__.__name__
        return None


def _attach_llm_fallbacks(state: AgentState) -> AgentState:
    """DEGRADE/01：把**本轮**（同 run_id）的 LLM 降级事件挂到 state.metadata。

    让 API/SSE 能明确说出"这次分析其实降级兜底了"，而不是靠人去读报告正文。
    """
    try:
        from ...interfaces import fallback_events  # A: 经 core.interfaces 中转，解耦 infrastructure.llm.router

        events = fallback_events(run_id=state.session_id)
    except Exception:
        events = []
    state.metadata["llm_fallbacks"] = events
    state.metadata["degraded"] = bool(events)
    return state


def _drive_sync(state: AgentState) -> AgentState:
    """Context → Planner → Executor* → Analyst → Reflection（REPLAN→Planner）→ Reporter."""
    state.status = "INIT"
    state = run_context(state)
    if state.status in ("ERROR", "CLARIFY"):
        # CLARIFY/01：澄清是一次对话回合的终点——等用户回答，不进 planner
        return _attach_llm_fallbacks(state)
    _resolve_route(state)
    _it = _try_iteration(state)
    if _it is not None:
        return _it

    prev_key: tuple | None = None
    stall = 0
    safety = 0
    while safety < 10:
        safety += 1
        state = run_planner(state)
        # python_code：模型真写码 → 沙箱验证 → 交付（避免 over-execute 报表）
        if state.mode == "python_code":
            try:
                from ....core.agents.data_analyst.pycode import deliver_python_code
                return deliver_python_code(state)
            except Exception:
                state.mode = "full"
        # execute all planned steps（P1-1：按依赖波次并发执行，单步时退化为顺序）
        state = run_executor_all(state)
        # ROUTE 轻模式：SQL/简短问答 → 裁剪 Analyst/Reflection/Reporter 重链
        if state.mode in ("sql_only", "quick_answer"):
            try:
                from ....core.agents.data_analyst.modes import terminal_quick, terminal_sql_only
                from .rigor import ensure_dq_disclosure
                return ensure_dq_disclosure(
                    terminal_sql_only(state) if state.mode == "sql_only" else terminal_quick(state))
            except Exception as exc:  # 轻终端故障则回退重链，绝不丢结果
                state.mode = "full"
                state.error = f"轻模式终态降级: {exc}"
        state = run_analyst(state)
        state = run_reflection(state)
        if state.status == "REPORT":
            return run_reporter(state)
        if state.status in ("FAILED", "ERROR"):
            # produce a best-effort report even on failure
            return run_reporter(state)
        # REPLAN → 无进展检测：连续同计划+同目标的 REPLAN 说明在空转，提前 FAIL
        if state.status == "REPLAN":
            failed, prev_key, stall = _stall_after_replan(state, prev_key, stall)
            if failed:
                state.status = "FAILED"
                state.error = "无进展循环：连续多次 REPLAN 的计划与目标均未改变"
                return run_reporter(state)
            continue
        # REPLAN → loop back to planner
    state.status = "FAILED"
    state.error = "超过最大编排步数"
    return state


def run_analysis(session_id: str, user_query: str, history: list | None = None,
                 force_full_rerun: bool = False,
                 skill_ids: list[str] | None = None) -> AgentState:
    """Run the full analysis pipeline, emitting a persisted structured trace."""
    from .response_cache import enabled as _cache_enabled, get_cached, put_cached

    # INTERVIEW/01 ④：请求级缓存——同会话同问直接命中，省下整条链的 LLM 调用。
    # force_full_rerun 是"用户显式要求重算"，绕过缓存并刷新它。
    if _cache_enabled() and not force_full_rerun and session_id:
        cached = get_cached(session_id, user_query)
        if cached:
            try:
                hit = AgentState.model_validate(cached)  # 重建新对象，避免共享可变状态
                hit.metadata["cache_hit"] = True
                hit.metadata["cache_key"] = "resp"
                return hit
            except Exception:
                pass  # 反序列化失败 → 当作未命中

    state = _new_state(session_id, user_query, history, force_full_rerun, skill_ids)
    with trace_run(run_id=state.session_id):
        # 编排节点抛出的异常（畸形模型输出、上游 5xx、依赖缺失…）**必须**收敛成
        # status=ERROR，而不是裸穿透 `run_analysis`：否则一次调用就把调用方（API / eval
        # runner）整体打挂 —— 实测一次真实 eval 重跑因此丢掉了全部 7 条已跑基线。
        # 这里是**最后一道网**；能用状态表达的错误应在各节点内处理。
        try:
            final = _drive_sync(state)
        except Exception as exc:  # noqa: BLE001 — 兜网必须宽
            logger.exception("分析流水线异常终止")
            state.status = "ERROR"
            state.error = f"{type(exc).__name__}: {exc}"
            state.metadata["aborted_by_exception"] = type(exc).__name__
            final = state
    _attach_llm_fallbacks(final)  # DEGRADE/01：降级对调用方可见
    checkpoint_save(final)  # 可回放/可续跑（best-effort）
    if _cache_enabled() and final.status == "FINISH":
        put_cached(final.session_id, user_query, final)
    return final


def resume_analysis(session_id: str, user_query: str | None = None,
                    history: list | None = None) -> AgentState:
    """Resume/retry a session.

    * No checkpoint or already FINISH → run fresh (same session).
    * An unfinished checkpoint (ERROR/FAILED…) is retried under the same
      session id; the memory layer carries the prior objective/results, so the
      retry effectively continues from where the run left off.
    """
    state = checkpoint_load(session_id)
    if state is not None and state.status == "FINISH":
        return state
    if state is not None and state.status == "CLARIFY" and not (user_query or "").strip():
        # CLARIFY/01：没有新信息就原样返回澄清状态；否则会重跑 Context → 再次反问（死循环）
        return state
    query = (user_query or (state.user_query if state else "")).strip()
    if not query:
        raise ValueError("resume_analysis 需要 user_query 或已存在的会话目标")
    return run_analysis(session_id, query, history)


# 终端态：到达这些状态后本次运行不再继续，需要落盘（与 run_analysis 一致）
_TERMINAL_STATUSES = frozenset({"FINISH", "FAILED", "ERROR", "CLARIFY"})


def stream_analysis(session_id: str, user_query: str, history: list | None = None,
                    force_full_rerun: bool = False,
                    skill_ids: list[str] | None = None) -> Iterator[AgentState]:
    """Yield a state snapshot after each node, wrapped in a persisted trace run.

    EXPORT/01：流式路径同样必须在终端态落 checkpoint。此前 ``stream_analysis``
    只在生成器里 yield、从不调 ``checkpoint_save``，导致前端（唯一走 SSE 的入口）
    跑完一次分析后，``/analyze/export`` ``/analyze/trace`` ``/analyze/artifacts``
    以及 ``resume_analysis`` 全部 404——界面上"导出"按钮点了必失败。
    这里用 try/finally 兜底：正常跑完、抛错、甚至客户端中途断连（GeneratorExit），
    只要已经推进到终端态就保存；中途中断（非终端态）不保存，避免半截状态被当成成品。
    """
    from .response_cache import enabled as _cache_enabled, put_cached

    last: AgentState | None = None
    try:
        for snap in _stream_analysis_inner(session_id, user_query, history,
                                           force_full_rerun, skill_ids):
            last = snap
            yield snap
    except Exception as exc:  # noqa: BLE001 — 兜网必须宽
        # 与同步路径 run_analysis（本文件上方 try/except 收敛为 status=ERROR）对齐。
        # 流式路径此前**没有**这层网：planner 抛 ModelOutputError 会直接穿透 ASGI，
        # SSE 连接死掉、前端收不到任何错误帧（只有"连接已断开"）。
        # GeneratorExit / 客户端断连是 BaseException，不会被这里吞掉。
        logger.exception("流式分析流水线异常终止")
        st = last if last is not None else _new_state(
            session_id, user_query, history, force_full_rerun, skill_ids)
        st.status = "ERROR"
        st.error = f"{type(exc).__name__}: {exc}"
        st.metadata["aborted_by_exception"] = type(exc).__name__
        last = _attach_llm_fallbacks(st)
        yield last
    finally:
        if last is not None and last.status in _TERMINAL_STATUSES:
            checkpoint_save(last)  # 可回放/可续跑（best-effort）
            if _cache_enabled():
                try:
                    put_cached(last.session_id, user_query, last)
                except Exception:
                    pass


def _stream_analysis_inner(session_id: str, user_query: str, history: list | None = None,
                           force_full_rerun: bool = False,
                           skill_ids: list[str] | None = None) -> Iterator[AgentState]:
    """Yield a state snapshot after each node, wrapped in a persisted trace run."""
    state = _new_state(session_id, user_query, history, force_full_rerun, skill_ids)
    with trace_run(run_id=state.session_id):
        state.status = "INIT"
        yield state
        state = run_context(state)
        _resolve_route(state)
        yield _attach_llm_fallbacks(state)
        if state.status in ("ERROR", "CLARIFY"):
            # 澄清帧同样要 attach（否则"降级后反问"时降级不可见）
            return
        _it = _try_iteration(state)
        if _it is not None:
            yield _attach_llm_fallbacks(_it)
            return

        safety = 0
        prev_key: tuple | None = None
        stall = 0
        while safety < 10:
            safety += 1
            state = run_planner(state)
            yield _attach_llm_fallbacks(state)
            # python_code：模型真写码 → 沙箱验证 → 交付
            if state.mode == "python_code":
                try:
                    from ....core.agents.data_analyst.pycode import deliver_python_code
                    state = deliver_python_code(state)
                    yield _attach_llm_fallbacks(state)
                    return
                except Exception:
                    state.mode = "full"
            # execute all planned steps（P1-1：按依赖波次并发执行）
            # 流式路径用逐步版本：每个工具步骤完成即 yield 一帧 EXECUTE 快照。
            # 此前用 run_executor_all（黑盒）→ SSE 永远没有 EXECUTE 帧，
            # 前端「执行完成 · 0 个工具」、时间线无工具步骤卡片，均源于此。
            for _exec_snap in iter_executor_all(state):
                yield _attach_llm_fallbacks(_exec_snap)
            # sql_only / quick_answer：裁剪 Analyst/Reflection/Reporter 重链
            if state.mode in ("sql_only", "quick_answer"):
                try:
                    from ....core.agents.data_analyst.modes import terminal_quick, terminal_sql_only
                    from .rigor import ensure_dq_disclosure
                    state = ensure_dq_disclosure(
                        terminal_sql_only(state) if state.mode == "sql_only"
                        else terminal_quick(state))
                    yield _attach_llm_fallbacks(state)
                    return
                except Exception:
                    state.mode = "full"
            state = run_analyst(state)
            yield _attach_llm_fallbacks(state)
            state = run_reflection(state)
            yield _attach_llm_fallbacks(state)
            if state.status == "REPORT":
                state = run_reporter(state)
                yield _attach_llm_fallbacks(state)
                return
            if state.status in ("FAILED", "ERROR"):
                state = run_reporter(state)
                yield _attach_llm_fallbacks(state)
                return
            # 无进展检测：与 _drive_sync 共用同一判定，避免两条路径结局不一致
            if state.status == "REPLAN":
                failed, prev_key, stall = _stall_after_replan(state, prev_key, stall)
                if failed:
                    state.status = "FAILED"
                    state.error = "无进展循环：连续多次 REPLAN 的计划与目标均未改变"
                    state = run_reporter(state)
                    yield _attach_llm_fallbacks(state)
                    return
        state.status = "FAILED"
        yield state


async def stream_analysis_async(session_id: str, user_query: str, history: list | None = None,
                                force_full_rerun: bool = False,
                                skill_ids: list[str] | None = None) -> AsyncGenerator[AgentState, None]:
    for snap in stream_analysis(session_id, user_query, history, force_full_rerun, skill_ids):
        yield snap


# --------------------------------------------------------------------------- #
# Optional LangGraph definition
# --------------------------------------------------------------------------- #
def build_graph():
    """Build a LangGraph ``StateGraph`` mirroring the manual driver.

    Requires ``langgraph``. The manual ``run_analysis`` above is the default
    path; this exists for teams that deploy the graph on a LangGraph server or
    want to visualise / add checkpointers.
    """
    try:
        from langgraph.graph import StateGraph, END
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("langgraph 未安装，无法构建图；请使用 run_analysis") from exc

    g = StateGraph(AgentState)

    def _ctx(s): return run_context(s)
    def _plan(s): return run_planner(s)
    def _exec(s): return run_executor(s)
    def _ana(s): return run_analyst(s)
    def _refl(s): return run_reflection(s)
    def _rep(s): return run_reporter(s)

    g.add_node("context", _ctx)
    g.add_node("planner", _plan)
    g.add_node("executor", _exec)
    g.add_node("analyst", _ana)
    g.add_node("reflection", _refl)
    g.add_node("reporter", _rep)

    g.set_entry_point("context")
    g.add_edge("context", "planner")

    def _after_exec(s):
        return "analyst" if s.status in ("ANALYZE",) else "executor"

    g.add_conditional_edges("executor", _after_exec)
    g.add_edge("planner", "executor")
    g.add_edge("analyst", "reflection")

    def _after_refl(s):
        if s.status == "REPORT":
            return "reporter"
        if s.status == "REPLAN":
            return "planner"
        return END

    g.add_conditional_edges("reflection", _after_refl)
    g.add_edge("reporter", END)
    return g.compile()
