"""E3 session-dataset iteration — act on the previous result instead of re-running the chain.

Spec: docs/specs/E3/01-iteration.md（增量执行）、02-targeted-iteration.md（目标阶段 + force 开关）、
03-caliber-switch.md（口径/维度守卫）、04-baseline-writeback.md（产物回写基线）
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

_SQL_TOOLS = ("sql_query", "freeform")


class IterationUnsafe(RuntimeError):
    """增量请求不可安全就地执行 → 调用方回退全链（绝不静默少做）。"""


# --------------------------------------------------------------------------- #
# 增量意图分类（E3/02）
# --------------------------------------------------------------------------- #
DRILLDOWN = "drilldown"
DATE_CHANGE = "date_change"
GRANULARITY = "granularity"
FILTER = "filter"
GENERIC = "generic"

# 期间证据（需显式数字/期间词，避免「按月」被误判为改期）
_DATE_RE = re.compile(
    r"(20\d{2}\s*年)|(\d{4}\s*[-/]\s*\d{1,2})|(近\s*\d+\s*[天周月年])|([qQ][1-4])"
    r"|(本季度|上个季度|上季度|去年同期|年初|年末|上个月|本月|改期)"
)
_GRAN_HINTS = ("粒度", "按天", "按日", "按周", "按月", "按季度", "按季", "按年",
               "汇总到", "聚合到")
_DRILL_HINTS = ("下钻", "钻取", "细分", "拆到", "拆成", "展开到", "到明细")
_FILTER_HINTS = ("只看", "只保留", "筛选", "过滤", "排除", "剔除", "限定")
_TIME_GRAIN_HINTS = ("按天", "按日", "按周", "按月", "按季度", "按季", "按年",
                     "天粒度", "周粒度", "月粒度", "季度粒度", "年粒度")

# 度量 / 维度可用性词表（E3/03）：上一结果是某次 SELECT 的投影，只含那次查询的列。
# 请求新度量/新维度 → 必须回数仓重取，绝不拿旧结果硬算。只用双字词，避免
# 「2024年3月」被当成「要月份维度」、「按月」被当成「要月维度」。
_MEASURE_HINTS: dict[str, tuple[str, ...]] = {
    "营收": ("revenue", "sales", "amount", "营收", "收入", "销售额"),
    "收入": ("revenue", "sales", "amount", "营收", "收入", "销售额"),
    "销售额": ("revenue", "sales", "amount", "营收", "收入", "销售额"),
    "订单数": ("orders", "order_cnt", "order_count", "订单"),
    "订单量": ("orders", "order_cnt", "order_count", "订单"),
    "客户数": ("customers", "customer_cnt", "customer", "客户"),
    "销量": ("qty", "quantity", "units", "销量"),
    "利润": ("profit", "margin", "利润"),
}
_DIM_HINTS: dict[str, tuple[str, ...]] = {
    "城市": ("city", "城市"),
    "区域": ("region", "区域"),
    "地区": ("region", "地区"),
    "产品": ("product", "产品"),
    "品类": ("category", "品类", "类别"),
    "类别": ("category", "品类", "类别"),
    "渠道": ("channel", "渠道"),
    "月份": ("month", "月份"),
    "日期": ("date", "日期"),
}

# 目标阶段：命中后**只**执行这些阶段（发现链一律跳过）
_STAGES = {
    FILTER: ["filter"],
    DRILLDOWN: ["filter", "groupby"],
    DATE_CHANGE: ["time_slice"],
    GRANULARITY: ["regrain", "aggregate"],
    GENERIC: ["custom"],
}
_SKIPS = ["planner", "schema_search", "sql_query"]
_STAGE_INSTRUCTION = {
    FILTER: "只对 df 做行筛选（按用户条件），不要重新聚合、不要重新取数。",
    DRILLDOWN: "在 df 上先按条件筛选、再按用户指定维度分组汇总（下钻），不要重新取数。",
    DATE_CHANGE: "只对 df 做时间切片（用户指定期间），不要重新取数。",
    GRANULARITY: "把 df 换算为用户指定粒度后重新汇总，不要重新取数。",
    GENERIC: "在 df 上完成用户要求的增量计算，不要重新取数。",
}

_FOLLOWUP_HINTS = (
    "基于上一结果", "上一个结果", "基于这个结果", "这个结果", "在此基础上", "在这份数据上",
    "同样数据", "改为", "改成", "只看", "换成", "再按", "接着", "继续按", "下钻到",
)
_FORCE_HINTS = ("重新完整分析", "全量重跑", "重新查", "从头分析", "完整分析一遍")


def is_followup(query: str) -> bool:
    q = "".join((query or "").lower().split())
    if any(h.lower() in q for h in _FORCE_HINTS):
        return False
    return any(h.lower() in q for h in _FOLLOWUP_HINTS)


def classify_followup(query: str) -> str:
    """增量意图分类（确定性）。仅在 is_followup 为真时有意义。"""
    q = "".join((query or "").lower().split())
    if _DATE_RE.search(q):
        return DATE_CHANGE
    if any(h.lower() in q for h in _GRAN_HINTS):
        return GRANULARITY
    if any(h.lower() in q for h in _DRILL_HINTS):
        return DRILLDOWN
    if any(h.lower() in q for h in _FILTER_HINTS):
        return FILTER
    return GENERIC


def iteration_plan(kind: str) -> dict:
    """kind → {kind, stages, skips}：目标阶段明确，发现链跳过。"""
    kind = kind if kind in _STAGES else GENERIC
    return {"kind": kind, "stages": list(_STAGES[kind]), "skips": list(_SKIPS)}


# --------------------------------------------------------------------------- #
# 安全守卫（不满足 → 回退全链）
# --------------------------------------------------------------------------- #
_YEAR_RE = re.compile(r"(20\d{2})")


def _requested_years(query: str) -> set[int]:
    return {int(y) for y in _YEAR_RE.findall(query or "")}


def _unavailable(query: str, columns: list, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    """请求里提到、但上一结果列中找不到的度量/维度词。"""
    cols = [str(c).lower() for c in (columns or [])]
    missing: list[str] = []
    for word, patterns in mapping.items():
        if word not in query:
            continue
        if not any(p.lower() in c for p in patterns for c in cols):
            missing.append(word)
    return missing


def guard_iteration(ds: dict, query: str, kind: str) -> tuple[bool, dict]:
    """执行前的确定性校验：返回 (是否可增量, guard 记录)。"""
    guard: dict = {"kind": kind, "reason": None, "missing": []}
    if not ds or not ds.get("csv"):
        guard["reason"] = "无上一结果数据集"
        return False, guard
    q = "".join((query or "").lower().split())

    if kind == DATE_CHANGE:
        dmin, dmax = ds.get("date_min"), ds.get("date_max")
        if not ds.get("date_col") or not dmin or not dmax:
            guard["reason"] = "数据集无日期列，无法确认可切片范围"
            return False, guard
        lo, hi = str(dmin)[:4], str(dmax)[:4]
        if not (lo.isdigit() and hi.isdigit()):
            guard["reason"] = f"数据集日期范围不可解析：{dmin}~{dmax}"
            return False, guard
        for y in _requested_years(query):
            if not (int(lo) <= y <= int(hi)):
                guard["reason"] = f"请求期间 {y} 越出数据集范围 {lo}~{hi}"
                return False, guard

    if kind == GRANULARITY and any(h.lower() in q for h in _TIME_GRAIN_HINTS) \
            and not ds.get("date_col"):
        guard["reason"] = "数据集无日期列，无法换算时间粒度"
        return False, guard

    # E3/03：新口径 / 新维度都不在上一结果的列里 → 必须重新取数（N2/N3）
    missing = _unavailable(q, ds.get("columns"), _MEASURE_HINTS) \
        + _unavailable(q, ds.get("columns"), _DIM_HINTS)
    if missing:
        guard["missing"] = missing
        guard["reason"] = f"上一结果缺少 {'、'.join(missing)}（新口径/维度需重新取数）"
        return False, guard

    return True, guard


def _last_data_step(state: Any):
    for r in reversed(getattr(state, "tool_results", None) or []):
        out = getattr(r, "output", None) or {}
        if getattr(r, "tool", "") in _SQL_TOOLS and getattr(r, "status", "") == "SUCCESS" \
                and out.get("csv_path"):
            return r
    return None


def _csv_columns(csv_path: str) -> list[str]:
    """只读表头取列名（0 行结果时 rows 为空，仍需列信息供守卫判定）。"""
    try:
        import pandas as pd

        return [str(c) for c in pd.read_csv(csv_path, nrows=0).columns]
    except Exception:
        return []


def _sample_rows(csv_path: str, n: int = 20) -> list[dict]:
    try:
        import pandas as pd

        return pd.read_csv(csv_path, nrows=n).to_dict("records")
    except Exception:
        return []


def _fresh_csvs(session_id: str, since: float) -> list[str]:
    """本步写入/覆盖的 CSV（按 mtime ≥ since 判定，取最新）。

    用时间而非「文件名是否新出现」：模型常沿用固定文件名（如 ``out.csv``）原地覆盖，
    只比文件集合会漏判；而上一轮/上一会话遗留的旧文件 mtime 早于本步，天然被排除。
    """
    try:
        from ....core.tools import session_workdir

        wd = session_workdir(session_id)
        fresh = [p for p in wd.iterdir()
                 if p.suffix.lower() == ".csv" and p.stat().st_mtime >= since - 1.0]
        fresh.sort(key=lambda p: p.stat().st_mtime)
    except Exception:
        return []
    return [str(p) for p in fresh]


def _write_back_baseline(state: Any, step: Any, prev_ds: Optional[dict],
                         since: float) -> Optional[dict]:
    """E3/04：增量产物若含本步产出的 CSV → 前移为下一轮「上一结果」基线。

    注意：不能用 ``ToolResult.artifacts``——它列的是整个 workdir 的 csv/png/json，
    含输入 CSV 与历史产物，取用会回写错文件。
    """
    fresh = _fresh_csvs(getattr(state, "session_id", ""), since)
    if not fresh:
        return None
    csv = fresh[-1]
    columns = _csv_columns(csv)
    if not columns:
        return None
    date_col, date_min, date_max = _date_bounds(csv)
    ds = {
        "csv": csv,
        "columns": columns,
        "rows": _sample_rows(csv),
        "sql": "",
        "step_id": step.step_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "derived_from": (prev_ds or {}).get("step_id"),
        "date_col": date_col,
        "date_min": date_min,
        "date_max": date_max,
    }
    state.last_dataset = ds
    _remember(state, ds)
    return ds


def _date_bounds(csv_path: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(date_col, min, max)：从数据集 CSV 探测日期列与范围（不可解析则 None）。"""
    try:
        import pandas as pd

        df = pd.read_csv(csv_path, nrows=200000)
    except Exception:
        return None, None, None
    for col in df.columns:
        name = str(col).lower()
        if "date" not in name and "日期" not in str(col) and "time" not in name:
            continue
        series = pd.to_datetime(df[col], errors="coerce")
        if series.notna().any():
            return str(col), str(series.min().date()), str(series.max().date())
    return None, None, None


