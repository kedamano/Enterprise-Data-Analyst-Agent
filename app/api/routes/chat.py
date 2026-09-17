from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...config import get_settings
from ...core.agents.data_analyst.graph import run_analysis, stream_analysis
from ...models.schemas import AnalyzeRequest, AnalyzeResponse
from ...core.agents.data_analyst.state import AgentStatus

router = APIRouter(prefix="/chat", tags=["chat"])


def _metric_cards(snap) -> list[dict] | None:
    """D51：指标卡数据（归一化在 `metric_cards`，**唯一口径**，与报告表格同源）。"""
    try:
        from ...core.agents.data_analyst.metric_cards import normalize_metrics

        return normalize_metrics(getattr(getattr(snap, "analysis", None), "metrics", None))
    except Exception:
        return None


def _to_response(state) -> AnalyzeResponse:
    findings = [f.model_dump() for f in state.analysis.findings]
    return AnalyzeResponse(
        session_id=state.session_id,
        status=state.status,
        mode=state.mode,
        iteration=state.iteration,
        # DEGRADE/01：本轮降级必须对调用方可见
        degraded=bool(state.metadata.get("degraded")),
        llm_fallbacks=state.metadata.get("llm_fallbacks") or [],
        quality_issues=state.metadata.get("gate_issues") or [],
        clarification=state.metadata.get("clarification"),
        cache_hit=bool(state.metadata.get("cache_hit")),
        report=state.report,
        objective=state.context.objective,
        plan_steps=[s.objective for s in state.plan.steps],
        tool_results=[r.model_dump() for r in state.tool_results],
        findings=findings,
        # D51：指标卡（REST 面与 SSE 用同一个归一化函数）
        metrics=_metric_cards(state) or [],
        reflection_decision=state.reflection.decision if state.reflection else None,
        confidence=state.reflection.confidence if state.reflection else None,
        error=state.error,
    )


def _effective_query(req: AnalyzeRequest) -> str:
    """CLARIFY/01：clarification_answer 是显式作答，并入本轮 query（否则该字段是死的）。"""
    if not req.clarification_answer:
        return req.query
    return f"{req.query}\n\n针对上一轮提问的回答：{req.clarification_answer}"


def _session_id_for(req: AnalyzeRequest) -> str:
    """AUTH/02：空 `session_id` 由服务端生成一个（随响应回传），而不是落到共享的 `default` 桶。

    此前用 `req.session_id or ""`，而附件层把空值归一成字面量 `default` ——
    于是**两个都不带 session_id 的调用方会共用同一个桶**（uploads/sidecar/数据集）。
    客户端拿得到生成值（`AnalyzeResponse.session_id` / SSE 的 FINISH 帧）就能继续用。
    """
    from ...core.attachments import resolve_session_id

    return resolve_session_id(req.session_id)


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    state = run_analysis(_session_id_for(req), _effective_query(req), req.history,
                         force_full_rerun=req.force_full_rerun, skill_ids=req.skill_ids)
    _record_owner(state.session_id)
    return _to_response(state)


def _record_owner(session_id: str) -> None:
    """AUTH/01：记下会话归属，供后续 export/trace/artifacts 校验（AUTH 关闭时为空操作）。"""
    try:
        from ...core.security.auth import current_principal, record_session_owner

        record_session_owner(session_id, current_principal())
    except Exception:
        pass


def _assert_session_access(session_id: str) -> None:
    """AUTH/01：越权读别人的会话 → 403（AUTH 关闭或匿名模式不校验）。"""
    try:
        from fastapi import HTTPException

        from ...core.security.auth import current_principal, owns_session

        principal = current_principal()
        if not owns_session(session_id, principal):
            raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception:
        pass


def _step_view(r) -> dict | None:
    """单步执行日志的精简视图（脱敏+截断），供 UI 逐步展开。"""
    if r is None:
        return None
    out = r.output or {}
    digest = ""
    if isinstance(out, dict):
        if out.get("rows"):
            digest = f"返回 {len(out['rows'])} 行"
            if out.get("csv_path"):
                import pathlib
                digest += f" → {pathlib.Path(out['csv_path']).name}"
        elif out.get("stdout"):
            digest = (out["stdout"] or "")[:120].replace("\n", " ")
        elif out.get("tables"):
            digest = f"发现 {len(out['tables'])} 张表"
        elif out.get("report"):
            digest = f"报告 {len(out['report'])} 字"
        elif out.get("ok") is False and out.get("error"):
            digest = (out["error"] or "")[:120]
    return {
        "step_id": r.step_id,
        "tool": r.tool,
        "status": r.status,
        "execution_time_ms": r.execution_time_ms,
        "attempts": getattr(r, "attempts", None),
        "error": (r.error or "")[:300] or None,
        "error_class": getattr(r, "error_class", None),
        "artifacts": len(r.artifacts or []),
        "digest": digest,
    }


