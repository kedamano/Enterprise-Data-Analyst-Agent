"""D42：prompt 预算**强制**执行（此前只是"有个 helper 没人用"）。

现状（Gap 原文）
---------------
> `budget.truncate` 只用于 `long_hits` 文本截断（500 字符）；
> **system/工具/历史整体预算仍未分配**。

实测核实：`fit_to_budget` 与 `estimate_tokens` **只在测试里被调用过**
（`grep -rn fit_to_budget app/` 零命中）——即"实现了但从未接进真实链路"。

风险点很具体：`build_user_message` 直接 `json.dumps(task_context)`，而 analyst 的
payload 里带 `tool_results[].output.rows` —— **无上限**。8 个工具结果 × 每次上限，
轻易把单次 prompt 推到十几万字符。

设计约束（三条，缺一不可）
------------------------
1. **不许静默**：压缩必须留下说明（铁律 3）。静默截断会让模型"看不到数据"却无人知道；
2. **JSON 围栏必须仍然合法**：直接砍字符串会把 `</task_context>` 前的内容砍坏；
3. **优先级**：`context`/`plan` 是回答所必需，先保；`tool_results` 的行数据最占地方，先砍。
"""
from __future__ import annotations

import json
import re

from app.core.memory.budget import estimate_tokens
from app.core.prompts import build_user_message
from app.core.prompts.budget import enforce_context_budget

_TASK_RE = re.compile(r"<task_context>\n(.*)\n</task_context>", re.S)


def _ctx(*, n_tools: int = 3, rows_per_tool: int = 400) -> dict:
    """构造一个"大 payload"：每个工具结果都带一堆行。"""
    return {
        "context": {"objective": "分析各区域营收", "metrics": ["营收"]},
        "plan": {"goal": "g", "steps": [{"id": "s1", "tool": "sql_query"}]},
        "tool_results": [
            {"step_id": f"s{i}", "tool": "sql_query", "status": "SUCCESS",
             "output": {"ok": True, "row_count": rows_per_tool,
                        "rows": [{"region_id": j, "revenue": j * 7} for j in range(rows_per_tool)]}}
            for i in range(n_tools)
        ],
    }


def _tokens(obj) -> int:
    return estimate_tokens(json.dumps(obj, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# 一、预算内：**原样不动**
# --------------------------------------------------------------------------- #
def test_small_payload_is_untouched():
    ctx = {"context": {"objective": "x"}, "plan": {"steps": []}}
    out, notes = enforce_context_budget(ctx, budget_tokens=10_000)
    assert out == ctx, "没超预算就不该改一个字"
    assert notes == []


# --------------------------------------------------------------------------- #
# 二、超预算：按优先级压缩，且**留痕**
# --------------------------------------------------------------------------- #
def test_large_payload_is_compressed_under_budget():
    ctx = _ctx()
    assert _tokens(ctx) > 8_000, "前置：构造的 payload 必须真的很大"
    out, notes = enforce_context_budget(ctx, budget_tokens=8_000)
    assert _tokens(out) <= 8_000, f"压缩后仍超预算：{_tokens(out)}"
    assert notes, "压缩必须留下说明（铁律 3：不许静默）"


def test_high_priority_fields_survive():
    """回答所必需的 `context` / `plan` 不能被砍掉。"""
    out, _ = enforce_context_budget(_ctx(), budget_tokens=4_000)
    assert out["context"]["objective"] == "分析各区域营收"
    assert out["plan"]["steps"]


def test_tool_rows_are_shrunk_first():
    """最占地方的行数据先被砍——这是优先级设计的直接体现。"""
    ctx = _ctx()
    out, notes = enforce_context_budget(ctx, budget_tokens=6_000)
    before = sum(len(r["output"]["rows"]) for r in ctx["tool_results"])
    after = sum(len(r["output"].get("rows") or []) for r in out["tool_results"])
    assert after < before, "行数据没有被压缩"
    assert any("行" in n or "tool" in n.lower() for n in notes), notes


def test_zero_budget_means_no_enforcement():
    """0 = 关闭（向后兼容）——既有调用点不传就不会变。"""
    ctx = _ctx()
    out, notes = enforce_context_budget(ctx, budget_tokens=0)
    assert out == ctx and notes == []


# --------------------------------------------------------------------------- #
# 三、端到端：user message 的 JSON 围栏必须仍然合法
# --------------------------------------------------------------------------- #
def test_user_message_stays_parseable_after_compression():
    raw = build_user_message("分析各区域营收", _ctx(), budget_tokens=6_000)
    m = _TASK_RE.search(raw)
    assert m, "task_context 围栏被破坏了"
    parsed = json.loads(m.group(1))          # 必须仍是合法 JSON
    assert parsed["context"]["objective"] == "分析各区域营收"
    assert "…[截断]" in raw or "budget" in raw.lower() or "压缩" in raw, \
        "压缩要写在给模型看的地方，不能只在日志里"


def test_no_budget_keeps_old_behaviour():
    raw = build_user_message("q", {"context": {"objective": "x"}})
    assert "<user_request>\nq\n</user_request>" in raw
    assert json.loads(_TASK_RE.search(raw).group(1))["context"]["objective"] == "x"


def test_user_request_block_is_never_truncated():
    """用户问题本身永远不被截断（截了就不是同一个问题了）。"""
    q = "分析" * 500
    raw = build_user_message(q, _ctx(), budget_tokens=3_000)
    assert f"<user_request>\n{q}\n</user_request>" in raw
