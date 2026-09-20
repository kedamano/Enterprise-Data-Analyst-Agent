"""Analytics aggregation endpoint – roll up trace JSONL summaries by time range.

GET /api/v1/analytics/stats?range=24h|7d|30d&session_id=xxx

Reads ``data/traces/*.jsonl``, takes the last line (summary) from each file,
filters by file mtime, and aggregates totals / by_stage / by_tool / daily /
recent_runs.  No new dependencies – stdlib ``json`` / ``pathlib`` only.
"""
from __future__ import annotations

import json
import math
import pathlib
import time
from collections import defaultdict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import get_settings
from ...core.security.auth import current_principal
from ...infrastructure.observability import metrics as _metrics_mod

router = APIRouter(prefix="/analytics", tags=["analytics"])

_DEFAULT_TRACE_DIR = pathlib.Path("data/traces")
_MAX_FILES = 200  # cap: only scan the newest N files; beyond that sample


def _range_seconds(range_str: str) -> int:
    """Convert ``24h`` / ``7d`` / ``30d`` to seconds."""
    mapping = {"24h": 86400, "7d": 604800, "30d": 2592000}
    if range_str in mapping:
        return mapping[range_str]
    # try parsing as Nd / Nh
    if range_str.endswith("h"):
        try:
            return int(range_str[:-1]) * 3600
        except ValueError:
            pass
    if range_str.endswith("d"):
        try:
            return int(range_str[:-1]) * 86400
        except ValueError:
            pass
    raise HTTPException(status_code=400, detail=f"无效的时间范围: {range_str}（支持 24h/7d/30d）")


def _pctl(sorted_vals: list[float], p: float) -> float:
    """Percentile over a **sorted** list (clamped nearest-rank)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, int(math.ceil((p / 100.0) * len(sorted_vals))) - 1)
    return sorted_vals[idx]


def _parse_file(path: pathlib.Path) -> tuple[list[dict], dict] | None:
    """Read a JSONL trace file → (spans, summary).

    Summary is the last non-parseable-as-span line (has ``duration_s``, no ``span_id``).
    Returns None on OSError or if no summary found.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.strip().splitlines()
    if not lines:
        return None

    spans: list[dict] = []
    summary: dict | None = None
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # heuristic: summary has "duration_s" + no "span_id"
        if "duration_s" in obj and "span_id" not in obj:
            summary = obj
        else:
            spans.append(obj)

    if summary is None:
        return None

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    summary["_file_mtime"] = mtime
    summary["_spans"] = spans
    return spans, summary


def _read_summaries(trace_dir: pathlib.Path, cutoff: float, session_id: Optional[str]) -> list[dict]:
    """Read JSONL files modified after *cutoff*, return their summary dicts (with ``_spans`` attached).

    When *session_id* is given only that one file is read (by safe-name lookup).
    """
    summaries: list[dict] = []

    if session_id:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in session_id)[:80]
        direct = trace_dir / f"{safe}.jsonl"
        if direct.exists():
            parsed = _parse_file(direct)
            if parsed:
                _, summary = parsed
                if summary.get("_file_mtime", 0) >= cutoff:
                    summaries.append(summary)
        return summaries

    # Scan directory, sort by mtime desc, cap at _MAX_FILES
    try:
        files = sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []

    # If too many files, sample evenly
    if len(files) > _MAX_FILES:
        step = len(files) / _MAX_FILES
        files = [files[int(i * step)] for i in range(_MAX_FILES)]

    for f in files:
        mtime: float
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        parsed = _parse_file(f)
        if parsed is None:
            continue
        _, summary = parsed
        summaries.append(summary)

    return summaries


