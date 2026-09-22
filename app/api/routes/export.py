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
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

logger = logging.getLogger(__name__)

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
            is_blocked: bool = False, masked: bool = False,
            charts: list[dict] | None = None) -> str:
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
        "charts/*.png   报告里内嵌的图表（报告用 /api/v1/chat/analyze/chart/... 引用，"
        "贴到外部平台会裂图，此处是**离线可看的那份**）",
        "WATERMARK.txt  导出水印（DLP，可验证）",
        "",
    ]
    if masked:
        lines += [
            "-- 本交付包已按 DLP 策略脱敏 --",
            "data/*.csv 按调用方角色的策略处理：strict 列**整列剔除**（含表头），",
            "sample 列按值掩码，none 列原样。水印见 WATERMARK.txt 与响应头 X-Dlp-Watermark。",
            "**本包不含 charts/ 图表**：图是位图，无法逐像素脱敏——宁可少给，",
            "不可假装脱敏过。需要图请用 masked=0 导出（走两步授权）。",
            "需要原始值请用 masked=0 重新导出（走两步授权）。",
            "",
        ]
    else:
        lines += [
            "-- 重要：这些文件**不做脱敏** --",
            "data/*.csv 与 queries.sql 保留**原始值**（分析师本机产物需可用）。",
            "只有「进 LLM 上下文」的那份样本会被脱敏（见 E4/02）；导出物不受影响，",
            "因此**对外分享前请自行确认是否含敏感数据**。",
            "",
        ]
    if charts and not masked:
        lines.append(f"本次运行有 {len(charts)} 张图，已随包提供（charts/）。")
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


# --------------------------------------------------------------------------- #
# D51：**唯一的**交付包构造函数 —— 导出预览与 zip 必须同源
#     "预览说有几个文件、各多少字节，包里就必须是同样那几个、同样那些字节。"
#     两处各写一份清单 = 预览迟早骗人，而且没人会立刻发现。
# --------------------------------------------------------------------------- #

#: 文件名 → (kind, note)。未登记的走默认（kind=data）。
_ITEM_META = {
    "report.md": ("report", "报告正文（含图表引用）"),
    "queries.sql": ("sql", "本次运行的全部只读查询"),
    "trace.json": ("trace", "溯源清单（结论 → SQL → 行样本）"),
    "README.txt": ("readme", "交付说明"),
    "WATERMARK.txt": ("watermark", "导出水印（可验证）"),
}


def _item_meta(name: str) -> tuple[str, str]:
    if name in _ITEM_META:
        return _ITEM_META[name]
    if name.startswith("charts/"):
        return "chart", "报告内嵌图表"
    if name.startswith("data/"):
        return "data", "数据产物（CSV）"
    return "other", ""


def _principal_check(session_id: str):
    """AUTH/01：越权读别人的会话 → 403；AUTH 关闭 → 返回匿名 principal。"""
    from ...core.security.auth import current_principal, owns_session

    principal = None
    try:
        principal = current_principal()
        if not owns_session(session_id, principal):
            raise HTTPException(status_code=403, detail="无权访问该会话")
    except HTTPException:
        raise
    except Exception:
        pass
    return principal


def _want_masked(masked: str) -> bool:
    """``masked`` 语义：``1``=脱敏（安全路径）/ ``0``=原始（走 HITL）/ 缺省按策略是否激活。"""
    from ...core.security import dlp as _dlp

    if masked == "1":
        return True
    if masked == "0":
        return False
    return _dlp.policy_active()


