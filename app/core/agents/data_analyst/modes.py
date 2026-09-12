"""ROUTE output-intent detection & light terminals (spec ROUTE/01).

Deterministic detection first (explicit instruction / output_format), fallback
``full``. Light terminals produce SQL / short-answer output without running the
full Analyst→Reflection→Reporter heavy chain.
"""
from __future__ import annotations

from typing import Any, Optional

FULL = "full"
SQL_ONLY = "sql_only"
QUICK = "quick_answer"
DOC = "markdown_doc"
PY = "python_code"

_SQL_HINTS = ("只要sql", "只给sql", "输出sql", "给我sql", "写sql", "生成sql",
              "sql语句", "sql 语句", "只要 SQL", "输出 SQL", "给 SQL")
_PY_HINTS = ("python代码", "写python", "输出python", "python 代码", "python代码", "写 python", "输出 python",
             "python脚本", "python 脚本", "写个python", "写个 python", "清洗", "处理csv", "处理excel",
             "分析这份csv", "用python", "python分析")
_DOC_HINTS = ("markdown文档", "markdown 文档", "写文档", "纪要", "输出文档", "markdown")
_QA_HINTS = ("简短回答", "一句话", "简单回答", "快速回答", "直接回答", "直接告诉我",
             "是什么", "是多少", "哪个最", "几个", "多少")
_OUTPUT_MAP = {"sql": SQL_ONLY, "markdown": DOC, "python": PY, "md": DOC}


def detect_mode(user_query: str, context: Any = None) -> str:
    q = (user_query or "").lower()
    for h in _SQL_HINTS:
        if h.lower() in q:
            return SQL_ONLY
    for h in _PY_HINTS:
        if h.lower() in q:
            return PY
    for h in _DOC_HINTS:
        if h.lower() in q:
            return DOC
    for h in _QA_HINTS:
        if h.lower() in q:
            return QUICK
    fmt = (getattr(context, "output_format", None) or "") if context else ""
    if fmt:
        return _OUTPUT_MAP.get(str(fmt).lower(), FULL)
    return FULL


# --------------------------------------------------------------------------- #
# 轻量终态
# --------------------------------------------------------------------------- #
def _last_sql_step(results: list[Any]):
    for r in reversed(results):
        tool = getattr(r, "tool", "")
        if tool in ("sql_query", "freeform") and getattr(r, "status", "") == "SUCCESS":
            return r
    return None


def _sql_of(step) -> str:
    inp = getattr(step, "input", None) or {}
    sql = str(inp.get("sql", "")).strip()
    if not sql:  # 兜底：取 output 可能有的 query 说明
        return sql
    return sql


def terminal_sql_only(state: Any) -> Any:
    """终态：输出只读 SQL 代码块 + 行数/预览（跳过 analyst/reflection/reporter）。"""
    step = _last_sql_step(getattr(state, "tool_results", None) or [])
    sql = _sql_of(step) if step else ""
    n = 0
    sample = ""
    if step is not None:
        out = getattr(step, "output", None) or {}
        rows = out.get("rows") or []
        n = len(rows)
        if rows:
            cols = list(rows[0].keys())
            sample = "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols[:6]) + " |"
                               for r in rows[:3])
    state.report = (f"```sql\n{sql}\n```\n\n"
                    f"> 已只读执行并校验，返回 {n} 行。\n"
                    + (f"\n预览：\n| {(' | '.join(list((rows[0].keys() if rows else []))[:6]))} |\n{sample}\n"
                       if rows else ""))
    state.status = "FINISH"
    return state


def terminal_quick(state: Any) -> Any:
    """终态：简短回答（保留取数，跳过 Reflection 与长报告）。"""
    step = _last_sql_step(getattr(state, "tool_results", None) or [])
    rows = []
    if step is not None:
        rows = (getattr(step, "output", None) or {}).get("rows") or []
    obj = getattr(getattr(state, "context", None), "objective", "") or getattr(state, "user_query", "")
    lines = [f"**快速回答** — {obj}", ""]
    if rows:
        lines.append(f"查询返回 {len(rows)} 行，前 {min(3, len(rows))} 行如下：")
        for r in rows[:3]:
            lines.append("- " + "，".join(f"{k}={v}" for k, v in list(r.items())[:6]))
    else:
        lines.append("未取到可用的数据行。")
    state.report = "\n".join(lines)
    state.status = "FINISH"
    return state