def _aggregate(summaries: list[dict], range_str: str, range_secs: int, now: float) -> dict[str, Any]:
    """Turn a list of summary dicts into the analytics response schema."""
    if not summaries:
        empty = {
            "range": range_str,
            "totals": {
                "runs": 0,
                "success": 0,
                "failed": 0,
                "success_rate": 0.0,
                "avg_duration_ms": 0,
                "p95_duration_ms": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_tool_calls_per_run": 0.0,
                "avg_runs_per_hour": 0.0,
            },
            "by_stage": [],
            "by_tool": [],
            "daily": [],
            "recent_runs": [],
            "process_metrics": _metrics_mod.metrics.snapshot(),
        }
        return empty

    # --- totals ---
    durations_ms: list[float] = []
    prompt_tokens_total = 0
    completion_tokens_total = 0
    cost_total = 0.0
    success_count = 0
    failed_count = 0
    tool_call_counts: list[int] = []

    # stage aggregation: stage -> list of durations
    stage_durations: dict[str, list[float]] = defaultdict(list)
    # tool aggregation: tool -> list of durations
    tool_durations: dict[str, list[float]] = defaultdict(list)
    # tool success counts
    tool_success: dict[str, int] = defaultdict(int)
    tool_fail: dict[str, int] = defaultdict(int)
    # daily buckets: hour-key -> {runs, tokens}
    daily_buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "tokens": 0})

    recent: list[dict] = []

    for s in summaries:
        dur = float(s.get("duration_s", 0)) * 1000  # stored in seconds → ms
        durations_ms.append(dur)
        status = s.get("status", "OK")
        if status == "ERROR":
            failed_count += 1
        else:
            success_count += 1

        pt = int(s.get("prompt_tokens", 0) or 0)
        ct = int(s.get("completion_tokens", 0) or 0)
        prompt_tokens_total += pt
        completion_tokens_total += ct
        # cost is not in summary by default; scan stages if available
        spans = s.get("_spans", [])
        for sp in spans:
            stage = sp.get("stage", "unknown")
            sp_dur = float(sp.get("duration_ms", 0) or 0)
            stage_durations[stage].append(sp_dur)
            tool = sp.get("tool")
            if tool:
                tool_durations[tool].append(sp_dur)
                if sp.get("status") == "ERROR":
                    tool_fail[tool] += 1
                else:
                    tool_success[tool] += 1

        stages_count = len(s.get("stages", []))
        tool_call_counts.append(stages_count)

        ts = s.get("_file_mtime", now)
        hour_key = time.strftime("%Y-%m-%dT%H", time.localtime(ts))
        daily_buckets[hour_key]["runs"] += 1
        daily_buckets[hour_key]["tokens"] += pt + ct

        recent.append({
            "session_id": s.get("trace_id", "unknown")[:16],
            "ts": int(ts),
            "duration_ms": round(dur),
            "status": status,
            "tokens": pt + ct,
        })

    total_runs = len(summaries)
    durations_sorted = sorted(durations_ms)
    p95 = _pctl(durations_sorted, 95)
    avg_dur = round(sum(durations_ms) / total_runs) if total_runs else 0
    avg_tool = round(sum(tool_call_counts) / total_runs, 1) if total_runs else 0.0
    success_rate = round(success_count / total_runs, 3) if total_runs else 0.0

    hours = range_secs / 3600.0
    runs_per_hour = round(total_runs / hours, 2) if hours > 0 else 0.0

    # --- by_stage ---
    by_stage = []
    for stage, durs in sorted(stage_durations.items(), key=lambda x: -sum(x[1]) / len(x[1])):
        by_stage.append({
            "stage": stage,
            "avg_ms": round(sum(durs) / len(durs)),
            "count": len(durs),
        })

    # --- by_tool ---
    by_tool = []
    for tool in sorted(tool_durations.keys()):
        durs = tool_durations[tool]
        ok = tool_success.get(tool, 0)
        fail = tool_fail.get(tool, 0)
        total = ok + fail
        by_tool.append({
            "tool": tool,
            "count": len(durs),
            "avg_ms": round(sum(durs) / len(durs)),
            "success_rate": round(ok / total, 3) if total else 1.0,
        })

    # --- daily ---
    daily = [{"hour": k, **v} for k, v in sorted(daily_buckets.items())]

    # --- recent_runs sorted by ts desc ---
    recent.sort(key=lambda r: r["ts"], reverse=True)

    return {
        "range": range_str,
        "totals": {
            "runs": total_runs,
            "success": success_count,
            "failed": failed_count,
            "success_rate": success_rate,
            "avg_duration_ms": avg_dur,
            "p95_duration_ms": round(p95),
            "total_prompt_tokens": prompt_tokens_total,
            "total_completion_tokens": completion_tokens_total,
            "total_cost_usd": round(cost_total, 4),
            "avg_tool_calls_per_run": avg_tool,
            "avg_runs_per_hour": runs_per_hour,
        },
        "by_stage": by_stage,
        "by_tool": by_tool,
        "daily": daily,
        "recent_runs": recent[:50],
        "process_metrics": _metrics_mod.metrics.snapshot(),
    }


def _admin_required(principal) -> None:
    """AUTH 开启时只有 admin 角色能访问。"""
    if not get_settings().auth_enabled:
        return
    roles = getattr(principal, "roles", [])
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="需要管理员权限")


@router.get("/stats")
def analytics_stats(
    range: str = Query(default="24h", description="时间范围：24h / 7d / 30d"),
    session_id: Optional[str] = Query(default=None, description="指定 session_id 时只返回该次运行"),
    principal=Depends(current_principal),
):
    """聚合 trace 统计。AUTH 开启时仅 admin 可访问。"""
    _admin_required(principal)
    range_secs = _range_seconds(range)
    now = time.time()
    cutoff = now - range_secs
    trace_dir = _DEFAULT_TRACE_DIR
    summaries = _read_summaries(trace_dir, cutoff, session_id)
    return _aggregate(summaries, range, range_secs, now)