def _build_items(state, session_id: str, principal, want_masked: bool
                 ) -> tuple[dict[str, bytes], list[str], str | None]:
    """构造交付包的全部条目（含 README 与可选水印）。

    返回 ``(items, skipped, watermark_token)``。**zip 与 manifest 共用本函数**：
    任何"包里有什么"的改动只在这里发生一次。
    """
    from ...config import get_settings as _settings

    from ...core.agents.data_analyst import charts as _charts
    from ...core.security import dlp as _dlp, watermark as _wm

    workdir = _session_workdir(session_id)
    csvs, skipped = _csv_artifacts(state, workdir)
    chart_items = _charts.collect_charts(state)

    resolver = _dlp.make_resolver(principal) if want_masked else None
    # 报告：运行期内嵌了**绝对** URL（chart 端点直出）；导出包要自包含 ——
    # 剥掉绝对路径那段 `## 图表`，按**相对**路径 `charts/{name}` 重嵌，与包内
    # charts/*.png 同根。脱敏版不含图，整段剔除（离线也看不到位图）。
    base_report = state.report or "（本次运行没有产出报告）"
    if not want_masked and chart_items:
        report_text = _charts.embed_charts(_charts.strip_charts_section(base_report),
                                           chart_items, session_id,
                                           url_mode="export")
    else:
        report_text = _charts.strip_charts_section(base_report)
    items: dict[str, bytes] = {
        "README.txt": _readme(state, csvs, skipped, masked=bool(want_masked),
                              charts=chart_items).encode("utf-8"),
        "report.md": report_text.encode("utf-8"),
        "queries.sql": _queries_sql(state).encode("utf-8"),
        "trace.json": _trace_json(state).encode("utf-8"),
    }
    for name, path in csvs:
        try:
            raw = path.read_bytes()
            if resolver is not None:
                raw = _dlp.mask_csv_text(raw.decode("utf-8", errors="replace"),
                                         resolver).encode("utf-8")
            items[name] = raw
        except Exception:
            skipped.append(str(path))

    # D51：图表入包。**脱敏版一律不含**——位图无法逐像素脱敏（README 已写明原因）
    if not want_masked:
        for c in chart_items:
            try:
                items[f"charts/{c['name']}"] = Path(c["path"]).read_bytes()
            except Exception:
                skipped.append(str(c.get("path", "")))

    # D49 水印：**附加**（新文件 + 响应头），不改动任何既有产物字节
    token = None
    secret = getattr(_settings(), "dlp_watermark_secret", "") or ""
    if secret:
        user_id = str(getattr(principal, "user_id", "") or "")
        exported_at = datetime.now(timezone.utc).isoformat()
        token = _wm.issue_watermark(user_id=user_id, session_id=session_id,
                                    exported_at=exported_at, secret=secret)
        if token:
            items["WATERMARK.txt"] = _wm.watermark_lines(
                token, user_id=user_id, session_id=session_id,
                exported_at=exported_at).encode("utf-8")
    else:
        # D49 审计：「为什么没加水印」必须有据可查 — 是故意（未配 secret）还是遗漏。
        logger.info("导出未附加 DLP 水印：DLP_WATERMARK_SECRET 未配置（session=%s）", session_id)
    return items, skipped, token


def _manifest_of(items: dict[str, bytes], session_id: str, want_masked: bool) -> dict[str, Any]:
    files = []
    for name, data in sorted(items.items()):
        kind, note = _item_meta(name)
        files.append({"name": name, "bytes": len(data), "kind": kind, "note": note})
    return {
        "session_id": session_id,
        "masked": bool(want_masked),
        "total_bytes": sum(f["bytes"] for f in files),
        "files": files,
    }


@router.get("/analyze/export/{session_id}/manifest")
def analyze_export_manifest(session_id: str,
                            masked: str = Query("", pattern="^(|0|1)$")):
    """D51：**导出预览** —— 打包之前先说清楚包里有什么。

    与 zip **同源**（同一个 :func:`_build_items`）：预览说几个文件、各多少字节，
    包里就是同样那几个、同样那些字节。

    **不需要两步授权**：预览只暴露文件名与字节数，不暴露任何**值**；
    真正取原始值仍然走 D45 的 428/confirm（见 `analyze_export`）。
    """
    principal = _principal_check(session_id)
    want_masked = _want_masked(masked)
    state = _load_state(session_id)
    items, _skipped, _token = _build_items(state, session_id, principal, want_masked)
    return _manifest_of(items, session_id, want_masked)


@router.get("/analyze/export/{session_id}")
def analyze_export(session_id: str,
                   format: str = Query("zip", pattern="^(zip|sql|report|csv)$"),
                   masked: str = Query("", pattern="^(|0|1)$")):
    """导出本次分析的交付物。

    ``format``：zip（默认）/ sql / report / csv。
    ``masked``：``1``=按调用方角色的 DLP 策略脱敏（**安全路径**，无需审批）；
    ``0``=原始值（走 D45 HITL 两步授权）；缺省按策略是否激活决定
    （策略未激活时缺省=0，保持既有行为一字不变）。
    """
    # AUTH/01：越权读别人的会话 → 403
    principal = _principal_check(session_id)

    # D49：脱敏版是安全默认；只有**原始值**导出才需要审批
    want_masked = _want_masked(masked)

    # D45 两步授权：原始导出**保留未脱敏原始值**，属合规上的高危动作。
    # 默认关（HITL_ENABLED=false）→ 这段不生效。
    if not want_masked:
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

    if format == "sql":
        return Response(_queries_sql(state), media_type="text/plain; charset=utf-8")
    if format == "report":
        return Response(state.report or "（本次运行没有产出报告）",
                        media_type="text/markdown; charset=utf-8")

    # D51：与 manifest **同源**的构造（README / CSV 脱敏 / 图表 / 水印都在里面）
    items, _skipped, token = _build_items(state, session_id, principal, want_masked)
    if format == "csv":
        items = {k: v for k, v in items.items() if k.startswith("data/") or k == "README.txt"}

    headers: dict[str, str] = {"Content-Disposition":
                               f'attachment; filename="analysis-{session_id}.zip"'}
    if want_masked:
        headers["masked"] = "1"
    if token:
        headers["X-Dlp-Watermark"] = token

    return Response(_zip_bytes(items), media_type="application/zip", headers=headers)