@router.post("/analyze/stream")
def analyze_stream(req: AnalyzeRequest):
    """Server-Sent-Events stream: one event per orchestration node."""

    def gen():
        seen: set[str] = set()
        sid = _session_id_for(req)
        _record_owner(sid)
        for snap in stream_analysis(sid, _effective_query(req), req.history,
                                    force_full_rerun=req.force_full_rerun,
                                    skill_ids=req.skill_ids):
            last = snap.tool_results[-1] if snap.tool_results else None
            try:
                from ...core.agents.data_analyst.modes import status_milestones, workflow_progress
                seen |= status_milestones(snap.status, last)
                workflow = (snap.intent or {}).get("workflow", []) if snap.intent else []
                wprog = workflow_progress(workflow, seen)
            except Exception:
                wprog = None
            event = {
                "status": snap.status,
                "message": _status_message(snap.status),
                "objective": snap.context.objective,
                # ROUTE/02：初始阶段透出执行计划供 UI 可视化
                "intent": snap.intent if snap.status in ("INIT", "UNDERSTAND") else None,
                "mode": getattr(snap, "mode", None),
                # 精确 workflow 进度（语义步→milestone）
                "workflow_progress": wprog,
                "tool": (last.tool if last else None),
                "last_result_ok": (last.status == "SUCCESS") if last else None,
                # 逐步执行日志：EXECUTE 事件带该步明细
                "step": _step_view(last) if snap.status == "EXECUTE" else None,
                "report": snap.report if snap.status == "FINISH" else None,
                # D51：指标卡（只有 FINISH 帧带——中途帧带半成品，前端会当最终值渲染）
                "metrics": _metric_cards(snap) if snap.status == "FINISH" else None,
                # E1：FINISH 时附溯源覆盖率
                "trace_summary": _trace_summary(snap) if snap.status == "FINISH" else None,
                # E2：自定义写码产物摘要
                "custom_summary": _custom_summary(snap) if snap.status == "FINISH" else None,
                # E3/02：增量本轮信息（kind/stages/skips）
                "iteration": getattr(snap, "iteration", None) if snap.status == "FINISH" else None,
                # CLARIFY/01：需要用户回答的问题
                "clarification": (snap.metadata or {}).get("clarification")
                if snap.status == "CLARIFY" else None,
                # E4/04：质量门禁问题（REFLECT 起可见，FINISH 带全量）
                "quality_issues": ((snap.metadata or {}).get("gate_issues") or [])
                if snap.status in ("REFLECT", "REPLAN", "REPORT", "FINISH") else None,
                # DEGRADE/01：降级发生时即刻可见（不等最后），FINISH 带明细
                "degraded": bool((snap.metadata or {}).get("degraded")),
                "llm_fallbacks": ((snap.metadata or {}).get("llm_fallbacks") or [])
                if snap.status == "FINISH" else None,
                # P0-4：降级摘要（哪一级降了/为什么/影响什么）。
                # 不再只给"degraded: true"这种哑信号，也省去前端二次解析事件流。
                "degradation": _degradation_view(snap),
            }
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _degradation_view(snap) -> dict | None:
    """P0-4：把本轮降级聚合成可读摘要。无降级时返回 None（前端不显示条幅）。

    优先用 reporter 回写的 ``degradation_summary``（FINISH 帧已有）；
    更早的帧则从 metadata 的原始事件现算，保证"一发生就可见"。
    """
    try:
        from ...infrastructure.llm.degradation import summarize_degradation

        meta = snap.metadata or {}
        cached = meta.get("degradation_summary")
        if cached:
            return cached
        events = meta.get("llm_fallbacks")
        if not events:
            from ...infrastructure.llm.router import fallback_events

            events = fallback_events(run_id=snap.session_id)
        if not events:
            return None
        return summarize_degradation(events)
    except Exception:
        return None


def _trace_summary(snap) -> dict | None:
    try:
        from ...core.agents.data_analyst.sources import trace_manifest
        return trace_manifest(snap).get("coverage")
    except Exception:
        return None


def _custom_summary(snap) -> dict | None:
    """E2：自定义写码产物摘要（python 尝试次数/产物/数据源），UI 可展示。"""
    mode = getattr(snap, "mode", None)
    if mode not in ("python_code", "sql_only"):
        return None
    results = list(snap.tool_results or [])
    if mode == "python_code":
        attempts = [r for r in results if str(r.step_id).startswith("pycode_val")]
        src = [r for r in results if r.step_id == "pycode_src"]
        last = attempts[-1] if attempts else None
        return {
            "mode": mode,
            "attempts": len(attempts),
            "validated": (last.status == "SUCCESS") if last else False,
            "artifacts": (last.artifacts or []) if last else [],
            "data_source": "sql" if src else None,
        }
    return {"mode": mode}


