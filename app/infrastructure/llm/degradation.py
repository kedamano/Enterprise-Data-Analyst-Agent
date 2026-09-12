"""降级诊断（P0-4）：把「哪一级降了 / 为什么降 / 影响什么」讲清楚。

背景
----
DEGRADE/01 已经把降级事件``llm_fallbacks``透出到 ``/health`` 与 SSE，
但那是**原始事件流**：调用方拿到 5 条 ``{"stage": "planner", "error": "Error code:
403 - ..."}``，还得自己解析。案例一里用户看到的是报告末尾一句
「当前处于降级模式（未启用真实模型）」，看不到究竟哪一级降了、为什么。

本模块把这层"翻译"补齐，提供三个层次：

1. :func:`classify_error` —— 原始错误串 → 结构化原因（code/kind/summary/action）
2. :func:`summarize_degradation` —— 事件列表 → 阶段聚合 + 影响范围 + 人话摘要
3. :func:`render_degradation_block` —— 摘要 → Markdown 区块（报告顶部用）

设计原则
--------
* **纯函数**：不读全局、不发网络、可单测。
* **不猜**：分类只依赖错误串里可验证的特征；拿不准就归 ``unknown`` 并保留原文。
* **说人话**：``summary``/``action`` 面向使用者，不堆栈、不堆 JSON。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------- #
# 阶段 → 人话名称 & 降级后的影响
#
# 这张表是"降级可见化"的知识核心：知道 planner 挂了意味着什么，
# 才能告诉用户"本次结论的可信度受什么限制"。
# --------------------------------------------------------------------------- #
STAGE_META: dict[str, dict[str, str]] = {
    "context": {
        "label": "上下文理解",
        "impact": "未能解析业务意图，按通用分析流程处理",
    },
    "planner": {
        "label": "计划制定",
        "impact": "改用确定性计划（可能不如模型自主规划贴合问题）",
    },
    "executor": {
        "label": "工具执行",
        "impact": "部分步骤未按模型预期执行",
    },
    "analyst": {
        "label": "证据分析",
        "impact": "结论仅基于工具原始数据，未做统计推断与因果分析",
    },
    "reflection": {
        "label": "质检反思",
        "impact": "未做质量校验，可能遗漏证据不足之处",
    },
    "reporter": {
        "label": "报告撰写",
        "impact": "报告为模板化输出，缺少自然语言洞察",
    },
    "sql": {
        "label": "SQL 生成",
        "impact": "改用规则化 SQL，复杂查询可能不完整",
    },
    "python": {
        "label": "Python 分析",
        "impact": "未生成分析代码，改由 SQL 承担统计",
    },
    "intent": {
        "label": "意图识别",
        "impact": "任务类型判断可能不准",
    },
    "router": {
        "label": "模型路由",
        "impact": "未走预期模型",
    },
}

_DEFAULT_STAGE_META = {
    "label": "未知阶段",
    "impact": "该环节未使用真实模型",
}

# --------------------------------------------------------------------------- #
# 错误分类：从错误串里提取可验证的特征
#
# 顺序敏感 —— 先匹配越具体的，再落到通用 HTTP 码。
# 每条规则 = (kind, 正则, 摘要, 建议动作)
# --------------------------------------------------------------------------- #
_CLASSIFY_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    (
        "region_blocked",
        re.compile(r"not available in your region", re.I),
        "模型在当前区域不可用（服务方按地域拒服）",
        "更换可用区域的模型/供应商，或改用该区域可访问的模型",
    ),
    (
        "auth_failed",
        re.compile(r"\b(401|invalid[_ ]api[_ ]key|incorrect api key|unauthorized)\b", re.I),
        "鉴权失败：API Key 无效或权限不足",
        "核对 API Key 是否正确、是否已过期、是否开通了该模型权限",
    ),
    (
        "quota_exhausted",
        re.compile(r"\b(402|insufficient[_ ](quota|balance|credit)|exceeded your current quota)\b", re.I),
        "额度不足或账单异常",
        "充值额度或更换 Key",
    ),
    (
        "rate_limited",
        re.compile(r"\b(429|rate[_ ]limit|too many requests)\b", re.I),
        "触发限流",
        "降低并发或稍后重试（熔断器会自动恢复）",
    ),
    (
        "server_error",
        re.compile(r"\b(500|502|503|504|internal server error|bad gateway|service unavailable)\b", re.I),
        "模型服务端异常",
        "稍后重试；若持续出现需排查供应商状态",
    ),
    (
        "model_not_found",
        re.compile(r"\b(404|model[_ ]not[_ ]found|does not exist|no such model)\b", re.I),
        "模型不存在或名称写错",
        "核对 LLM_MODEL 配置项",
    ),
    (
        "timeout",
        re.compile(r"\b(timeout|timed out|read timeout|connect timeout)\b", re.I),
        "调用超时",
        "检查网络/代理；必要时调大超时时间",
    ),
    (
        "connection",
        re.compile(r"\b(connection|connect failed|connection refused|dns|ssl|proxy)\b", re.I),
        "网络连接失败",
        "检查网络、代理与 base_url 配置",
    ),
    (
        "context_length",
        re.compile(r"\b(context[_ ]length|too many tokens|maximum context)\b", re.I),
        "输入超出模型上下文上限",
        "缩小上下文（如小文件分段、减少样本行数）",
    ),
]


def classify_error(error: Any) -> dict[str, str]:
    """把原始错误串归类为结构化原因。

    返回 ``{"kind", "code", "summary", "action", "raw"}``。
    拿不准时为 ``kind="unknown"``，并保留 ``raw`` 原文 —— 不吞信息。
    """
    raw = "" if error is None else str(error)
    kind, summary, action = "unknown", "未知错误", "查看服务端日志获取完整堆栈"

    for rule_kind, pattern, rule_summary, rule_action in _CLASSIFY_RULES:
        if pattern.search(raw):
            kind, summary, action = rule_kind, rule_summary, rule_action
            break

    code = None
    m = re.search(r"\b([45]\d{2})\b", raw)
    if m:
        code = m.group(1)

    return {
        "kind": kind,
        "code": code or "",
        "summary": summary,
        "action": action,
        "raw": raw,
    }


def _stage_meta(stage: str) -> dict[str, str]:
    key = (stage or "").strip().lower()
    if key in STAGE_META:
        return STAGE_META[key]
    # 兼容 "planner_v2" / "stage:planner" 这类变体
    for known, meta in STAGE_META.items():
        if known and known in key:
            return meta
    return _DEFAULT_STAGE_META


def summarize_degradation(
    events: Optional[Iterable[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """把原始降级事件聚合成"可读、可判断"的摘要。

    返回结构::

        {
          "degraded": bool,
          "count": int,                     # 事件数
          "stages": [                       # 按阶段聚合（去重）
             {"stage","label","impact","kind","summary","action","code"}
          ],
          "reasons": [{"kind","summary","action","count"}],
          "headline": str,                  # 一句话结论
          "impacts": [str],                 # 影响范围（去重）
          "severity": "none"|"partial"|"total",
        }
    """
    items = list(events or [])
    if not items:
        return {
            "degraded": False,
            "count": 0,
            "stages": [],
            "reasons": [],
            "headline": "",
            "impacts": [],
            "severity": "none",
        }

    stages: list[dict[str, Any]] = []
    seen_stages: set[str] = set()
    reasons: dict[str, dict[str, Any]] = {}

    for ev in items:
        stage = str(ev.get("stage") or "unknown")
        meta = _stage_meta(stage)
        cls = classify_error(ev.get("error"))

        if stage not in seen_stages:
            seen_stages.add(stage)
            stages.append({
                "stage": stage,
                "label": meta["label"],
                "impact": meta["impact"],
                "kind": cls["kind"],
                "summary": cls["summary"],
                "action": cls["action"],
                "code": cls["code"],
            })

        bucket = reasons.setdefault(
            cls["kind"],
            {"kind": cls["kind"], "summary": cls["summary"], "action": cls["action"], "count": 0},
        )
        bucket["count"] += 1

    # 严重度：全部阶段降级 = total；部分 = partial
    total_known = len(STAGE_META)
    severity = "total" if len(stages) >= min(5, total_known) else "partial"

    stage_names = " / ".join(s["label"] for s in stages)
    primary = max(reasons.values(), key=lambda r: r["count"])
    headline = (
        f"本次运行有 {len(stages)} 个阶段降级（{stage_names}）；"
        f"主要原因：{primary['summary']}。"
    )

    return {
        "degraded": True,
        "count": len(items),
        "stages": stages,
        "reasons": list(reasons.values()),
        "headline": headline,
        "impacts": [s["impact"] for s in stages],
        "severity": severity,
    }


def render_degradation_block(summary: dict[str, Any]) -> str:
    """把摘要渲染成 Markdown 区块，供报告顶部插入。无降级时返回空串。"""
    if not summary.get("degraded"):
        return ""

    lines: list[str] = []
    lines.append("> ⚠️ **运行健康度**")
    lines.append(">")
    lines.append(f"> {summary['headline']}")
    lines.append(">")

    for s in summary.get("stages", []):
        code = f"（HTTP {s['code']}）" if s.get("code") else ""
        lines.append(f"> - **{s['label']}**{code}：{s['summary']} → {s['action']}")

    if summary.get("impacts"):
        lines.append(">")
        lines.append("> **对本次结论的影响**：")
        for impact in summary["impacts"]:
            lines.append(f"> - {impact}")

    return "\n".join(lines)


def degradation_footer(summary: dict[str, Any]) -> str:
    """一句话页脚（比整块轻量，适合提示条）。"""
    if not summary.get("degraded"):
        return ""
    stages = "、".join(s["label"] for s in summary.get("stages", []))
    reasons = "；".join(
        f"{r['summary']}" for r in summary.get("reasons", [])[:2]
    )
    return f"降级阶段：{stages}（{reasons}）"
