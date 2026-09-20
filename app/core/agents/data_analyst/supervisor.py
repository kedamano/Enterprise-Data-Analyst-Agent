"""Multi-Agent Supervisor: split complex queries into parallel sub-analyses.

For questions that naturally decompose into independent sub-tasks
(e.g. "compare North vs South China on revenue, AOV, and conversion over
the past 3 months"), the Supervisor:

  1. Plans sub-tasks from ``state.goal`` / ``state.plan`` (LM-free heuristics).
  2. Runs each sub-task through a light pipeline:
       SubContext → SubPlan (schema_search + sql_query) → SubAnalyst → SubReporter
  3. Merges all sub-reports into a single master report.

Sub-tasks never communicate with each other (no cross-contamination).
The Supervisor is only activated when ``_should_supervisor`` returns True;
otherwise the existing single-thread pipeline handles the request.
"""
from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....config import get_settings
from ...interfaces import trace_run
from .nodes import run_analyst, run_context, run_executor_all, run_planner, run_reporter
from .state import AgentState, ContextModel, PlanModel, PlanStep

logger = logging.getLogger("da.supervisor")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class SubTask:
    sub_id: str            # "sub_0", "sub_1", ...
    title: str             # "华北营收分析"
    query: str             # 子任务完整问题（独立描述，不依赖兄弟上下文）
    objective: str         # 分析目标
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    priority: int = 0      # 并行优先级（0 为最高；同 priority 可并发）
    depends_on: list[str] = field(default_factory=list)  # 依赖的 sub_id


# --------------------------------------------------------------------------- #
# Heuristic: should we activate the Supervisor?
# --------------------------------------------------------------------------- #
_SPLIT_SIGNALS = (
    "对比", "vs", "以及", "还有", "并且", "和", "与",
    ",", "；", ";",
)

# Common metric keywords used for automatic metric extraction
_METRIC_KEYWORDS = (
    "营收", "收入", "revenue", "客单价", "aov", "arpu",
    "转化率", "conversion", "留存", "retention",
    "毛利", "利润", "profit", "毛利额",
    "订单数", "order", "成交量", "gmv",
    "复购", "复购率", "退货率", "退货",
    "成本", "cost",
)

_DIMENSION_KEYWORDS = (
    "华北", "华南", "华东", "华中", "西北", "西南", "东北",
    "north", "south", "east", "west",
    "区域", "地区", "城市", "渠道", "品类", "产品",
    "月度", "季度", "年度", "日", "week", "month",
)


def _should_supervisor(state: AgentState) -> bool:
    """Heuristic: decide whether to activate the Supervisor.

    Returns True when the plan is complex enough to benefit from splitting.
    Single-metric / single-dimension queries run faster on the normal pipeline.
    """
    settings = get_settings()
    if not getattr(settings, "supervisor_enabled", True):
        return False
    # 用户强制全跑意味着要精细对比，不走 splits
    if getattr(state, "force_full_rerun", False):
        return False
    steps = state.plan.steps if state.plan else []
    dimensions = state.context.dimensions if state.context else []
    metrics = state.context.metrics if state.context else []
    n_steps = len(steps)
    n_cross = len(dimensions) * len(metrics)
    # 单指标 → 不拆（跑 supervisor 反而慢）
    if n_steps < 3 and n_cross < 2:
        return False
    # 核心规则：>= 3 steps 且维度×指标 >= 4
    if n_steps >= 3 and n_cross >= 4:
        return True
    return False


