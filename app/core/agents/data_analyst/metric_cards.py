"""D51：指标卡 —— ``metrics`` 的**唯一归一化口径**。

Spec: docs/specs/C5/01-report-charts-ui.md §2

断在哪：`AnalysisResult.metrics` 的真实形态有两种——结构化
``{name, value, comparison}`` 与 coerce 后的纯文本 ``{text}``；
而 SSE 的 FINISH 帧**此前根本不下发 metrics**，前端再怎么写也无从消费。

设计选择：**归一在后端**。前端拿到的一定是 ``{name, value, comparison}``，
于是"两种形态怎么处理"只有一份实现，而不是前端再猜一遍——
猜错的那一半永远没人测到。`report_tool` 也复用这里（同一优先级表，两处不分叉）。
"""
from __future__ import annotations

from typing import Any

#: 指标名的候选键，**按优先级**。改这里即改全部（报告表格与 UI 卡片同源）。
NAME_KEYS = ("name", "text", "metric")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def normalize_metric(raw: Any) -> dict[str, str] | None:
    """单条指标 → ``{name, value, comparison}``；无名字（含非字典）→ ``None``（丢弃）。"""
    if not isinstance(raw, dict):
        return None
    name = ""
    for key in NAME_KEYS:
        candidate = raw.get(key)
        if candidate is None:
            continue
        text = _text(candidate).strip()
        if text:
            name = text
            break
    if not name:
        return None
    return {
        "name": name,
        "value": _text(raw.get("value")),
        "comparison": _text(raw.get("comparison")),
    }


def normalize_metrics(metrics: Any) -> list[dict[str, str]]:
    """列表 → 归一化列表。非列表 / ``None`` → ``[]``（**空不是错，但绝不放行垃圾**）。"""
    if not isinstance(metrics, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for item in metrics:
        normalized = normalize_metric(item)
        if normalized is not None:
            out.append(normalized)
    return out
