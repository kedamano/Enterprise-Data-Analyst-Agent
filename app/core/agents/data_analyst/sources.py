"""E1 traceability helpers — resolve an evidence ``sql_id`` back to a real query.

An evidence that quotes a number must link to an actual, successful ``sql_query``
step. Anything else (unknown id, a non-SQL step, a failed step) resolves to
``None``: better no trace than a fabricated one.
"""
from __future__ import annotations

from typing import Any, Optional

from .state import ToolResult


_SQL_TOOLS = {"sql_query", "freeform"}


def resolve_sql_source(sql_id: str, results: list[ToolResult]) -> Optional[ToolResult]:
    if not sql_id:
        return None
    for r in results:
        if r.step_id == sql_id and r.tool in _SQL_TOOLS and r.status == "SUCCESS":
            return r
    return None


def _is_numeric(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def unresolved_numeric_claims(findings: list[Any],
                              tool_results: list[ToolResult]) -> list[str]:
    """返回未满足溯源契约的数值 claim 描述列表。

    判定规则（E1 v1.0）：evidence.value 为数值（含数字字符串）时，
    必须携带能解析到成功 sql_query step 的 ``sql_id``，否则视为无效数值。
    """
    msgs: list[str] = []
    for f in findings:
        label = str(getattr(f, "finding", ""))[:40]
        for ev in (getattr(f, "evidence", None) or []):
            if not _is_numeric(getattr(ev, "value", None)):
                continue  # 非数值（解读/建议）不强求溯源
            sql_id = getattr(ev, "sql_id", None)
            if not sql_id:
                msgs.append(f"finding[{label}] 数值 evidence 缺 sql_id")
            elif resolve_sql_source(sql_id, tool_results) is None:
                msgs.append(f"finding[{label}] sql_id={sql_id} 无效/非成功SQL步骤")
    return msgs


def last_successful_sql_id(results: list[ToolResult]) -> Optional[str]:
    """最近一次成功的 sql/freeform step_id（按执行顺序取最后）。"""
    for r in reversed(results):
        if r.tool in _SQL_TOOLS and r.status == "SUCCESS":
            return r.step_id
    return None


def auto_trace(findings: list[Any], results: list[ToolResult]) -> list[Any]:
    """Analyst 后处理：给「数值型但没有 sql_id」的 evidence 自动补最近成功 SQL 的 step_id。

    有来源才补（last_successful_sql_id）；数值但查不到 SQL 时保持 None，
    交由溯源校验（unresolved_numeric_claims / eval）判定。
    """
    src = last_successful_sql_id(results)
    if src is None:
        return findings
    for f in findings:
        for ev in (getattr(f, "evidence", None) or []):
            if getattr(ev, "sql_id", None) is None and _is_numeric(getattr(ev, "value", None)):
                ev.sql_id = src
    return findings


def sql_of_step(step_id: str, results: list[ToolResult]) -> tuple[str, str, list]:
    """返回 (sql_text, sql_hash, rows_sample≤10) 供溯源展示；找不到返回空。"""
    r = resolve_sql_source(step_id, results)
    if r is None:
        return "", "", []
    inp = r.input or {}
    sql = str(inp.get("sql", ""))[:300]
    out = r.output or {}
    rows = out.get("rows") or []
    return sql, "", list(rows[:10])


def trace_coverage(findings: list[Any], results: list[ToolResult]) -> dict:
    """**唯一的溯源口径**：数值 claim 数 / 可溯源数 / 覆盖率 / 疑似幻觉率。

    离线（`app/eval/runner`）与在线（`/trace` API、SSE FINISH、`/metrics`）
    **必须都走这里**。此前的真问题：两边各算各的——

    | 位置 | 判据 |
    |---|---|
    | `trace_counts`（离线） | `sql_id` 能 resolve 到**真实 SQL step** |
    | `trace_manifest`（在线） | `sql_id` 非空 **且 `sql_of_step` 有返回** |

    两个定义会漂移，而**没有任何测试比对**——离线说 0.9、在线说 0.6 也没人知道。

    **语义边界（必须守住）**：零数值 claim 时 `rate` / `hallucination_rate` 为
    `None`（未定义），**不是 0**。把"没测到"报成"0 幻觉"是最典型的自欺——
    D38 的 `溯源 0/0` 就是这样全程判过的。
    """
    total = traced = 0
    for f in findings or []:
        for ev in (getattr(f, "evidence", None) or []):
            if not _is_numeric(getattr(ev, "value", None)):
                continue
            total += 1
            sql_id = getattr(ev, "sql_id", None)
            if sql_id and resolve_sql_source(sql_id, results) is not None:
                traced += 1
    rate = round(traced / total, 4) if total else None
    return {
        "numeric_claims": total,
        "traced_claims": traced,
        "rate": rate,
        "hallucination_rate": (round(1 - rate, 4) if rate is not None else None),
    }


def trace_manifest(state: Any) -> dict:
    """把一次运行的分析结果折叠成 claim→SQL→样本 的可视化清单。

    `coverage` **直接取自** :func:`trace_coverage`（唯一口径），不另算一套。
    """
    findings = getattr(getattr(state, "analysis", None), "findings", []) or []
    results = list(getattr(state, "tool_results", None) or [])
    claims = []
    for f in findings:
        evs = []
        for ev in (getattr(f, "evidence", None) or []):
            if not _is_numeric(getattr(ev, "value", None)):
                continue
            sql_id = getattr(ev, "sql_id", None)
            sql, _hash, rows = sql_of_step(sql_id or "", results)
            evs.append({"sql_id": sql_id or None, "sql": sql or None,
                        "rows_sample": rows, "value": getattr(ev, "value", None)})
        if evs:
            claims.append({"finding": getattr(f, "finding", ""), "evidence": evs})
    return {
        "objective": getattr(getattr(state, "context", None), "objective", ""),
        "claims": claims,
        "coverage": trace_coverage(findings, results),
    }


def append_citations(report: str, analysis: Any, results: list[ToolResult]) -> str:
    """给报告追加『数字来源』块：每条数值 evidence → [src: step_id]（有源才列）。"""
    findings = getattr(analysis, "findings", None) or []
    lines: list[str] = []
    for f in findings:
        label = str(getattr(f, "finding", "")).strip()
        for ev in (getattr(f, "evidence", None) or []):
            if not _is_numeric(getattr(ev, "value", None)):
                continue
            sql_id = getattr(ev, "sql_id", None)
            if sql_id and resolve_sql_source(sql_id, results) is not None:
                lines.append(f"- {label[:60]} — 数值 {getattr(ev,'value','')} [src: {sql_id}]")
    if not lines:
        return report
    block = "\n\n## 数字来源\n" + "\n".join(lines)
    return (report or "").rstrip() + block


def trace_counts(findings: list[Any], results: list[ToolResult]) -> tuple[int, int]:
    """返回 (数值 evidence 总数, 其中有有效 sql_id 的数量)。

    保留此签名（多处调用），实现**委托**给 :func:`trace_coverage` —— 单一口径。
    """
    cov = trace_coverage(findings, results)
    return cov["numeric_claims"], cov["traced_claims"]
