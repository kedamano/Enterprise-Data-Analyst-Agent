"""Tool registry for the Data Analyst Agent.

Maps the logical tool names used by the Planner/Executor to concrete
implementations and provides a single ``execute_tool`` entry point that returns
the Executor contract (status / output / error / artifacts). Session-scoped
scratch directories let ``sql_query`` → ``python_analysis`` hand off CSVs and
let ``visualization`` persist PNGs.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents.data_analyst.state import ToolResult

from ...config import get_settings
from ...infrastructure.observability.metrics import record_tool_call
from .errors import ErrorClass, classify_error
from .specs import AUDIT_LOG, TOOL_SPECS, RateLimiter
from . import (
    freeform,
    knowledge_tool,
    profile_tool,
    python_tool,
    report_tool,
    schema_tool,
    sql_tool,
    viz_tool,
    vision_tool,
)

REGISTRY: dict[str, Any] = {
    "sql_query": sql_tool.run,
    "freeform": freeform.run,
    "python_analysis": python_tool.run,
    "schema_search": schema_tool.run,
    "dataset_profile": profile_tool.run,
    "knowledge_search": knowledge_tool.run,
    "visualization": viz_tool.run,
    "generate_report": report_tool.run,
    "image_analyze": vision_tool.run,
}

# Tools that need a writable scratch dir for artifact hand-off.
_SCRATCH_TOOLS = {"python_analysis", "visualization"}

# 高危工具：执行任意代码/横跨权限面，审计必须带标记（供合规/告警筛选）
_HIGH_RISK_TOOLS = {"python_analysis"}

_RATE_LIMITER = RateLimiter()

# 工具返回统一体积管控（04-Q6）：防止单次工具结果撑爆上下文
_MAX_ROWS = 2000
_MAX_TEXT = 20000
_TRUNC = "…[截断]"


def _cap_output(raw: dict[str, Any]) -> dict[str, Any]:
    """顶层大字段截断：rows 列表限行数、文本字段限长。错误字段不截（供分类用）。"""
    if not isinstance(raw, dict):
        return raw
    capped = dict(raw)
    for key, value in raw.items():
        if key == "error" or key == "ok":
            continue
        if isinstance(value, list) and len(value) > _MAX_ROWS:
            capped[key] = value[:_MAX_ROWS] + [{"_truncated": len(value) - _MAX_ROWS}]
        elif isinstance(value, str) and len(value) > _MAX_TEXT:
            capped[key] = value[:_MAX_TEXT] + _TRUNC
    return capped

_SECRET_RE = ("password", "secret", "api_key", "apikey", "token", "credential",
              "authorization", "private_key")


def _sanitize(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if any(s in key.lower() for s in _SECRET_RE):
        return "***"
    if isinstance(value, str) and len(value) > 2000:  # 大输出只留摘要
        return value[:500] + f"…[{len(value)}字符截断]"
    return value


def session_workdir(session_id: str) -> Path:
    wd = Path("data/artifacts") / (session_id or uuid.uuid4().hex)
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _audit(step_id: str, tool: str, session_id: str, result: ToolResult | None = None,
           status: str = "", error: str | None = None, error_class: str | None = None) -> None:
    """Append one JSONL audit record (spec §22 audit_policy=ALWAYS)."""
    spec = TOOL_SPECS.get(tool)
    if spec is None or spec.audit_policy != "ALWAYS":
        return
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "step_id": step_id,
            "tool": tool,
            "permission": spec.permission.value,
            "high_risk": tool in _HIGH_RISK_TOOLS,
            "params_sanitized": _sanitize(result.input if result else {}),
            "status": status or (result.status if result else ""),
            "error": error or (result.error if result else None),
            "error_class": error_class or (result.error_class if result else None),
            "execution_time_ms": result.execution_time_ms if result else None,
            "attempts": result.attempts if result else None,
        }
        # D46：统一经审计存储（默认仍写同一个 JSONL 文件；可切 sqlite/postgres）
        from ...core.security.audit_store import record as _store_record

        _store_record("tool", record, path=AUDIT_LOG)
    except Exception:  # 审计绝不能打断工具调用
        return


def execute_tool(step_id: str, tool: str, params: dict[str, Any], session_id: str = "") -> ToolResult:
    started = time.time()
    if tool not in REGISTRY:
        res = ToolResult(
            step_id=step_id, tool=tool, status="FAILED",
            error=f"未知工具: {tool}", error_class="NON_RETRYABLE",
            execution_time_ms=int((time.time() - started) * 1000),
        )
        record_tool_call(tool, time.time() - started, False)
        _audit(step_id, tool, session_id, res)
        return res
    spec = TOOL_SPECS.get(tool)
    # 速率限制（per session+tool 滑动窗口，§22 rate_limit）
    if spec and not _RATE_LIMITER.allow((session_id, tool), spec.rate_limit_per_min):
        res = ToolResult(
            step_id=step_id, tool=tool, status="FAILED",
            error=f"rate limit exceeded: {tool} 每分钟最多 {spec.rate_limit_per_min} 次（session={session_id}）",
            error_class="NON_RETRYABLE",
            execution_time_ms=int((time.time() - started) * 1000),
        )
        record_tool_call(tool, time.time() - started, False)
        _audit(step_id, tool, session_id, res)
        return res
    # AUTH/01 §6：RBAC 工具级门禁。鉴权开启时，按 principal 的角色判定是否拥有该工具
    # 所需权限；不足则直接拒绝（NON_RETRYABLE），绝不执行。鉴权关闭保持既有全开。
    if spec:
        from ..security import auth as _sec

        if _sec.enabled():
            from ..security.auth import current_principal, effective_permissions

            principal = current_principal()
            if spec.permission.value not in effective_permissions(principal):
                res = ToolResult(
                    step_id=step_id, tool=tool, status="FAILED",
                    error=f"权限不足：工具 {tool} 需要 {spec.permission.value}，"
                          f"当前角色 {principal.roles or []} 未授予（permission denied）",
                    error_class="NON_RETRYABLE",
                    execution_time_ms=int((time.time() - started) * 1000),
                )
                record_tool_call(tool, time.time() - started, False)
                _audit(step_id, tool, session_id, res)
                return res
    max_attempts = 1 + get_settings().max_tool_retries
    last_raw: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        try:
            wd = str(session_workdir(session_id))
            # ATTACH/01：把 session_id 透传给 sql_query / schema_search，
            # 让它们能挂载并发现用户上传的边车库。
            # 不改调用方签名，只在需要的工具上补一个下划线前缀字段。
            call_params = params
            if tool in ("sql_query", "freeform", "schema_search", "dataset_profile", "image_analyze") and session_id:
                call_params = {**params, "_session_id": session_id}
            raw = REGISTRY[tool](call_params, wd) if tool in _SCRATCH_TOOLS else REGISTRY[tool](call_params)
            # 上下文预算与落盘数据必须分开：_cap_output 只约束"进 LLM 的行数"，
            # CSV 物化要用**未截断**的完整结果（否则大结果被悄悄少写）。
            full_rows = (raw or {}).get("rows") if isinstance(raw, dict) else None
            raw = _cap_output(raw or {}) if isinstance(raw, dict) else raw
            ok = bool(raw.get("ok", True)) if isinstance(raw, dict) else True
            if ok or attempt >= max_attempts:
                res = _to_result(step_id, tool, params, raw or {}, ok, attempt, started, wd,
                                 full_rows=full_rows, session_id=session_id)
                _audit(step_id, tool, session_id, res)
                return res
            # 失败且还有重试额度 → 仅 RETRYABLE 错误重试（错误处理协议 §23）
            last_raw = raw
            if classify_error(raw.get("error")) is not ErrorClass.RETRYABLE:
                res = _to_result(step_id, tool, params, raw, False, attempt, started, wd,
                                     session_id=session_id)
                _audit(step_id, tool, session_id, res)
                return res
        except Exception as exc:  # never let a tool crash the graph
            last_raw = {"ok": False, "error": str(exc)}
            if classify_error(str(exc)) is not ErrorClass.RETRYABLE or attempt >= max_attempts:
                res = ToolResult(
                    step_id=step_id, tool=tool, status="FAILED", input=params,
                    error=str(exc),
                    error_class=classify_error(str(exc)).value, attempts=attempt,
                    execution_time_ms=int((time.time() - started) * 1000),
                )
                _audit(step_id, tool, session_id, res)
                return res
    # 循环耗尽（理论上不可达，防御性兜底）
    res = _to_result(step_id, tool, params, last_raw, False, max_attempts, started,
                     str(session_workdir(session_id)), session_id=session_id)
    _audit(step_id, tool, session_id, res)
    return res


def _to_result(step_id: str, tool: str, params: dict[str, Any], raw: dict[str, Any],
               ok: bool, attempt: int, started: float, wd: str,
               full_rows: list[Any] | None = None,
               session_id: str = "") -> ToolResult:
    artifacts = raw.get("artifacts", []) if isinstance(raw, dict) else []
    # Materialise sql results to CSV so downstream python_analysis can read them.
    # ``full_rows``（未截断）优先于 ``raw["rows"]``（已按上下文预算截断并可能带哨兵）。
    rows = full_rows if full_rows is not None else (raw.get("rows") if isinstance(raw, dict) else None)
    if tool in ("sql_query", "freeform") and ok and isinstance(raw, dict) and rows:
        import csv
        csv_path = Path(wd) / f"{step_id}.csv"
        cols = raw.get("columns") or list(rows[0].keys())
        # 哨兵行（``{"_truncated": N}``）是上下文预算标记，不是数据，绝不写进 CSV
        data_rows = [r for r in rows if isinstance(r, dict) and "_truncated" not in r]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(data_rows)
        raw["csv_path"] = str(csv_path)
        raw["csv_rows"] = len(data_rows)   # 落盘行数，供观测/对账
        artifacts = artifacts + [str(csv_path)]
    # E4/02 输出脱敏（**单一收口**）：CSV 已按完整数据落盘，此处只处理"进上下文"的那份。
    # 顺序不能反 —— 先落盘再脱敏，分析师本机产物必须可用。
    if isinstance(raw, dict):
        try:
            from ...core.security.masking import apply_masking

            raw, _masked = apply_masking(raw, tool=tool, step_id=step_id,
                                         session_id=session_id)
        except Exception as exc:  # 脱敏故障**失败即关闭**：丢掉行样本，绝不放行原始数据
            raw["rows"] = []
            raw["masking_error"] = str(exc)[:200]
    # AUTH/01：禁列整列剔除（连列名都不给）——与 E4/02 脱敏同一个收口点
    if ok and isinstance(raw, dict):
        try:
            from ..security.auth import current_principal
            from ..security.data_guard import drop_denied_columns

            _cleaned, _dropped = drop_denied_columns(raw, current_principal())
            if _dropped:
                raw["denied_columns_removed"] = _dropped
        except Exception:
            pass  # 权限层故障不打断工具调用（guard_sql 已在执行前把关）

    error = raw.get("error") if isinstance(raw, dict) and not ok else None
    record_tool_call(tool, time.time() - started, bool(ok))
    return ToolResult(
        step_id=step_id,
        tool=tool,
        status="SUCCESS" if ok else "FAILED",
        input=params,
        output=raw or {},
        execution_time_ms=int((time.time() - started) * 1000),
        error=error,
        error_class=None if ok else classify_error(error).value,
        attempts=attempt,
        artifacts=artifacts,
    )
