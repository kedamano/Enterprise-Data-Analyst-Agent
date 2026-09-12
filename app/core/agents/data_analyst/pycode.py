"""E2 free-form Python delivery — model-authored script, sandbox-validated.

Flow (python_code mode): obtain a real CSV (demo query) → ask the LLM to write
an analysis script against it → run it through the AST-guarded python sandbox →
deliver the code plus a note of the sandbox run. If no data source is available,
still hand back the code without a fake "validated" claim.
"""
from __future__ import annotations

from typing import Any

_SYSTEM = (
    "You are a Python data-analysis writer. Output ONLY Python code, no "
    "markdown fences, no explanation. The script may use pandas. A DataFrame "
    "`df` is already loaded from DATA_CSV when the file exists; guard for None. "
    "Keep it runnable, print key outputs with print()."
)


def _last_csv(state: Any) -> str:
    for r in reversed(getattr(state, "tool_results", None) or []):
        out = getattr(r, "output", None) or {}
        if out.get("csv_path"):
            return out["csv_path"]
    return ""


def deliver_python_code(state: Any, max_attempts: int = 3) -> Any:
    from ....core.tools import execute_tool
    from ....infrastructure.llm.router import get_llm

    sid = getattr(state, "session_id", "python_code")
    obj = getattr(getattr(state, "context", None), "objective", "") or getattr(state, "user_query", "")

    # 1) 造一份真实数据源（demo SQL → CSV），让脚本有东西可跑
    csv_path = _last_csv(state)
    if not csv_path:
        demo = execute_tool(
            "pycode_src", "sql_query",
            {"sql": "SELECT region_id, SUM(revenue) AS rev, SUM(orders) AS orders "
                    "FROM fact_sales GROUP BY region_id ORDER BY rev DESC LIMIT 50"},
            sid)
        if demo.status == "SUCCESS" and demo.output.get("csv_path"):
            state.tool_results.append(demo)
            csv_path = demo.output["csv_path"]

    # 2) 失败自纠错：生成→沙箱验证→失败把结构化错误回注→重写（有界）
    feedback: list[str] = []
    code = ""
    note = ""
    for attempt in range(1, max_attempts + 1):
        user = (f"目标：{obj}\n数据文件：{csv_path or '（无，请写通用可运行脚本）'}")
        if feedback:
            user += "\n\n上次执行失败，请修复。错误：\n" + feedback[-1]
        try:
            code = (get_llm().complete(_SYSTEM, user, stage="python_code_gen",
                                       json_mode=False) or "").strip()
        except Exception as exc:
            feedback.append(f"代码生成异常：{exc}")
            continue
        if not csv_path:
            note = "> ⚠ 无可用数据文件，未做沙箱验证（代码未运行）。"
            break
        res = execute_tool("pycode_val", "python_analysis",
                           {"code": code, "data_csv": csv_path}, sid)
        state.tool_results.append(res)
        if res.status == "SUCCESS":
            out = res.output or {}
            note = f"> ✅ 沙箱验证通过（第 {attempt} 次尝试）；stdout 摘要：{(out.get('stdout') or '')[:160]}"
            break
        feedback.append(f"[第 {attempt} 次] {res.error}")
    else:
        note = f"> ⚠ 连续 {max_attempts} 次失败仍未通过。最后一次错误：{feedback[-1]}"

    state.report = f"**目标**：{obj}\n\n```python\n{code}\n```\n\n{note}".rstrip()
    state.status = "FINISH"
    return state