def _remember(state: Any, ds: dict) -> None:
    """会话数据集写回 short_term（best-effort，记忆层故障不影响主链）。"""
    try:
        from ....core.memory import short_term

        short_term.put(getattr(state, "session_id", ""), "last_dataset", ds)
    except Exception:
        pass


def save_last_dataset(state: Any) -> Optional[dict]:
    """成功数据步后落「会话数据集」（short_term）。"""
    step = _last_data_step(state)
    if step is None:
        return None
    out = step.output or {}
    rows = out.get("rows") or []
    date_col, date_min, date_max = _date_bounds(out.get("csv_path") or "")
    ds = {
        "csv": out.get("csv_path"),
        "columns": list(rows[0].keys()) if rows else _csv_columns(out.get("csv_path") or ""),
        "rows": rows[:20],
        "sql": (getattr(step, "input", None) or {}).get("sql", ""),
        "step_id": step.step_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "derived_from": None,  # 直接来自 sql_query/freeform；增量产物见 E3/04
        "date_col": date_col,
        "date_min": date_min,
        "date_max": date_max,
    }
    state.last_dataset = ds
    _remember(state, ds)
    return ds


def load_last_dataset(session_id: str) -> Optional[dict]:
    try:
        from ....core.memory import short_term
        return short_term.get(session_id, "last_dataset")
    except Exception:
        return None