# --------------------------------------------------------------------------- #
# Step 1: plan_subtasks — LM-free heuristic splitting
# --------------------------------------------------------------------------- #
def plan_subtasks(state: AgentState, max_subtasks: int = 4) -> list[SubTask]:
    """Decompose a parent AgentState into independent SubTasks.

    Uses LM-free heuristics (no extra LLM call):
    - Detect split signal words ("对比", "vs", "以及" ...).
    - Match metric and dimension keywords from the query / plan.
    - For simple queries (1 step / single-dimension) return a single-element list.

    Returns at most ``max_subtasks`` sub-tasks.
    """
    ctx = state.context
    dimensions: list[str] = list(ctx.dimensions) if ctx else []
    metrics: list[str] = list(ctx.metrics) if ctx else []

    # Fallback: try to extract from context / plan text
    if not metrics:
        metrics = _extract_metrics_from_query(state.user_query or "")
    if not dimensions:
        dimensions = _extract_dimensions_from_query(state.user_query or "")

    # Simple query → single sub-task (no split benefit)
    n_steps = len(state.plan.steps) if state.plan else 0
    n_cross = len(dimensions) * len(metrics)
    if n_steps < 3 or n_cross < 2:
        return [_make_single_subtask(state, metrics, dimensions)]

    # Complex query: generate Cartesian cross of dimension x metric
    subtasks: list[SubTask] = []
    priority = 0
    for dim in dimensions:
        for met in metrics:
            if len(subtasks) >= max_subtasks:
                break
            sub = SubTask(
                sub_id=f"sub_{len(subtasks)}",
                title=f"{dim}{met}分析",
                query=_build_sub_query(state, dim, met, dimensions, metrics),
                objective=f"分析 {dim} 下的 {met} 表现",
                metrics=[met],
                dimensions=[dim],
                priority=priority,
                depends_on=[],
            )
            subtasks.append(sub)
        if len(subtasks) >= max_subtasks:
            break

    # If heuristics produced nothing useful, fall back to a single sub-task
    if not subtasks:
        return [_make_single_subtask(state, metrics, dimensions)]

    return subtasks


def _make_single_subtask(
    state: AgentState,
    metrics: list[str],
    dimensions: list[str],
) -> SubTask:
    """Build a single SubTask that wraps the original query unchanged."""
    return SubTask(
        sub_id="sub_0",
        title="综合分析",
        query=state.user_query,
        objective=state.context.objective if state.context else state.user_query,
        metrics=metrics,
        dimensions=dimensions,
        priority=0,
        depends_on=[],
    )


def _extract_metrics_from_query(query: str) -> list[str]:
    """LM-free metric extraction from user query."""
    found: list[str] = []
    q = query.lower()
    for m in _METRIC_KEYWORDS:
        if m.lower() in q and m not in found:
            found.append(m)
    return found


def _extract_dimensions_from_query(query: str) -> list[str]:
    """LM-free dimension extraction from user query."""
    found: list[str] = []
    q = query.lower()
    for d in _DIMENSION_KEYWORDS:
        if d.lower() in q and d not in found:
            found.append(d)
    return found


def _build_sub_query(
    state: AgentState,
    dim: str,
    met: str,
    all_dims: list[str],
    all_metrics: list[str],
) -> str:
    """Build an independent, self-contained query string for a sub-task."""
    base = state.context.objective if state.context and state.context.objective else state.user_query
    return (
        f"{base}\n\n"
        f"子任务焦点：{dim} 区域下的 {met} 指标分析。\n"
        f"请独立完成该维度×指标的数据查询、分析与结论，不依赖其他子任务的结果。"
    )


# --------------------------------------------------------------------------- #
# Step 2: lightweight pipeline for each sub-task
# --------------------------------------------------------------------------- #
def _run_subtask_pipeline(
    parent_state: AgentState,
    subtask: SubTask,
) -> AgentState:
    """Light pipeline for a single sub-task.

    SubContext → SubPlan (schema_search + sql_query) → SubAnalyst → SubReporter
    Does NOT include the heavy Reflection/Reporter chain.
    Reuses existing node functions to avoid logic duplication.
    """
    sub_state = _build_sub_state(parent_state, subtask)
    # Context
    sub_state = run_context(sub_state)
    if sub_state.status in ("ERROR", "CLARIFY"):
        return sub_state
    # Planner (produces plan with schema_search + sql_query steps)
    sub_state = run_planner(sub_state)
    # Executor — run all planned steps
    sub_state = run_executor_all(sub_state)
    # Analyst
    sub_state = run_analyst(sub_state)
    # Reporter (single segment)
    sub_state = run_reporter(sub_state)
    return sub_state


