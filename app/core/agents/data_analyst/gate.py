"""E4/04 质量门禁 — 把 `dataset_profile` 的质量基元变成**决策与披露**。

Spec: docs/specs/E4/04-quality-gate.md

范式与 `sources.py` 一致：**纯函数 + 违规清单**，不花 LLM、不发新查询，只消费已有的
``tool_results``。挂接点在 ``nodes.run_reflection`` 出口。

三级语义（详见 spec §1.1）：
- ``ANNOTATE``  结论可用但必须披露（稀疏日期 / 口径未声明）→ 不改决策
- ``REPLAN``    缺证据且**可补查** → 抬到 REPLAN
- ``BLOCK``     结论按当前数据**必然错**（如笛卡尔放大的求和）→ 抬到 REPLAN 去修；
                **重试额度用尽时必须 FAILED，不得降级为 best-effort 报告**
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel

from .state import AnalysisResult, ReflectionDecision, ReflectionResult, ToolResult

Severity = Literal["BLOCK", "REPLAN", "ANNOTATE"]

# 严重度排序：抬升决策时取最严的一条
_SEV_RANK = {"ANNOTATE": 0, "REPLAN": 1, "BLOCK": 2}
_DECISION_RANK = {ReflectionDecision.PASS: 0, ReflectionDecision.REPLAN: 1,
                  ReflectionDecision.FAIL: 2}

_DEFAULT_AMP_THRESHOLD = 1.5
_DEFAULT_NULL_RATIO = 0.3

_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
# FROM 子句正文（截至下一个顶层子句关键字）。用于识别**逗号连接** `FROM a, b`。
# 必须截断：否则 `WHERE x IN (1, 2)` / `GROUP BY a, b` 里的逗号会被误判成多表连接。
_FROM_CLAUSE_RE = re.compile(
    r"\bfrom\b(.*?)(?=\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bunion\b|$)",
    re.IGNORECASE | re.DOTALL)
_FROM_ITEM_RE = re.compile(r"(?:^|,)\s*([A-Za-z_][A-Za-z0-9_]*)")
# "有聚合主张" 才判主键/粒度问题——否则连"数据覆盖 5 个区域"都会被算成聚合
_AGG_RE = re.compile(r"(sum|avg|count|total|合计|汇总|总计|平均|求和|去重|计数|加总)", re.IGNORECASE)
_TREND_RE = re.compile(r"(趋势|走势|连续|每天|逐日|逐周|逐月|时间序列|近\s*\d+\s*[天周月])")
_ROW_LEVEL_RE = re.compile(r"(每行|逐条|明细|一条记录|单条|行级)")
_SQL_TOOLS = ("sql_query", "freeform")


class GateIssue(BaseModel):
    code: str
    severity: Severity
    detail: str
    metric: Optional[str] = None
    fixable: bool = False


# --------------------------------------------------------------------------- #
# join 放大
# --------------------------------------------------------------------------- #
def _table_rows(tool_results: list[ToolResult]) -> dict[str, int]:
    """schema_search 已带回各表行数——用它当放大检测的基线，无需额外查询。"""
    rows: dict[str, int] = {}
    for r in tool_results:
        if r.tool == "schema_search" and r.status == "SUCCESS":
            for t in (r.output or {}).get("tables") or []:
                if isinstance(t, dict) and t.get("table") and t.get("row_count"):
                    rows.setdefault(str(t["table"]), int(t["row_count"]))
    return rows


def _result_rows(r: ToolResult) -> int:
    out = r.output or {}
    rows = out.get("row_count")
    if isinstance(rows, int):
        return rows
    listed = out.get("rows")
    return len(listed) if isinstance(listed, list) else 0


def _has_join(sql: str) -> bool:
    """SQL 是否构成**多表连接**。

    逗号连接（`FROM a, b`）也算 join：它正是"忘写 join 条件"最常见的写法，
    早期版本只认 `join` 关键字（`_JOIN_RE`），导致最该被抓的笛卡尔积反而逃过检测。
    """
    sql = sql or ""
    if _JOIN_RE.search(sql):
        return True
    m = _FROM_CLAUSE_RE.search(sql)
    return bool(m and "," in m.group(1))


def _join_tables(sql: str) -> set[str]:
    """SQL 里出现的表名（小写）。含 FROM/JOIN 后的表 与 FROM 子句内逗号分隔的其余表。"""
    tables = {m.lower() for m in _TABLE_RE.findall(sql or "")}
    m = _FROM_CLAUSE_RE.search(sql or "")
    if m:
        tables |= {t.lower() for t in _FROM_ITEM_RE.findall(m.group(1))}
    return tables


def join_amplification_facts(tool_results: list[ToolResult],
                             threshold: float = _DEFAULT_AMP_THRESHOLD) -> list[dict]:
    """放大事实清单。两条路径：① profile 声明式（E4/01）② 事后启发式（不额外查询）。

    启发式只对**多表连接的 SQL 步骤**生效（`join` 关键字**或** `FROM a, b` 逗号连接），
    基线取所涉表的最大行数：
    维表 join 事实表（N:1）行数不变 → factor≈1 不误报；忘写 join 条件 → factor=维表行数 → 报警。
    拿不到基线（无 schema_search 行数 / 解析不出表名）→ **不判**（宁缺勿滥）。
    """
    facts: dict[str, dict] = {}
    for r in tool_results:
        if r.tool == "dataset_profile" and r.status == "SUCCESS":
            amp = (r.output or {}).get("join_amplification") or {}
            if amp.get("amplified") and amp.get("factor"):
                facts[r.step_id] = {"step_id": r.step_id, "factor": float(amp["factor"]),
                                    "result_rows": amp.get("result_rows"),
                                    "baseline": amp.get("base_rows"), "source": "declared"}
    base_rows = _table_rows(tool_results)
    if base_rows:
        for r in tool_results:
            if r.tool not in _SQL_TOOLS or r.status != "SUCCESS" or r.step_id in facts:
                continue
            sql = str((r.input or {}).get("sql") or "")
            if not _has_join(sql):
                continue
            tables = _join_tables(sql)
            known = [n for t, n in base_rows.items() if t.lower() in tables]
            result_rows = _result_rows(r)
            if not known or not result_rows:
                continue
            baseline = max(known)
            factor = round(result_rows / baseline, 4)
            if factor >= threshold:
                facts[r.step_id] = {"step_id": r.step_id, "factor": factor,
                                    "result_rows": result_rows, "baseline": baseline,
                                    "source": "heuristic"}
    return list(facts.values())


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
def _texts(analysis: AnalysisResult) -> str:
    parts: list[str] = []
    for f in analysis.findings or []:
        parts.append(str(getattr(f, "finding", "")))
        parts.append(str(getattr(f, "interpretation", "")))
    for h in getattr(analysis, "hypotheses", None) or []:
        parts.append(str(getattr(h, "hypothesis", "")))
    return " ".join(parts)


def _used_sql_ids(analysis: AnalysisResult) -> set[str]:
    ids: set[str] = set()
    for f in analysis.findings or []:
        for ev in getattr(f, "evidence", None) or []:
            if getattr(ev, "sql_id", None):
                ids.add(str(ev.sql_id))
    return ids


def profile_gate(tool_results: list[ToolResult], analysis: AnalysisResult, *,
                 amp_threshold: float = _DEFAULT_AMP_THRESHOLD,
                 null_ratio: float = _DEFAULT_NULL_RATIO) -> list[GateIssue]:
    """纯函数：已产出的 tool_results + 分析结论 → 质量 issue 清单。

    阈值由调用方从 settings 传入（保持本模块可纯函数测试）。

    全部规则都是**双条件**（有事实 AND 有主张）。单条件会把样例库点亮——
    `fact_sales` 的日期天然稀疏（52/358 天），"sparse ⇒ 报警"会在每个用例上触发。
    """
    issues: list[GateIssue] = []
    text = _texts(analysis)
    used = _used_sql_ids(analysis)

    # ① join 放大
    for fact in join_amplification_facts(tool_results, threshold=amp_threshold):
        in_conclusion = fact["step_id"] in used
        issues.append(GateIssue(
            code="join_amplified_used" if in_conclusion else "join_amplified_unused",
            severity="BLOCK" if in_conclusion else "ANNOTATE",
            detail=(f"步骤 {fact['step_id']} 的结果 {fact['result_rows']} 行 / 基线 "
                    f"{fact['baseline']} 行 = {fact['factor']}×，疑似 join 放大；"
                    + ("该结果已用于结论，数值不可信（需修正 join 条件后重算）"
                       if in_conclusion else "该结果未进入结论，仅作提示")),
            fixable=True))

    profiles = [r for r in tool_results
                if r.tool == "dataset_profile" and r.status == "SUCCESS"
                and isinstance(r.output, dict) and r.output.get("ok")]

    # ②~⑤ 每条规则**独立**判定（早期版本把它们嵌在上一条的条件里，
    #     结果"主键唯一"时粒度/日期/缺失的检查全被跳过）
    for p in profiles:
        ku = (p.output or {}).get("key_uniqueness") or {}
        agg_claim = bool(_AGG_RE.search(text))

        # ② 主键不唯一 + 聚合主张（去重计数/求和会因重复行失真）
        if ku.get("is_unique") is False and agg_claim:
            ratio = ku.get("duplicate_ratio")
            near = isinstance(ratio, (int, float)) and ratio < 0.01
            key = "、".join(ku.get("declared_key") or []) or "声明键"
            issues.append(GateIssue(
                code="key_not_unique", severity="ANNOTATE" if near else "REPLAN",
                detail=(f"声明键 [{key}] 不唯一（重复 {ku.get('duplicate_rows')} 行"
                        f"{f'，占比 {ratio}' if ratio is not None else ''}），"
                        "对含重复键的结果做计数/求和会失真，需先去重或改用明细粒度"),
                metric=key, fixable=True))

        # ③ 粒度误读
        if ku.get("grain") == "aggregated" and _ROW_LEVEL_RE.search(text):
            issues.append(GateIssue(
                code="grain_misread", severity="ANNOTATE",
                detail="该结果每行是一组聚合（无可用行键），但结论按「逐条/明细」表述，粒度口径不一致",
                fixable=False))

        # ④ 日期稀疏 + 时间连续性主张
        dc = (p.output or {}).get("date_continuity") or {}
        if dc.get("sparse") and _TREND_RE.search(text):
            issues.append(GateIssue(
                code="date_sparse_claimed", severity="ANNOTATE",
                detail=(f"日期列 {dc.get('column')} 仅覆盖 {dc.get('distinct_days')}/"
                        f"{dc.get('expected_days')} 天（稀疏），结论含时间趋势/连续性表述，"
                        "需说明缺口或改用同口径周期对比"),
                metric=dc.get("column"), fixable=False))

        # ⑤ 高缺失列被提及
        for col, meta in ((p.output or {}).get("columns") or {}).items():
            ratio = (meta or {}).get("null_ratio") or 0
            if ratio >= null_ratio and str(col) in text:
                issues.append(GateIssue(
                    code="null_high_on_group", severity="ANNOTATE",
                    detail=f"列 {col} 缺失率 {ratio}，且被用于结论，需说明缺失处理方式",
                    metric=str(col), fixable=False))
    return issues


def apply_gate(issues: list[GateIssue],
               reflection: Optional[ReflectionResult]) -> tuple[ReflectionDecision, list[str]]:
    """把 issue 施加到 LLM 的决策上——**只收紧，不放松**。

    返回 ``(决策, 披露文本)``。``BLOCK`` 抬到 REPLAN（让 planner 带着诊断去修）；
    是否在额度用尽时判 FAILED 由调用方按 ``gate_block`` 决定（spec §1.1）。
    """
    base = reflection.decision if reflection else ReflectionDecision.REPLAN
    if not issues:
        return base, []
    floor = max((i.severity for i in issues), key=lambda s: _SEV_RANK[s])
    target = {"ANNOTATE": base,
              "REPLAN": ReflectionDecision.REPLAN,
              "BLOCK": ReflectionDecision.REPLAN}[floor]
    decision = base if _DECISION_RANK[base] >= _DECISION_RANK[target] else target
    notes = [f"[{i.code}] {i.detail}" for i in issues]
    return decision, notes


def is_blocked(issues: list[GateIssue]) -> bool:
    """是否存在 BLOCK 级问题（决定重试额度用尽时 FAILED 而非 best-effort 出报告）。"""
    return any(i.severity == "BLOCK" for i in issues)
