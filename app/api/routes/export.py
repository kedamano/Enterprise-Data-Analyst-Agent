"""E5/03 分析交付物导出：报告 / SQL / 数据 / 溯源一次拿走。

Spec: docs/specs/E5/03-export.md

分析师做完分析要**交出去**——贴周报、发同事、存档复盘。此前报告只在界面上、
SQL 散在 trace 接口、CSV 散在 artifacts 接口，没有"一次拿走"的产物。

**安全**：只打包该会话工作目录**之下**的文件（路径白名单，防穿越）；
只读该 session 自己的 checkpoint；纯读，不新增写操作。
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

router = APIRouter(prefix="/chat", tags=["export"])

_SQL_TOOLS = ("sql_query", "freeform")


def _load_state(session_id: str):
    from ...core.agents.data_analyst.checkpoint import load as cp_load

    state = cp_load(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"未找到运行记录: {session_id}")
    return state


def _sql_steps(state) -> list[dict]:
    """按执行顺序取出可导出的 SQL（只读语句本身，不含结果）。"""
    out: list[dict] = []
    for r in state.tool_results or []:
        if getattr(r, "tool", "") in _SQL_TOOLS and getattr(r, "status", "") == "SUCCESS":
            sql = (getattr(r, "input", None) or {}).get("sql") or ""
            if sql.strip():
                out.append({"step_id": r.step_id, "sql": sql.strip()})
    return out


def _queries_sql(state) -> str:
    plan_step = {s.id: s for s in (state.plan.steps if state.plan else [])}
    lines = ["-- 本次分析的查询清单（只读）",
             f"-- session: {state.session_id}",
             f"-- objective: {state.context.objective}",
             f"-- exported: {datetime.now(timezone.utc).isoformat()}",
             ""]
    steps = _sql_steps(state)
    if not steps:
        lines.append("-- 本次运行没有产生 SQL 查询。")
    for s in steps:
        step = plan_step.get(s["step_id"])
        if step is not None:
            lines.append(f"-- [{s['step_id']}] {step.objective}")
        lines.append(s["sql"].rstrip(";") + ";")
        lines.append("")
    return "\n".join(lines)


def _session_workdir(session_id: str) -> Path:
    from ...core.tools import session_workdir

    return session_workdir(session_id).resolve()


def _csv_artifacts(state, workdir: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """收集会话工作目录**之下**的 CSV。越界路径跳过并记录（防路径穿越）。"""
    found: dict[str, Path] = {}
    skipped: list[str] = []
    for r in state.tool_results or []:
        out = getattr(r, "output", None) or {}
        candidates = [out.get("csv_path")] + list(getattr(r, "artifacts", None) or [])
        for raw in candidates:
            if not raw or not str(raw).lower().endswith(".csv"):
                continue
            p = Path(str(raw))
            try:
                resolved = p.resolve()
                resolved.relative_to(workdir)  # 越界会抛 ValueError
            except Exception:
                skipped.append(str(raw))
                continue
            if resolved.exists():
                found[f"data/{resolved.name}"] = resolved
    return sorted(found.items()), skipped


def _readme(state, csvs: list[tuple[str, Path]], skipped: list[str],
            is_blocked: bool = False) -> str:
    lines = [
        "== 分析交付包 ==",
        f"会话 session_id：{state.session_id}",
        f"分析目标：{state.context.objective or state.user_query}",
        f"运行状态：{state.status}",
        f"导出时间：{datetime.now(timezone.utc).isoformat()}",
        "",
        "-- 文件清单 --",
        "report.md      报告正文（含「数字来源」「口径说明」「数据质量与限制」）",
        "queries.sql    本次运行的全部只读查询（按执行顺序，含 step_id）",
        "trace.json     溯源清单（结论 → sql_id → SQL → 行样本）",
        "data/*.csv     数据产物",
        "",
        "-- 重要：这些文件**不做脱敏** --",
        "data/*.csv 与 queries.sql 保留**原始值**（分析师本机产物需可用）。",
        "只有「进 LLM 上下文」的那份样本会被脱敏（见 E4/02）；导出物不受影响，",
        "因此**对外分享前请自行确认是否含敏感数据**。",
        "",
    ]
    if not csvs:
        lines.append("本次运行**无数据产物**（未执行取数步骤，或结果为空）。")
    if skipped:
        lines.append(f"已跳过 {len(skipped)} 个越界/不存在的产物路径（不在本会话工作目录内）：")
        lines += [f"  - {s}" for s in skipped[:10]]
    notes = list(getattr(getattr(state, "analysis", None), "quality_notes", None) or [])
    if notes:
        lines += ["", "-- 数据质量与口径披露 --"] + [f"- {n}" for n in notes]
    if state.error:
        lines += ["", f"-- 运行中的问题 --\n{state.error}"]
    return "\n".join(lines) + "\n"


def _trace_json(state) -> str:
    try:
        from ...core.agents.data_analyst.sources import trace_manifest

        return json.dumps(trace_manifest(state), ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        return json.dumps({"error": f"溯源清单不可用: {exc}"}, ensure_ascii=False)


def _zip_bytes(items: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items.items():
            zf.writestr(name, data)
    return buf.getvalue()


@router.get("/analyze/export/{session_id}")
def analyze_export(session_id: str,
                   format: str = Query("zip", pattern="^(zip|sql|report|csv)$")):
    """导出本次分析的交付物。``format``：zip（默认）/ sql / report / csv。"""
    # AUTH/01：越权读别人的会话 → 403
    try:
        from ...core.security.auth import current_principal, owns_session

        if not owns_session(session_id, current_principal()):
            raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception:
        pass

    # D45 两步授权：导出物**保留未脱敏原始值**（E4/02 只约束进 LLM 上下文的那份），
    # 属合规上的高危动作。默认关（HITL_ENABLED=false）→ 这段不生效。
    try:
        from ...core.security import hitl

        if not hitl.take_grant(session_id, "export_raw"):
            risk = hitl.requires_confirmation("export_raw", {"format": format})
            if risk is not None:
                pending = hitl.begin(session_id, risk.action, risk.detail)
                raise HTTPException(status_code=428, detail={
                    "message": "该导出需要人工确认（两步授权）",
                    "action": risk.action, "reason": risk.reason,
                    "token": pending["token"],
                    "howto": "POST /api/v1/chat/analyze/confirm {session_id, token, approved}",
                })
    except HTTPException:
        raise
    except Exception:
        pass  # 策略层故障不得让导出直接崩（fail-closed 由 requires_confirmation 内部保证）

    state = _load_state(session_id)
    workdir = _session_workdir(session_id)
    csvs, skipped = _csv_artifacts(state, workdir)

    if format == "sql":
        return Response(_queries_sql(state), media_type="text/plain; charset=utf-8")
    if format == "report":
        return Response(state.report or "（本次运行没有产出报告）",
                        media_type="text/markdown; charset=utf-8")

    items: dict[str, bytes] = {
        "README.txt": _readme(state, csvs, skipped).encode("utf-8"),
        "report.md": (state.report or "（本次运行没有产出报告）").encode("utf-8"),
        "queries.sql": _queries_sql(state).encode("utf-8"),
        "trace.json": _trace_json(state).encode("utf-8"),
    }
    for name, path in csvs:
        try:
            items[name] = path.read_bytes()
        except Exception:
            skipped.append(str(path))
    if format == "csv":
        items = {k: v for k, v in items.items() if k.startswith("data/") or k == "README.txt"}
    return Response(_zip_bytes(items), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="analysis-{session_id}.zip"'})