def _build_sub_state(parent_state: AgentState, subtask: SubTask) -> AgentState:
    """Create a fresh AgentState that inherits context but addresses one sub-task."""
    sub_state = AgentState(
        session_id=f"{parent_state.session_id}-{subtask.sub_id}",
        user_query=subtask.query,
        conversation_history=parent_state.conversation_history,
        context=ContextModel(
            objective=subtask.objective,
            metrics=subtask.metrics,
            dimensions=subtask.dimensions,
            analysis_object=parent_state.context.analysis_object if parent_state.context else [],
            time_range=parent_state.context.time_range if parent_state.context else None,
            comparison=parent_state.context.comparison if parent_state.context else None,
            output_format="report",
        ),
        attachment_context=getattr(parent_state, "attachment_context", "") or "",
        mode="full",
        force_full_rerun=False,
        max_replans=getattr(parent_state, "max_replans", 2),
    )
    # Carry metadata
    sub_state.metadata["supervisor_parent_session"] = parent_state.session_id
    sub_state.metadata["supervisor_sub_id"] = subtask.sub_id
    sub_state.metadata["supervisor_sub_title"] = subtask.title
    # Add a minimal plan so that run_planner has at least one step reference
    # The planner will overwrite this, but the state must have a valid PlanModel
    sub_state.plan = PlanModel(
        goal=subtask.objective,
        steps=[
            PlanStep(
                id="sub_ctx",
                objective=f"理解 {subtask.title} 的分析目标",
                action="context",
                tool="context",
            ),
        ],
    )
    return sub_state


# --------------------------------------------------------------------------- #
# Step 3: topological-sort by priority bands, execute in parallel
# --------------------------------------------------------------------------- #
def _topological_bands(subtasks: list[SubTask]) -> list[list[SubTask]]:
    """Group sub-tasks into priority bands.

    Sub-tasks with the same priority that have no unresolved dependencies
    can run in parallel. Each band is a list of sub-tasks to execute concurrently.
    """
    if not subtasks:
        return []
    # Group by priority
    priority_map: dict[int, list[SubTask]] = {}
    for st in subtasks:
        priority_map.setdefault(st.priority, []).append(st)
    # Sort by priority ascending (0 first)
    sorted_priorities = sorted(priority_map.keys())
    bands: list[list[SubTask]] = []
    completed: set[str] = set()
    for prio in sorted_priorities:
        band = [st for st in priority_map[prio]
                if all(d in completed for d in st.depends_on)]
        if band:
            bands.append(band)
            for st in band:
                completed.add(st.sub_id)
    return bands


