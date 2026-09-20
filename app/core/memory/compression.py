"""Multi-turn context window governance (长对话上下文窗口治理).

状态环路：
1. 每次 run_cycle 完成后，计算当前 messages 的总 tokens（轻量估算）。
2. 若 tokens > compress_threshold（默认 max_context_tokens * 0.8）：
   a. 最老的 N 轮 tool_result → LLM 摘要压缩（fallback：截断 + 行数统计）。
   b. 同期 historical report 只保留 ≤3 句 executive_summary。
3. 若压缩后仍 > hard_limit → ContextOverflow → status="CONTEXT_OVERFLOW"。

设计约束：
- 不依赖 tiktoken（纯 Python 估算）。
- 压缩失败决不能 500：fallback 到截断。
- MVP 不依赖 LLM（仅截取 + regex 提取关键数字）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ...config import get_settings

logger = logging.getLogger("da.memory.compression")

# 压缩标记
_COMPRESS_MARKER = "[已压缩]"
_OVERFLOW_MARKER = "[上下文溢出]"


class ContextOverflow(Exception):
    """压缩后仍超 hard_limit → 触发此异常。agent 应设 status=CONTEXT_OVERFLOW。

    前端据此提示"请精简问题 / 新开对话"。
    """


class ContextCompressor:
    """多轮上下文窗口治理器。

    对旧的 tool_result 和 historical report 执行摘要压缩，防止长对话场景下
    LLM input tokens 指数增长 + 推理退化。
    """

    @staticmethod
    def compress_tool_result(raw_result: str, max_budget_chars: int = 500) -> str:
        """Fallback 压缩（无 LLM 可用 / 预算耗尽）：截断 + 保留首尾摘要 + 行数统计。

        策略：
        - 行数统计：提取前 N 行和最后 1 行（保留首尾语义）。
        - 关键数字提取：regex 抓取所有数值型指标。
        - 总长度 ≤ max_budget_chars。
        """
        if not raw_result or len(raw_result) <= max_budget_chars:
            return raw_result

        lines = raw_result.splitlines()
        total_lines = len(lines)

        # 提取关键数值（保留最多 10 个，避免噪音）
        numbers = re.findall(r"\b\d[\d,]*\.?\d*\b", raw_result)
        key_numbers = numbers[:10]
        num_summary = f"关键数值: {', '.join(key_numbers)}" if key_numbers else ""

        # 首尾保留：前 2 行 + 最后 1 行
        head = "\n".join(lines[:2]) if total_lines > 3 else "\n".join(lines[:1])
        tail = lines[-1] if total_lines > 3 else ""

        # 组装
        parts = [
            f"{_COMPRESS_MARKER} (原{total_lines}行/{len(raw_result)}字)",
            head,
            f"... (省略 {max(0, total_lines - 3)} 行) ...",
        ]
        if tail and tail != head:
            parts.append(tail)
        if num_summary:
            parts.append(num_summary)

        compressed = "\n".join(parts)

        # 硬截断：max_budget_chars 兜底
        if len(compressed) > max_budget_chars:
            compressed = compressed[: max(0, max_budget_chars - 3)] + "..."

        return compressed

    @staticmethod
    def compress_via_llm(raw_result: str, query: str) -> str:
        """LLM 压缩（仅在 lightweight LLM 可用时调用；失败 → fallback 截断）。

        TODO: 实际调用 LLM 生成 "Tool returned X rows, key findings: ..." 摘要。
        MVP 阶段始终 fallback（零额外 API 调用）。
        """
        # MVP: 直接 fallback，不消耗 LLM token
        # 进阶实现（TODO）：
        #   llm = get_llm(tier="light")
        #   prompt = f"将以下 tool result 压缩到 2-3 句摘要（关键指标数值+行数）:\nQuery: {query}\n\nResult:\n{raw_result[:2000]}"
        #   return llm.complete(system="压缩助手", user=prompt)
        return ContextCompressor.compress_tool_result(raw_result)

    @staticmethod
    def count_messages_tokens(messages: list[dict]) -> int:
        """轻量 token 估算。

        每 4 chars ≈ 1 token（中英混合保守估计）+ 固定 overhead（每条消息 ~4 token）。
        不能精确到 tiktoken 级别，但量级正确、零依赖、零延迟。
        """
        if not messages:
            return 0
        total_chars = 0
        for msg in messages:
            if isinstance(msg, dict):
                # 取 content 字段（LangChain/fopen 消息格式）或整个 dict 序列化
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)
                else:
                    total_chars += len(str(content)) if content else 0
                # 固定 overhead：role + 结构字符
                total_chars += 20
            elif isinstance(msg, str):
                total_chars += len(msg)
            else:
                total_chars += len(str(msg))
        # 中英混合保守估计：4 chars ≈ 1 token
        return total_chars // 4 + len(messages) * 4

    @staticmethod
    def would_overflow(messages: list[dict], max_ctx: int) -> bool:
        """判断当前 messages 是否超过 max_ctx（无需配置开关，内部判断）。"""
        return ContextCompressor.count_messages_tokens(messages) > max_ctx


def summarize_report_for_history(report: str, max_chars: int = 300) -> str:
    """把完整 report 压缩成 ≤max_chars 的 summary：保留核心指标数值 + 1-2 句结论。

    MVP：直接截取 report 前 N 句 + 关键指标数值 regex（不依赖 LLM，省 token）。
    进阶（TODO）：调 LLM 生成 abstract。
    """
    if not report or len(report) <= max_chars:
        return report

    # 提取关键数值 + 百分比 + 金额
    key_numbers = re.findall(
        r"\b\d[\d,]*\.?\d*\s*(?:%|倍|万|亿|元|个)?", report
    )[:8]

    # 取前 2 句
    sentences = re.split(r"[。！？\n]", report)
    head = "。".join(s.strip() for s in sentences[:2] if s.strip()) + "。"

    parts = [head]
    if key_numbers:
        parts.append(f"核心指标: {', '.join(key_numbers)}")
    parts.append(f"[原报告 {len(report)} 字已归档]")

    summary = " | ".join(parts)

    if len(summary) > max_chars:
        summary = summary[: max(0, max_chars - 3)] + "..."

    return summary


def _compress_context(state: Any) -> tuple[Any, dict[str, Any]]:
    """主压缩入口：压缩 state 中的旧 tool_result 和 report。

    返回 (state, compress_stats)。

    compress_stats = {
        "before_tokens": int,
        "after_tokens": int,
        "tool_results_compressed": int,
        "reports_summarized": int,
        "triggered": bool,
    }
    """
    settings = get_settings()

    # 初始化 stats
    stats = {
        "before_tokens": 0,
        "after_tokens": 0,
        "tool_results_compressed": 0,
        "reports_summarized": 0,
        "triggered": False,
    }

    if not getattr(settings, "context_compression_enabled", True):
        # 关闭时不作任何修改，但返回合法 stats 结构
        msgs_proxy = _state_to_messages(state)
        stats["before_tokens"] = ContextCompressor.count_messages_tokens(msgs_proxy)
        stats["after_tokens"] = stats["before_tokens"]
        return state, stats

    # ---- 计算当前 tokens ----
    msgs_proxy = _state_to_messages(state)
    before_tokens = ContextCompressor.count_messages_tokens(msgs_proxy)
    stats["before_tokens"] = before_tokens

    max_ctx = getattr(settings, "context_max_tokens", 60_000)
    threshold = int(max_ctx * getattr(settings, "context_compress_threshold", 0.8))
    hard_max = getattr(settings, "context_hard_max_tokens", 80_000)
    tool_max_chars = getattr(settings, "context_compress_tool_result_chars", 500)
    report_max_chars = getattr(settings, "context_compress_report_chars", 300)

    # 未达阈值 → 不压缩
    if before_tokens <= threshold:
        stats["after_tokens"] = before_tokens
        return state, stats

    stats["triggered"] = True
    tool_results_compressed = 0
    reports_summarized = 0

    # ---- 压缩旧 tool_results ----
    # 策略：保留最近 2 个，压缩更早的
    tool_results = getattr(state, "tool_results", [])
    if tool_results and len(tool_results) > 2:
        # 从最早开始压缩，保留最近 2 个
        for i in range(len(tool_results) - 2):
            tr = tool_results[i]
            if isinstance(tr, dict):
                output = tr.get("output", {})
                raw = output.get("rows", output.get("content", output.get("result", "")))
                if isinstance(raw, (list, dict)):
                    import json as _json
                    raw = _json.dumps(raw, ensure_ascii=False, default=str)
                raw_str = str(raw) if raw else ""
                if len(raw_str) > tool_max_chars:
                    compressed = ContextCompressor.compress_tool_result(raw_str, tool_max_chars)
                    # 替换 output 字段
                    tr["output"] = {"rows": [compressed]} if "rows" in output else {"content": compressed}
                    tool_results_compressed += 1
            else:
                # Pydantic 模型路径
                output = getattr(tr, "output", {})
                raw = output.get("rows", output.get("content", output.get("result", ""))) if isinstance(output, dict) else ""
                if isinstance(raw, (list, dict)):
                    import json as _json
                    raw = _json.dumps(raw, ensure_ascii=False, default=str)
                raw_str = str(raw) if raw else ""
                if len(raw_str) > tool_max_chars:
                    compressed = ContextCompressor.compress_tool_result(raw_str, tool_max_chars)
                    try:
                        if isinstance(output, dict):
                            new_out = dict(output)
                            if "rows" in new_out:
                                new_out["rows"] = [compressed]
                            else:
                                new_out["content"] = compressed
                            tr.output = new_out
                            tool_results_compressed += 1
                    except Exception:
                        pass

    stats["tool_results_compressed"] = tool_results_compressed

    # ---- 压缩 report 历史 ----
    report = getattr(state, "report", "")
    report_history = state.metadata.get("report_history", []) if hasattr(state, "metadata") else []

    if report and len(report) > report_max_chars:
        summary = summarize_report_for_history(report, report_max_chars)
        if hasattr(state, "metadata") and isinstance(state.metadata, dict):
            if report_history:
                # 已有历史：只保留 summary
                state.metadata["report_history"] = [
                    summarize_report_for_history(r, report_max_chars) if isinstance(r, str) and len(r) > report_max_chars else r
                    for r in report_history
                ]
                reports_summarized = sum(1 for r in report_history if isinstance(r, str) and len(r) > report_max_chars)
            # 当前 report 不动（本次结果），但历史压缩
        stats["reports_summarized"] = reports_summarized

    # ---- 压缩后 token 检查 ----
    msgs_after = _state_to_messages(state)
    after_tokens = ContextCompressor.count_messages_tokens(msgs_after)
    stats["after_tokens"] = after_tokens

    # 超 hard_limit → raise ContextOverflow
    if after_tokens > hard_max:
        logger.warning(
            "上下文溢出：压缩后 tokens=%d > hard_limit=%d (原始 %d)",
            after_tokens, hard_max, before_tokens,
        )
        raise ContextOverflow(
            f"context overflow after compression: {after_tokens} > {hard_max}"
        )

    return state, stats


def _state_to_messages(state: Any) -> list[dict]:
    """从 AgentState 提取近似 messages 列表（用于 token 估算）。

    不修改 schema：从 tool_results / report / context / plan 派生。
    """
    messages: list[dict] = []

    # 1. user_query
    query = getattr(state, "user_query", "")
    if query:
        messages.append({"role": "user", "content": query})

    # 2. context (序列化)
    context = getattr(state, "context", None)
    if context and hasattr(context, "model_dump"):
        messages.append({"role": "system", "content": str(context.model_dump())})
    elif context:
        messages.append({"role": "system", "content": str(context)})

    # 3. tool_results → messages
    tool_results = getattr(state, "tool_results", [])
    for tr in tool_results:
        if isinstance(tr, dict):
            content = str(tr.get("output", tr))
        else:
            output = getattr(tr, "output", {})
            content = str(output)
        messages.append({"role": "tool", "content": content})

    # 4. report
    report = getattr(state, "report", "")
    if report:
        messages.append({"role": "assistant", "content": report})

    # 5. plan (序列化)
    plan = getattr(state, "plan", None)
    if plan and hasattr(plan, "model_dump"):
        messages.append({"role": "system", "content": str(plan.model_dump())[:2000]})

    # 6. conversation_history
    conv_hist = getattr(state, "conversation_history", [])
    for ch in conv_hist:
        if isinstance(ch, dict):
            messages.append(ch)
        else:
            messages.append({"role": "user", "content": str(ch)})

    return messages
