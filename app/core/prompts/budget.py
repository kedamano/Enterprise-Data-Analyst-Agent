"""D42：**prompt 预算强制执行**。

Gap 原文（`docs/对标企业级Gap.md` 三）：
> `budget.truncate` 只用于 `long_hits` 文本截断（500 字符）；
> **system/工具/历史整体预算仍未分配**。

核实：`fit_to_budget` / `estimate_tokens` 在 `app/` 下**零调用**（只在测试里用过）
——"实现了但从未接进真实链路"。

风险很具体：`build_user_message` 直接 `json.dumps(task_context)`，而 analyst 的
payload 带 `tool_results[].output.rows`。8 个工具结果轻易把单次 prompt 推到十几万字符。

三条设计约束
------------
1. **不许静默**：压缩必须留说明，并且**写进给模型看的那份文本**——只在日志里等于没留；
2. **围栏必须仍然合法**：直接砍序列化后的字符串会把 `<task_context>` 砍坏，
   所以压缩发生在**序列化之前**（按 key 结构化地砍）；
3. **优先级**：`context` / `plan` 是回答所必需 → 最后才动；
   `tool_results` 的行数据最占地方 → 先砍。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Optional

from ..memory.budget import estimate_tokens

# 压缩顺序：先动"行数据"，再动整个工具结果，最后才是高优先级字段
_ROW_HOLDERS = ("rows", "row_sample", "sample_rows")
# 绝不压缩的顶层键（回答所必需）
_NEVER_DROP = ("context", "plan")


def _size(obj: Any) -> int:
    return estimate_tokens(json.dumps(obj, ensure_ascii=False, default=str))


def _shrink_rows(ctx: dict, notes: list[str]) -> bool:
    """把**最旧**的工具结果先减半，逐步逼近（避免一次砍太狠）。

    ⚠️ **只有真的变短才返回 True**。此前写成 ``keep = max(1, len//2)``：
    当 ``len(rows) == 1`` 时 ``keep == 1``，长度不变却仍返回 True →
    调用方的 ``while`` **死循环**（测试当场挂满 180s 超时才暴露）。
    "返回真表示有进展"这种约定，必须在**每个**分支上成立。
    """
    results = ctx.get("tool_results")
    if not isinstance(results, list):
        return False
    for tool in results:                      # 从旧到新
        out = tool.get("output") if isinstance(tool, dict) else None
        if not isinstance(out, dict):
            continue
        for key in _ROW_HOLDERS:
            rows = out.get(key)
            if isinstance(rows, list) and len(rows) > 1:
                keep = max(1, len(rows) // 2)
                out[key] = rows[:keep]
                notes.append(f"压缩 {tool.get('step_id', '?')}.output.{key}："
                             f"{len(rows)} → {keep} 行")
                return True
    return False


def _drop_oldest_tool_result(ctx: dict, notes: list[str]) -> bool:
    results = ctx.get("tool_results")
    if isinstance(results, list) and len(results) > 1:
        dropped = results.pop(0)              # 留最新的一条（最相关）
        notes.append(f"丢弃工具结果 {dropped.get('step_id', '?')}（预算不足，保留更新的）")
        return True
    return False


def enforce_context_budget(
        context: dict, budget_tokens: int,
        estimate: Optional[Callable[[Any], int]] = None) -> tuple[dict, list[str]]:
    """把 ``task_context`` 压进 token 预算 → ``(压缩后, 说明清单)``。

    ``budget_tokens <= 0`` → **不压缩**（向后兼容：既有调用点不传就不变行为）。

    策略**分阶段、可解释**（每次只动一处，便于定位"为什么模型没看到数据"）：
    ① 行数据减半（最旧的先）→ ② 丢弃最旧的整条工具结果
    → ③ 最后兜底：只留 ``context``/``plan`` 并标注。
    每一步都写进 ``notes``（**不许静默**）。
    """
    if not budget_tokens or budget_tokens <= 0:
        return context, []
    estimate = estimate or _size
    out = copy.deepcopy(context)
    notes: list[str] = []
    if estimate(out) <= budget_tokens:
        return out, notes

    # ①②：先砍工具结果，直到进预算或无可再砍
    while estimate(out) > budget_tokens:
        if _shrink_rows(out, notes) or _drop_oldest_tool_result(out, notes):
            continue
        break

    # ③：兜底——只留必需字段（仍不砍 context / plan）
    if estimate(out) > budget_tokens:
        kept = {k: out[k] for k in _NEVER_DROP if k in out}
        dropped_keys = sorted(set(out) - set(kept))
        if dropped_keys and estimate(kept) < estimate(out):
            notes.append("预算仍不足，已丢弃字段：" + "、".join(dropped_keys))
            out = kept

    if estimate(out) > budget_tokens:
        # **如实说明**：压不到预算内就不假装压到了（与"降级要可见"同一条纪律）
        notes.append(f"预算未完全满足：当前约 {estimate(out)} tokens > {budget_tokens}")
    return out, notes
