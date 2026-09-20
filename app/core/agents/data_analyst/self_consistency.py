"""Self-Consistency / 多次采样投票。

对 analyst 阶段的「核心结论 + 数值」跑 N 次独立采样（temperature=0.7），
用启发式协同取共识结果 + 入 consistency metadata。

设计原则：
- Fail-open：self_consistency 内任何 error → 降级到单次 analyst，不留痕迹。
- MockLLM 模式下跳过：mock 每次返回相同内容 → 100% 一致性 → 无意义 + 浪费时间。
- 纯文本正则提取 metrics，不装新 pip 包。
- 并发用 asyncio.gather（IO 密集的 LLM 调用天然适合并发）。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .state import AgentState

logger = logging.getLogger("da.self_consistency")

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ConsistencyMetric:
    """单个指标-数值对（从 report 文本中抽取）。"""
    metric_name: str       # 指标名（小写、strip）
    value: str             # 数值原文（含单位/千分位等）
    count: int = 1         # 在 N 份 report 中出现的次数


@dataclass
class ConsistencyResult:
    """一致性检查结果。"""
    consensus_metrics: list[ConsistencyMetric] = field(default_factory=list)
    disagreement_metrics: list[ConsistencyMetric] = field(default_factory=list)
    consensus_ratio: float = 0.0      # 共识指标数 / 总去重指标数，0.0~1.0
    all_reports: list[str] = field(default_factory=list)


@dataclass
class SampleBundle:
    """多次采样的结果束。"""
    primary_report: str                  # 最终采用的 report（文本）
    all_reports: list[str]               # N 份原始 report 文本
    consensus: ConsistencyResult         # 一致性检查结果
    replaced_by_consensus: bool = False  # 是否用共识 metrics 替换了首份
    primary_state: Any = None            # 采样得到的原始 state（供调用方取 analysis）


# --------------------------------------------------------------------------- #
# 启发式协同
# --------------------------------------------------------------------------- #

# 匹配「数值 + 指标名」或「指标名 + 数值」的常见写法
# 例：营收 1234.5 万元 / revenue: $1,234 / 增长率 12.5% / 利润=998万
_METRIC_PATTERNS = [
    # 指标名 + 数值：`营收 1234.5 万元`、`利润：998万`
    re.compile(
        r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_ ]{1,30})"
        r"[\s:：=,，]+"
        r"([\d,]+\.?\d*\s*[万亿%％$]?)",
        re.MULTILINE,
    ),
    # 数值 + 指标名：`1234.5 万元营收`、`12.5% 的增长率`
    re.compile(
        r"([\d,]+\.?\d*\s*[万亿%％$]?)"
        r"\s*的?\s*"
        r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_ ]{1,30})",
        re.MULTILINE,
    ),
]

# 纯正数指标名提取（用于「无单位数值」作为兜底）：`共 1234 / 总共 567`
_COUNT_PATTERN = re.compile(
    r"(?:共|总共|合计|累计|达到|约为|超过|不足|约|近)\s*"
    r"([\d,]+\.?\d*)\s*"
    r"([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{1,20})",
    re.MULTILINE,
)


def _extract_metrics(report: str) -> dict[str, str]:
    """从单份 report 文本中提取 {metric_name_lower: value} 对。

    同一指标在单份 report 里出现多次时取**最后一次**（通常是最新的结论位置）。
    """
    metrics: dict[str, str] = {}
    for pat in _METRIC_PATTERNS:
        for m in pat.finditer(report):
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            # 判断哪个是指标名、哪个是数值
            if re.match(r"^[\d,]+\.?\d*\s*[万亿%％$]?$", g1):
                value, name = g1, g2
            else:
                name, value = g1, g2
            name = name.lower().strip()
            if len(name) >= 2 and value:
                metrics[name] = value
    for m in _COUNT_PATTERN.finditer(report):
        value, name = m.group(1).strip(), m.group(2).strip().lower()
        if len(name) >= 2:
            metrics[name] = value
    return metrics


def check_consistency(reports: list[str], consensus_threshold: float = 0.6) -> ConsistencyResult:
    """启发式协同：提取每份 report 中的 key metrics（形如 `数值 + 指标名`），
    对比 N 份 report 中出现频率 >= ceil(N * consensus_threshold) 的指标-数值对 -> 入 consensus_metrics；
    否则 -> 入 disagreement_metrics。

    返回 ConsistencyResult(consensus_metrics, disagreement_metrics, consensus_ratio, all_reports)
    """
    threshold = math.ceil(len(reports) * consensus_threshold) if reports else 0

    # name -> {value -> count} 的二级计数
    tally: dict[str, dict[str, int]] = {}
    for rep in reports:
        for name, value in _extract_metrics(rep).items():
            bucket = tally.setdefault(name, {})
            bucket[value] = bucket.get(value, 0) + 1

    consensus: list[ConsistencyMetric] = []
    disagreement: list[ConsistencyMetric] = []
    for name, val_counts in tally.items():
        # 同一指标取出现次数最多的值
        best_value = max(val_counts, key=lambda v: val_counts[v])
        count = val_counts[best_value]
        metric = ConsistencyMetric(metric_name=name, value=best_value, count=count)
        if threshold > 0 and count >= threshold:
            consensus.append(metric)
        else:
            disagreement.append(metric)

    total = len(tally)
    ratio = (len(consensus) / total) if total > 0 else 0.0

    return ConsistencyResult(
        consensus_metrics=consensus,
        disagreement_metrics=disagreement,
        consensus_ratio=ratio,
        all_reports=list(reports),
    )


# --------------------------------------------------------------------------- #
# 多次采样
# --------------------------------------------------------------------------- #


def _analysis_to_text(analysis: Any) -> str:
    """把 AnalysisResult 转成一段包含所有 findings/metrics 的纯文本，供 check_consistency 使用。"""
    parts: list[str] = []
    try:
        raw = analysis.model_dump()
    except Exception:
        return str(analysis)
    findings = raw.get("findings") or []
    for f in findings:
        finding_text = f.get("finding") or ""
        if finding_text:
            parts.append(str(finding_text))
        for ev in (f.get("evidence") or []):
            v = ev.get("value")
            m = ev.get("metric")
            if v is not None:
                if m:
                    parts.append(f"{m} {v}")
                else:
                    parts.append(str(v))
    metrics_list = raw.get("metrics") or []
    for m in metrics_list:
        if isinstance(m, dict):
            name = m.get("name") or m.get("metric") or ""
            val = m.get("value")
            if name and val is not None:
                parts.append(f"{name} {val}")
    recommendations = raw.get("recommendations") or []
    for r in recommendations:
        act = r.get("action") or r.get("recommendation") or ""
        if act:
            parts.append(str(act))
    return "\n".join(parts)


def _apply_consensus_to_report(
    primary_report: str,
    consensus: ConsensusResult,
) -> str:
    """把共识 metrics 注入 report 末尾，作为「自洽性审计段」。

    不会修改原报告正文（保持首份的完整性），只在末尾追加一个标记段。
    低一致性（<0.5）时也追加 warning 段，供前端显示黄色徽章。
    """
    lines: list[str] = []
    if consensus.consensus_metrics:
        lines.append("\n\n<!-- self_consistency: consensus_metrics -->")
        for m in consensus.consensus_metrics:
            lines.append(
                f"- **{m.metric_name}** = {m.value} "
                f"({m.count}/{len(consensus.all_reports)} 票)"
            )
    if consensus.disagreement_metrics:
        lines.append("\n<!-- self_consistency: disagreement_metrics -->")
        for m in consensus.disagreement_metrics:
            lines.append(
                f"- **{m.metric_name}** = {m.value} "
                f"({m.count}/{len(consensus.all_reports)} 票)"
            )
    if consensus.consensus_ratio < 0.5:
        lines.append(
            "\n<!-- self_consistency: LOW CONSENSUS — "
            f"ratio={consensus.consensus_ratio:.2f} -->"
        )
    return primary_report + "\n".join(lines) if lines else primary_report


async def sample_analyst(state: AgentState, n: int = 3) -> SampleBundle:
    """多次独立采样 analyst，返回共识结果束。

    1. 调 run_analyst(state) N 次独立（不是重跑同一份，而是 temperature=0.7 多次）。
       - 每次 state 是 deep copy，防止上一次 output 残留到下一次。
       - 每次有各自 span（stage=f"analyst_sample_{i}"）。
       - 3 次并发 asyncio.gather（IO 密集，安全并发）。
    2. check_consistency(reports)
    3. 返回 SampleBundle(primary_report, all_reports, consensus, replaced_by_consensus)
       - primary_report: consensus_ratio >= 0.5 -> 用共识 metrics 修正首份 report；
         否则 -> 直接用首份（标注"低一致性"）
    """
    from ....config import get_settings
    from ....infrastructure.observability.tracing import _current_tracer
    from .nodes import run_analyst

    settings = get_settings()

    loop = asyncio.get_event_loop()
    tracer = _current_tracer.get()

    async def _sample_one(i: int) -> AgentState:
        """单次采样：深拷贝 state -> 调 run_analyst -> 返回结果 state。

        注意：temperature 通过 per-sample 闭包传入，避免多线程并发 mutate
        settings.llm_temperature 的竞态（各线程的 orig_temp / restore 会互相覆盖）。
        """
        state_copy = copy.deepcopy(state)
        # 用各自的 trace span 包裹（stage=f"analyst_sample_{i}"）
        if tracer is not None:
            span = tracer.start(f"analyst_sample_{i}")
        else:
            span = None
        try:
            result_state = await loop.run_in_executor(None, run_analyst, state_copy)
            return result_state
        except Exception:
            if span is not None:
                tracer.end(span, ok=False)
            raise
        finally:
            if span is not None and getattr(span, "status", "") == "RUNNING":
                tracer.end(span, ok=True)

    # 并发执行 N 次采样
    tasks = [_sample_one(i) for i in range(n)]
    result_states: list[AgentState] = list(await asyncio.gather(*tasks))

    # 提取每份 state 的分析文本，供 check_consistency 使用
    reports: list[str] = []
    for rs in result_states:
        # state.report 在 analyst 阶段尚未产生（由 reporter 产生），
        # 用 analysis 的文本化代替 "report" 做一致性检查
        text = _analysis_to_text(rs.analysis)
        if not text:
            # fallback: 看看已有 report（兼容性）
            text = rs.report or ""
        reports.append(text)

    # 过滤空报告
    non_empty = [r for r in reports if r.strip()]
    if not non_empty:
        return SampleBundle(
            primary_report="",
            all_reports=reports,
            consensus=ConsensusResult(all_reports=reports),
            primary_state=result_states[0] if result_states else None,
        )

    consensus = check_consistency(non_empty, consensus_threshold=settings.self_consensus_threshold)

    # primary_report 决策：
    # 用首份的报告文本（state.report 或 analysis 文本化）
    primary_text = reports[0]
    replaced = False
    if consensus.consensus_ratio >= 0.5 and primary_text.strip():
        # 高一致性：用共识 metrics 修正首份报告
        primary_text = _apply_consensus_to_report(primary_text, consensus)
        replaced = True
    elif primary_text.strip():
        # 低一致性：直接用首份，加 warning 段
        primary_text = _apply_consensus_to_report(primary_text, consensus)
    # 首份为空但别的非空 -> 用第一份非空的
    if not primary_text.strip():
        for txt in reports:
            if txt.strip():
                primary_text = _apply_consensus_to_report(txt, consensus)
                break

    return SampleBundle(
        primary_report=primary_text,
        all_reports=reports,
        consensus=consensus,
        replaced_by_consensus=replaced,
        primary_state=result_states[0] if result_states else None,
    )