# --------------------------------------------------------------------------- #
# ROUTE/02 ExecutionPlan：任务分类 + 计划模板（确定性兜底）
# --------------------------------------------------------------------------- #
TASK_SQL = "sql"
TASK_SQL_OPT = "sql_optimization"
TASK_PYTHON = "python"
TASK_MD = "markdown_report"
TASK_BIZ = "business_analysis"
TASK_EXPLORE = "data_exploration"
TASK_METRIC = "metric_definition"
TASK_MODEL = "data_modeling"
TASK_VIZ = "visualization"
TASK_INTERP = "data_interpretation"
TASK_QUICK = "quick_answer"
TASK_FULL = "full_analysis"

_WORKFLOWS: dict[str, list[str]] = {
    TASK_SQL: ["understand_metric", "generate_sql", "validate_sql"],
    TASK_SQL_OPT: ["understand_sql", "check_correctness", "optimize", "output_sql"],
    TASK_PYTHON: ["understand_data", "design_logic", "generate_code", "sandbox_validate"],
    TASK_MD: ["query_data", "aggregate", "analyze", "write_markdown"],
    TASK_BIZ: ["define_metrics", "query_data", "compare_period", "drill_dimension",
               "locate_anomaly", "root_cause", "insight", "recommendation"],
    TASK_EXPLORE: ["overview", "quality", "stats", "distributions", "anomalies", "insight"],
    TASK_METRIC: ["define_metric", "formula", "grain", "filters", "sql_impl"],
    TASK_MODEL: ["entities", "tables", "keys", "relations", "metrics", "ddl"],
    TASK_VIZ: ["metric", "dimension", "chart_type", "layout"],
    TASK_INTERP: ["understand_result", "anomaly", "explain", "conclusion"],
    TASK_QUICK: ["query_min", "answer"],
    TASK_FULL: ["discover", "plan", "execute", "analyze", "reflect", "report"],
}
_REQUIRES: dict[str, dict] = {
    TASK_SQL: {"data": False, "sql": True, "python": False, "report": False},
    TASK_SQL_OPT: {"data": False, "sql": True, "python": False, "report": False},
    TASK_PYTHON: {"data": False, "sql": False, "python": True, "report": False},
    TASK_MD: {"data": True, "sql": True, "python": False, "report": True},
    TASK_BIZ: {"data": True, "sql": True, "python": False, "report": True},
    TASK_EXPLORE: {"data": True, "sql": True, "python": True, "report": False},
    TASK_METRIC: {"data": False, "sql": True, "python": False, "report": False},
    TASK_MODEL: {"data": False, "sql": False, "python": False, "report": False},
    TASK_VIZ: {"data": True, "sql": True, "python": True, "report": False},
    TASK_INTERP: {"data": True, "sql": False, "python": False, "report": False},
    TASK_QUICK: {"data": True, "sql": True, "python": False, "report": False},
    TASK_FULL: {"data": True, "sql": True, "python": False, "report": True},
}
_DELIVERABLE_DEFAULT: dict[str, list[str]] = {
    TASK_SQL: ["sql"], TASK_SQL_OPT: ["sql"], TASK_PYTHON: ["python"],
    TASK_MD: ["markdown"], TASK_BIZ: ["markdown"], TASK_EXPLORE: ["markdown", "table"],
    TASK_METRIC: ["markdown", "sql"], TASK_MODEL: ["markdown", "ddl"],
    TASK_VIZ: ["markdown", "python"], TASK_INTERP: ["markdown"], TASK_QUICK: ["text"],
    TASK_FULL: ["markdown"],
}