def deliver_iteration(state: Any, max_attempts: int = 2, kind: str | None = None) -> Any:
    """增量执行：以 last_dataset.csv 为源，只执行目标阶段 → 沙箱验证 → 精简交付。"""
    from ....core.tools import execute_tool
    from ....infrastructure.llm.router import get_llm

    ds = getattr(state, "last_dataset", None) or load_last_dataset(getattr(state, "session_id", ""))
    kind = kind or classify_followup(state.user_query)
    plan = iteration_plan(kind)
    ok, guard = guard_iteration(ds or {}, state.user_query, kind)
    if not ok:
        raise IterationUnsafe(guard["reason"])  # 交由调用方回退全链

    system = ("You are a Python data-analysis writer. Output ONLY Python code. "
              "A DataFrame `df` is preloaded from the PREVIOUS result CSV.")
    user = (f"上一结果列：{ds.get('columns')}\n用户增量要求：{state.user_query}\n"
            f"本次只执行目标阶段：{', '.join(plan['stages'])}。{_STAGE_INSTRUCTION[kind]}\n"
            f"（数据文件：{ds['csv']}）")
    feedback: list[str] = []
    code = ""
    note = ""
    for attempt in range(1, max_attempts + 1):
        u = user + (("\n\n上次失败：" + feedback[-1]) if feedback else "")
        try:
            code = (get_llm().complete(system, u, stage="python_code_gen", json_mode=False) or "").strip()
        except Exception as exc:
            feedback.append(str(exc))
            continue
        since = time.time()  # E3/04：判定本步产物（mtime 基准）
        res = execute_tool("iter_eval", "python_analysis", {"code": code, "data_csv": ds["csv"]},
                           getattr(state, "session_id", ""))
        state.tool_results.append(res)
        if res.status == "SUCCESS":
            out = res.output or {}
            _write_back_baseline(state, res, ds, since)  # E3/04：产物成为下一轮基线
            note = f"> ✅ 基于上一结果增量执行（第 {attempt} 次）；stdout：{(out.get('stdout') or '')[:200]}"
            break
        feedback.append(f"[{attempt}] {res.error}")
    else:
        note = f"> ⚠ 增量执行未通过（{max_attempts} 次）。最后错误：{feedback[-1] if feedback else '未知'}"

    state.mode = "iteration"
    state.iteration = {"kind": kind, "stages": plan["stages"], "skips": plan["skips"],
                       "attempts": attempt, "guard": guard}
    state.report = (f"**增量分析（基于上一结果 · {kind}）**：{getattr(state, 'user_query', '')}\n\n"
                    f"```python\n{code}\n```\n\n{note}")

    # E4/03：迭代轮绕过 Reflection→Reporter，故在此做一次轻量口径检查。
    # 本轮改了时间切片/粒度后再做环比/同比 = 跨口径比较，是结论错误。
    try:
        from .caliber import caliber_check, caliber_notes

        check = caliber_check(state.analysis, getattr(state, "context", None),
                              iteration=state.iteration, report=state.report)
        drift = [i for i in check.issues if i.kind == "iteration_drift"]
        if drift:
            state.analysis.quality_notes = list(state.analysis.quality_notes or []) + \
                caliber_notes(check)
            state.metadata["caliber_issues"] = [i.model_dump() for i in check.issues]
            state.report += "\n\n> ⚠ **口径提示**：" + drift[0].detail
    except Exception as exc:  # 口径检查故障绝不打断增量主流程
        state.metadata["caliber_error"] = str(exc)

    state.status = "FINISH"
    return state