@router.post("/analyze/confirm")
def analyze_confirm(payload: dict):
    """D45 两步授权：对挂起的高危动作落定（允许 / 拒绝）。

    请求体：``{"session_id": ..., "token": ..., "approved": true|false}``。
    凭证由 428 响应下发；**允许与拒绝都写审计**（`data/audit/hitl.jsonl`）。
    确认成功后该动作获得**一次性放行**——重试原请求即可，不会被反复拦住。
    """
    session_id = str((payload or {}).get("session_id") or "")
    token = str((payload or {}).get("token") or "")
    approved = bool((payload or {}).get("approved"))
    if not session_id or not token:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="缺少 session_id 或 token")

    # AUTH/01：越权确认别人的会话 → 403
    try:
        from ...core.security.auth import current_principal, owns_session

        if not owns_session(session_id, current_principal()):
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="无权访问该会话")
    except Exception:
        pass

    from ...core.security import hitl

    ok, message = hitl.decide(session_id, token, approved=approved)
    if not ok:
        # 凭证不匹配 / 无挂起动作 → 409（可重试：调用方应回到 428 重新发起）
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message}


@router.get("/analyze/artifacts/{session_id}")
def analyze_artifacts(session_id: str):
    _assert_session_access(session_id)
    """某次运行的工具产物与溯源清单（CSV/PNG/代码等）。"""
    try:
        from ...core.agents.data_analyst.checkpoint import load as cp_load
        from fastapi import HTTPException
    except ImportError:
        raise HTTPException(status_code=500, detail="依赖缺失") from None
    state = cp_load(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {session_id}")
    items = []
    for r in state.tool_results or []:
        if r.artifacts:
            items.append({"step_id": r.step_id, "tool": r.tool, "status": r.status,
                          "artifacts": list(r.artifacts)})
    return {"session_id": session_id, "artifacts": items}


@router.get("/analyze/chart/{session_id}/{name}")
def analyze_chart(session_id: str, name: str):
    """D51：取会话工作目录里的图（报告 `## 图表` 内嵌引用的图源）。

    两层防护：**文件名白名单**（400）+ **目录校验**（403）。只有 .png/.jpg 等位图，
    **不含 svg**——同源 inline 的 SVG 可带脚本，等于给自己开一个 XSS 面。
    """
    _assert_session_access(session_id)
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from ...core.agents.data_analyst import charts

    if not charts.is_safe_chart_name(name):
        raise HTTPException(status_code=400, detail=f"非法的图表文件名: {name}")
    path = charts.resolve_chart_file(session_id, name)
    if path is None:
        raise HTTPException(status_code=403, detail="图表不在该会话工作目录内")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"图表不存在: {name}")
    return FileResponse(str(path), media_type=charts.media_type_for(name))


@router.get("/analyze/trace/{session_id}")
def analyze_trace(session_id: str):
    _assert_session_access(session_id)
    """E1：某次运行的数字溯源清单（claim → SQL → 样本）。"""
    try:
        from ...core.agents.data_analyst.checkpoint import load as cp_load
        from ...core.agents.data_analyst.sources import trace_manifest
        from fastapi import HTTPException
    except ImportError:
        raise HTTPException(status_code=500, detail="依赖缺失") from None
    state = cp_load(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {session_id}")
    return trace_manifest(state)


@router.get("/analyze/lineage/{session_id}")
def analyze_lineage(session_id: str):
    """D47：**指标血缘**清单（metric → 口径 → sql_id → SQL → 表/列）。

    `parsed_from_sql` 表示表/列由 SQL **文本解析**，不是权威元数据——
    调用方不得当成 `information_schema` 那样的事实。
    """
    _assert_session_access(session_id)
    from fastapi import HTTPException

    from ...core.agents.data_analyst.checkpoint import load as cp_load
    from ...core.agents.data_analyst.lineage import lineage_summary, metric_lineage

    state = cp_load(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {session_id}")
    lineages = metric_lineage(state.analysis, list(state.tool_results or []))
    return {"session_id": session_id, "summary": lineage_summary(lineages),
            "metrics": lineages}


def _status_message(status: str) -> str:
    return {
        "INIT": "初始化",
        "UNDERSTAND": "解析业务意图",
        "PLAN": "制定分析计划",
        "EXECUTE": "执行工具获取数据",
        "ANALYZE": "分析证据",
        "REFLECT": "质检与反思",
        "REPLAN": "证据不足，重新规划",
        "REPORT": "生成业务报告",
        "FINISH": "完成",
        "CLARIFY": "需要澄清",
        "ERROR": "出错",
        "FAILED": "失败",
    }.get(status, status)