# --------------------------------------------------------------------------- #
# Step 4: merge all sub-reports into a master report
# --------------------------------------------------------------------------- #
def _merge_reports(
    subtask_results: dict[str, AgentState],
    subtasks: list[SubTask],
    bands: list[list[SubTask]],
    total_wall_ms: float,
) -> tuple[str, dict[str, Any]]:
    """Merge all sub-task reports into a single master report string."""
    sections: list[str] = []
    sections.append("# 综合分析报告（Supervisor 并行子任务）\n")
    for subtask in subtasks:
        sub_state = subtask_results.get(subtask.sub_id)
        if sub_state is None:
            sections.append(f"## {subtask.title}: [子任务 {subtask.sub_id} 未执行]\n")
            continue
        if sub_state.status in ("ERROR", "FAILED"):
            err = sub_state.error or sub_state.status
            sections.append(
                f"## {subtask.title}: [子任务 {subtask.sub_id} 失败]\n\n"
                f"> 错误信息: {err}\n"
            )
            continue
        report_text = sub_state.report or "(无报告)"
        sections.append(f"## {subtask.title}:\n\n{report_text}\n")

    master_report = "\n---\n".join(sections)
    supervisor_meta: dict[str, Any] = {
        "total_subtasks": len(subtasks),
        "parallel_bands": len(bands),
        "total_wall_time_ms": int(total_wall_ms),
        "subtask_ids": [st.sub_id for st in subtasks],
    }
    return master_report, supervisor_meta


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def run_supervisor(
    parent_state: AgentState,
    traces_dir: Path | None = None,
) -> AgentState:
    """Supervisor entry point.

    1. Plan sub-tasks from parent_state.
    2. If only 1 sub-task → return parent_state unchanged (fall back to main pipeline).
    3. Topological-sort sub-tasks into priority bands.
    4. Within each band, execute sub-tasks in parallel via ThreadPoolExecutor.
    5. Merge all sub-reports into a single master_report on parent_state.
    """
    settings = get_settings()
    max_subtasks = int(getattr(settings, "max_subtasks", 4))
    max_workers = int(getattr(settings, "supervisor_max_parallel", 4))
    timeout_per = float(getattr(settings, "supervisor_timeout_per_subtask_s", 120.0))

    subtasks = plan_subtasks(parent_state, max_subtasks=max_subtasks)

    # Fallback: single sub-task → let the main pipeline handle it
    if len(subtasks) <= 1:
        logger.info("Supervisor: 仅 %d 个子任务，跳过并行，回退主流水线", len(subtasks))
        return parent_state

    logger.info(
        "Supervisor 启动: %d 个子任务, max_workers=%d",
        len(subtasks), max_workers,
    )

    wall_start = time.monotonic()
    subtask_results: dict[str, AgentState] = {}
    errors: dict[str, str] = {}
    bands = _topological_bands(subtasks)

    with trace_run(
        run_id=f"{parent_state.session_id}-supervisor",
        trace_dir=traces_dir,
    ):
        for band_idx, band in enumerate(bands):
            if len(band) == 1:
                # Single task in this band — run inline (no thread overhead)
                st = band[0]
                try:
                    subtask_results[st.sub_id] = _run_subtask_pipeline(parent_state, st)
                except Exception as exc:
                    logger.warning("子任务 %s 异常: %s", st.sub_id, exc)
                    err_state = _error_sub_state(parent_state, st, str(exc))
                    subtask_results[st.sub_id] = err_state
                    errors[st.sub_id] = str(exc)
            else:
                # Parallel execution
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    future_map = {
                        pool.submit(_run_subtask_pipeline, parent_state, st): st
                        for st in band
                    }
                    for future in as_completed(future_map, timeout=timeout_per * len(band)):
                        st = future_map[future]
                        try:
                            subtask_results[st.sub_id] = future.result(timeout=timeout_per)
                        except Exception as exc:
                            logger.warning("子任务 %s 异常: %s", st.sub_id, exc)
                            err_state = _error_sub_state(parent_state, st, str(exc))
                            subtask_results[st.sub_id] = err_state
                            errors[st.sub_id] = str(exc)

    wall_ms = (time.monotonic() - wall_start) * 1000.0

    # Merge
    master_report, supervisor_meta = _merge_reports(subtask_results, subtasks, bands, wall_ms)

    # Write results into parent_state
    parent_state.report = master_report
    parent_state.status = "FINISH"
    parent_state.metadata["supervisor"] = supervisor_meta
    if errors:
        parent_state.metadata["supervisor"]["errors"] = errors

    logger.info(
        "Supervisor 完成: %d 个子任务, %d 个并行波段, %.1fms",
        len(subtasks), len(bands), wall_ms,
    )
    return parent_state


def _error_sub_state(
    parent_state: AgentState,
    subtask: SubTask,
    error_msg: str,
) -> AgentState:
    """Build an error-state AgentState for a failed sub-task."""
    state = _build_sub_state(parent_state, subtask)
    state.status = "ERROR"
    state.error = error_msg
    state.report = f"子任务 {subtask.sub_id} ({subtask.title}) 执行失败: {error_msg}"
    return state
