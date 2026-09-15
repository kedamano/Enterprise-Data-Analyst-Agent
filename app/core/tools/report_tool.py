"""``generate_report`` tool – assemble a structured markdown business report.

Used by the Reporter node as a deterministic template fallback (and as a tool
the Planner may schedule). It follows the spec's required report structure:
Executive Summary, Key Metrics, Key Findings, Root Cause, Recommendations,
Limitations.
"""
from __future__ import annotations

from typing import Any

from ..agents.data_analyst.state import AnalysisResult, ReflectionResult


def run(params: dict[str, Any]) -> dict[str, Any]:
    analysis = AnalysisResult.model_validate(params.get("analysis", {}))
    reflection = params.get("reflection")
    if isinstance(reflection, dict):
        reflection = ReflectionResult.model_validate(reflection)
    objective = params.get("objective", "")

    lines: list[str] = []
    lines.append(f"# 数据分析报告：{objective or '业务分析'}\n")

    lines.append("## Executive Summary\n")
    lines.append(analysis.limitations[0] if analysis.limitations else "（基于工具获取的真实数据形成结论）")
    lines.append("")

    lines.append("## Key Metrics\n")
    # D51：指标归一化只有一份实现（`metric_cards`）——报告表格与 UI 卡片同源。
    # 此前这里自己判一遍 name/text/metric 优先级，前端若再判一遍就是两处口径。
    from ..agents.data_analyst.metric_cards import normalize_metrics

    metrics = normalize_metrics(analysis.metrics)
    if metrics:
        lines.append("| 指标 | 值 | 对比 |")
        lines.append("| --- | --- | --- |")
        for m in metrics:
            lines.append(f"| {m['name']} | {m['value']} | {m['comparison']} |")
    else:
        lines.append("_未显式计算指标，详见发现。_")
    lines.append("")

    lines.append("## Key Findings\n")
    for i, f in enumerate(analysis.findings, 1):
        lines.append(f"### 发现 {i}（置信度 {f.confidence:.2f}）")
        lines.append(f"- **结论**：{f.finding}")
        for ev in f.evidence:
            lines.append(f"  - 证据（{ev.source or '未知来源'}）：{ev.metric or ''} = {ev.value if ev.value is not None else ''}")
        lines.append(f"- **解读**：{f.interpretation}")
    lines.append("")

    lines.append("## Hypotheses\n")
    for h in analysis.hypotheses:
        lines.append(f"- {h.hypothesis} → **{h.result}**（置信度 {h.confidence:.2f}）")
    lines.append("")

    lines.append("## Recommendations\n")
    for r in analysis.recommendations:
        lines.append(f"- **问题**：{r.problem or ''} → **行动**：{r.action or ''} "
                     f"（预期影响：{r.expected_impact or ''}，优先级 {r.priority}）")
    lines.append("")

    lines.append("## Limitations\n")
    for lim in analysis.limitations:
        lines.append(f"- {lim}")
    if reflection and reflection.summary:
        lines.append(f"- 质检结论：{reflection.summary}（置信度 {reflection.confidence:.2f}）")
    lines.append("")

    # E4/03：口径说明（结构性口径问题必然影响可比性，必须让读者看到）
    cal = getattr(reflection, "caliber_comparability", None) if reflection else None
    cal_issues = list(getattr(cal, "issues", None) or [])
    if cal_issues:
        lines.append("## 口径说明")
        lines.append("")
        for it in cal_issues:
            detail = getattr(it, "detail", None) or str(it)
            kind = getattr(it, "kind", "")
            lines.append(f"- **{kind}**：{detail}")
        lines.append("")

    # E4/04：质量门禁的确定性披露（有才出现，绝不给"看起来没问题"的错觉）
    if analysis.quality_notes:
        lines.append("## 数据质量与限制\n")
        lines.append("_以下由确定性质量检查给出（非模型判断），影响结论的可信范围：_")
        for note in analysis.quality_notes:
            lines.append(f"- {note}")
        lines.append("")

    return {"ok": True, "report": "\n".join(lines)}