_ORDERED_RULES = (
    (TASK_SQL_OPT, ("优化sql", "优化这段sql", "优化一下sql", "修改这段sql", "修改sql", "sql慢", "sql优化")),
    (TASK_SQL, ("只要sql", "只给sql", "输出sql", "生成sql", "帮我写sql", "写个sql", "写一个sql",
                "写sql", "sql语句", "的sql", "给sql", "统计复购率", "计算dau", "复购率sql",
                "留存率sql", "转化率sql")),
    (TASK_PYTHON, ("python代码", "写python", "输出python", "python脚本", "清洗数据", "清洗这份数据",
                   "处理excel", "处理csv", "分析这份csv", "写代码", "python分析", "python脚本分析")),
    (TASK_MD, ("做周报", "写周报", "周报", "月报", "输出报告", "出一份报告", "分析报告",
               "总结数据", "整理成文档", "markdown文档", "输出markdown", "业务总结")),
    (TASK_BIZ, ("为什么", "原因", "下降", "上升", "下滑", "流失", "哪个渠道好", "转化率为什么", "诊断", "对比分析")),
    (TASK_EXPLORE, ("看看这份", "看看这个", "探索", "这个数据集", "有没有异常", "这份数据")),
    (TASK_METRIC, ("怎么定义", "怎么算", "怎么计算", "口径", "定义复购", "指标定义", "设计电商核心指标")),
    (TASK_MODEL, ("设计模型", "模型设计", "数仓模型", "应该有哪些表", "画像标签", "用户分析模型", "数据模型", "建模")),
    (TASK_VIZ, ("可视化", "图表", "dashboard", "看板", "画一张", "bi看板")),
    (TASK_INTERP, ("这个结果说明", "解释这个", "结果正常吗", "帮我解释")),
    (TASK_QUICK, ("简短回答", "一句话", "直接回答", "直接告诉我", "是多少", "哪个最高", "哪个区域")),
)


def classify_task(user_query: str) -> str:
    q = "".join((user_query or "").lower().split())  # 去空白，规避“只给 sql / 的 SQL”变体
    for task, hints in _ORDERED_RULES:
        for h in hints:
            if h in q:
                return task
    return TASK_FULL


def _extra_deliverables(q: str) -> list[str]:
    out: list[str] = []
    if "sql" in q.lower() or "sql语句" in q.lower():
        out.append("sql")
    if "python" in q.lower():
        out.append("python")
    if "markdown" in q.lower() or "文档" in q or "报告" in q or "周报" in q:
        out.append("markdown")
    return out


def build_plan(user_query: str) -> dict:
    task = classify_task(user_query)
    q = (user_query or "").lower()
    dl = list(_DELIVERABLE_DEFAULT[task])
    for x in _extra_deliverables(q):
        if x not in dl:
            dl.append(x)
    req = _REQUIRES[task]
    return {
        "task_type": task,
        "deliverable": dl,
        "requires_data": req["data"],
        "requires_sql": req["sql"],
        "requires_python": req["python"],
        "requires_report": req["report"],
        "workflow": _WORKFLOWS[task],
    }


def terminal_python(state: Any) -> Any:
    """python_code 终态：交付可运行的 Python 分析脚本（不强制取数/报告）。

    尽量引用会话内已有 CSV（sql/freeform 产物）；没有则给可替换占位。
    """
    csv_path = ""
    for r in reversed(getattr(state, "tool_results", None) or []):
        out = getattr(r, "output", None) or {}
        if out.get("csv_path"):
            csv_path = out["csv_path"]
            break
    obj = getattr(getattr(state, "context", None), "objective", "") or getattr(state, "user_query", "")
    data_line = (f'DATA_CSV = {csv_path!r}' if csv_path
                 else "# 请把 DATA_CSV 指向你的数据文件（CSV/Excel）")
    code = (
        "# 由 Data Analyst Agent 生成（可直接运行）\n"
        "import pandas as pd\n"
        f"{data_line}\n"
        "df = pd.read_csv(DATA_CSV) if 'DATA_CSV' in dir() else None\n"
        "# 1) 概览\n"
        "if df is not None:\n"
        "    print(df.shape, list(df.columns))\n"
        "    print(df.dtypes)\n"
        "    print(df.describe(include='all'))\n"
        "# 2) 数据质量\n"
        "if df is not None:\n"
        "    print('缺失值: \n', df.isna().sum()[df.isna().sum() > 0])\n"
        "    print('重复行:', int(df.duplicated().sum()))\n"
    )
    state.report = (
        f"**目标**：{obj}\n\n```python\n{code}\n```\n\n"
        "> 依赖：pandas（沙箱内可运行验证）。请按需补充业务分析步骤。"
    )
    state.status = "FINISH"
    return state


# --------------------------------------------------------------------------- #
# 精确 workflow 进度：语义步 → 里程碑 token（供 SSE/UI 精确打勾）
# --------------------------------------------------------------------------- #
# 里程碑 token：context/plan/sql/python/analyze/reflect/report/finish
_ITEM_TOKEN = {
    # 理解/定义类
    "understand_metric": "context", "understand_data": "context", "understand_sql": "context",
    "define_metric": "context", "define_metrics": "context", "overview": "context",
    "define_metric_formula": "context", "entities": "context",
    # 规划/设计类
    "design_logic": "plan", "plan": "plan", "generate_plan": "plan",
    # 数据/执行类
    "query_data": "sql", "generate_sql": "sql", "validate_sql": "sql", "sql_impl": "sql",
    "query_min": "sql", "grain": "sql", "filters": "sql", "aggregate": "sql",
    "compare_period": "sql", "drill_dimension": "sql", "quality": "sql",
    "distributions": "sql", "anomalies": "sql", "overview": "context",
    # python
    "generate_code": "python", "sandbox_validate": "python",
    # 分析
    "analyze": "analyze", "stats": "analyze", "insight": "analyze", "explain": "analyze",
    "locate_anomaly": "analyze", "root_cause": "analyze", "interpret": "analyze",
    "generate_insight": "analyze", "conclusion": "analyze", "answer": "analyze",
    # 质检
    "reflect": "reflect",
    # 报告/交付
    "write_markdown": "report", "recommendation": "report", "write_report": "report",
    "output_sql": "report", "output": "report", "ddl": "report",
}
# 出现在多个 map 名，去除重复的 overview 冲突（sql 上面的 overview 属多余，以 context 为准）
_ITEM_TOKEN.pop("overview", None)
_ITEM_TOKEN["overview"] = "context"


def item_token(item: str):
    """语义 workflow 步 → milestone token；未知步返回 None（仅在 finish 时算完成）。"""
    return _ITEM_TOKEN.get(item.strip())


def status_milestones(status: str, step: Any = None) -> set[str]:
    """某节点事件完成后产生哪些里程碑 token。"""
    out: set[str] = set()
    if status in ("UNDERSTAND",):
        out.add("context")
    elif status == "PLAN":
        out.add("plan")
    elif status == "EXECUTE":
        if step is not None and getattr(step, "status", "") == "SUCCESS":
            tool = getattr(step, "tool", "")
            if tool in ("sql_query", "freeform"):
                out.add("sql")
            elif tool == "python_analysis":
                out.add("python")
    elif status == "ANALYZE":
        out.add("analyze")
    elif status in ("REFLECT", "REPLAN"):
        out.add("reflect")
    elif status in ("REPORT", "FINISH"):
        out.add("report")
    if status == "FINISH":
        out.add("finish")
    return out


def workflow_progress(workflow: list[str], seen: set[str]) -> dict:
    """返回 {done, total}：语义步按 milestone 精确完成；未知步仅 finish 时完成。"""
    total = len(workflow)
    if total == 0:
        return {"done": 0, "total": 0}
    finished = "finish" in seen
    done = sum(1 for item in workflow
               if finished or (item_token(item) is not None and item_token(item) in seen))
    return {"done": done if finished else min(done, total - 1 if total > 1 else total), "total": total}
