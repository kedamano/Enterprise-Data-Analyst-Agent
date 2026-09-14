"""E4/03 口径可比性检查 — 基准里 Rubric 权重最大的那一块。

Spec: docs/specs/E4/03-caliber-comparability.md

分析报告里最难自查、也最致命的错误是**口径不可比**：
"Q1 营收 1.2 亿" 与 "Q2 营收 1.5 亿" 若口径不同（含/不含退款、不同区域范围、期间长度不等），
"环比增长 25%" 就是假的。Reflection 现有 6 个维度都不查这个。

本模块只做**结构性判定**（期间长度、分母缺失、迭代口径漂移）——确定性、可测；
"含不含退款"这类语义判读标 ``[待真实验证]``，留给 LLM 维度。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .state import CaliberCheck, CaliberIssue, ReflectionDecision, ReflectionResult

# 比率类指标：必须声明分子/分母，否则只是"看起来像个比例"
_RATIO_RE = re.compile(r"(率|占比|比例|rate|ratio|转化|复购|留存|渗透)", re.IGNORECASE)
# 分母声明标记
_DENOM_RE = re.compile(r"(分母|分子|除以|/\s*\d|其中|基数|占.{0,8}的|百分|万分)")
# 期间表达 → 天数（只处理可确定性解析的写法）
_PERIOD_PATTERNS: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"近\s*(\d+)\s*天"), 1),
    (re.compile(r"近\s*(\d+)\s*周"), 7),
    (re.compile(r"近\s*(\d+)\s*个?月"), 30),
    (re.compile(r"近\s*(\d+)\s*个?季度"), 90),
    (re.compile(r"近\s*(\d+)\s*年"), 365),
)
# 顺序即优先级：**具体写法必须排在泛化写法前**（否则 "2024年3月" 会先命中裸 "月"）
_FIXED_PERIODS: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"\d{4}\s*年\s*(\d{1,2})\s*月"), 31),   # 2024年3月
    (re.compile(r"(去年|今年|本年|上年)同期"), 365),          # 去年同期
    (re.compile(r"(本|上|下)?季(度)?"), 90),                  # 本季度 / 上季
    (re.compile(r"(去年|今年|本年|上年)"), 365),              # 去年 / 今年
    (re.compile(r"^\d{4}\s*年$"), 365),                     # 2024年
    (re.compile(r"(本|上|下)?月"), 30),                       # 上月 / 本月
    (re.compile(r"(本|上|下)?周"), 7),                        # 上周 / 本周
)
_COMPARE_RE = re.compile(r"(环比|同比|相比|对比|较上|较去年|增长|下降|变化|趋势)", re.IGNORECASE)
_PERIOD_TOLERANCE = 0.2  # 期间长度差异超过 20% 才算不可比

# --- v1.2：单位混用（unit_mismatch） --------------------------------------- #
# 长单位必须排在短单位前，否则 "1.2 亿元" 会被 "亿" 先匹配掉、丢掉 "元"。
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(百万元|亿元|千元|万元|亿|万)")
# 指标名 = 金额紧邻前方的 2~8 个中文/字母（"营收 1.2 亿元" → "营收"）
_METRIC_BEFORE_RE = re.compile(r"([一-龥A-Za-z]{2,8})\s*$")
# 指标名前的期间修饰要剥掉，否则 "营收" 与 "上年同期营收" 会被当成两个指标而漏判
_PERIOD_PREFIX_RE = re.compile(
    r"^(上年|去年|本年|今年|上期|本期|同期|当期|上月|本月|当月|上季度|本季度|去年|同期|同比|环比)+")

# --- v1.2：限定词极性冲突（filter_mismatch） -------------------------------- #
# 交替顺序即优先级：`不含` 必须排在 `含` 前，否则 "不含退款" 会被当成"含退款"。
_QUALIFIER_RE = re.compile(
    r"(?P<pol>不含|不包含|不包括|剔除|排除|扣除|去除|包含|包括|仅含|只含|含)"
    r"\s*(?P<obj>退款|退货|税|运费|赠品|内部|测试|异常|停用)")
_EXCLUDE_WORDS = frozenset({"不含", "不包含", "不包括", "剔除", "排除", "扣除", "去除"})


def parse_period_days(text: str) -> Optional[int]:
    """把常见期间写法解析成天数；解析不出 → None（宁缺勿滥）。"""
    if not text:
        return None
    t = str(text).strip()
    for pattern, factor in _PERIOD_PATTERNS:
        m = pattern.search(t)
        if m:
            try:
                return int(m.group(1)) * factor
            except (TypeError, ValueError):
                return None
    for pattern, days in _FIXED_PERIODS:
        if pattern.search(t):
            return days
    return None


def _text_of(analysis: Any, report: str = "") -> str:
    parts = [report or ""]
    for f in getattr(analysis, "findings", None) or []:
        parts.append(str(getattr(f, "finding", "")))
        parts.append(str(getattr(f, "interpretation", "")))
    for h in getattr(analysis, "hypotheses", None) or []:
        parts.append(str(getattr(h, "hypothesis", "")))
    for r in getattr(analysis, "recommendations", None) or []:
        parts.append(str(getattr(r, "action", "")))
    return " ".join(parts)


def _metrics_of(analysis: Any, context: Any) -> list[str]:
    names = list(getattr(context, "metrics", None) or [])
    for f in getattr(analysis, "findings", None) or []:
        for ev in getattr(f, "evidence", None) or []:
            if getattr(ev, "metric", None):
                names.append(str(ev.metric))
    seen: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


def _amounts_by_metric(text: str) -> list[tuple[str, str]]:
    """从自由文本抽出 ``(指标名, 数量级单位)``（v1.2）。

    指标名取金额**紧邻前方**的 2~8 个中文/字母，并剥掉期间修饰
    （``上年同期营收`` → ``营收``）——否则同一指标的两次取值会被当成两个指标而漏判。
    抽不出指标名的金额**直接跳过**：宁可漏判，不可把"两个不相干数字"凑成口径问题。
    """
    out: list[tuple[str, str]] = []
    for m in _AMOUNT_RE.finditer(text or ""):
        mm = _METRIC_BEFORE_RE.search((text or "")[:m.start()])
        if not mm:
            continue
        name = _PERIOD_PREFIX_RE.sub("", mm.group(1))
        if len(name) >= 2:
            out.append((name, m.group(2)))
    return out


def caliber_check(analysis: Any, context: Any = None, *, iteration: Any = None,
                  report: str = "") -> CaliberCheck:
    """确定性口径可比性检查。只报结构性问题，语义判读留给 LLM 维度（标待验）。"""
    text = _text_of(analysis, report)
    metrics = _metrics_of(analysis, context)
    issues: list[CaliberIssue] = []

    # ① 期间长度不等（拿结构化字段，比从自由文本里猜可靠）
    tr = getattr(context, "time_range", None)
    cmp_ = getattr(context, "comparison", None)
    current = " ".join(x for x in [getattr(tr, "start", None), getattr(tr, "end", None)] if x)
    baseline = str(getattr(cmp_, "period", None) or "")
    if not current and tr is not None:
        current = str(getattr(tr, "raw", None) or "")
    d_cur, d_base = parse_period_days(current), parse_period_days(baseline)
    if d_cur and d_base and abs(d_cur - d_base) / max(d_cur, d_base) > _PERIOD_TOLERANCE:
        issues.append(CaliberIssue(
            kind="period_mismatch",
            detail=(f"对比期间长度不等：本期约 {d_cur} 天 vs 基期约 {d_base} 天，"
                    "直接算环比/同比会高估或低估幅度，需对齐期间长度"),
            metric=metrics[0] if metrics else None))

    # ② 比率类指标未声明分母
    ratio_metrics = [m for m in re.findall(r"[一-龥A-Za-z]{2,8}(?:率|占比|比例)", text)]
    if (ratio_metrics or _RATIO_RE.search(text)) and not _DENOM_RE.search(text):
        issues.append(CaliberIssue(
            kind="denominator_missing",
            detail=(f"出现比率类指标（{'、'.join(ratio_metrics[:3]) or '率类指标'}）"
                    "但未声明分子/分母口径，无法判断可比性"),
            metric=ratio_metrics[0] if ratio_metrics else None))

    # ③ 迭代轮口径漂移（E3 联动）：本轮改了时间切片/粒度，就不可与上一轮直接比
    kind = str(getattr(iteration, "get", lambda *_: None)("kind") if isinstance(iteration, dict)
               else getattr(iteration, "kind", "") or "")
    if kind in ("date_change", "granularity") and _COMPARE_RE.search(text):
        issues.append(CaliberIssue(
            kind="iteration_drift",
            detail=(f"本轮为增量迭代（{kind}），口径已变更，"
                    "与上一轮结果直接做环比/同比不成立，需在同一口径下重算基期"),
            metric=metrics[0] if metrics else None))

    # ④ 同一指标混用不同数量级单位（v1.2）
    #    **只判同一指标**——不同指标用不同量级单位是正常写法（营收 1.2 亿 / 成本 3000 万），
    #    全文见两种单位就报会把正常报告全部点亮。
    by_metric: dict[str, set[str]] = {}
    for metric_name, unit in _amounts_by_metric(text):
        by_metric.setdefault(metric_name, set()).add(unit)
    mixed = {m: us for m, us in by_metric.items() if len(us) > 1}
    if mixed:
        name, units = next(iter(mixed.items()))
        issues.append(CaliberIssue(
            kind="unit_mismatch",
            detail=(f"同一指标「{name}」混用了不同数量级单位（{'、'.join(sorted(units))}），"
                    "跨单位比较前须统一换算，否则倍数是错的"),
            metric=name))

    # ⑤ 限定词极性冲突（v1.2）
    #    **只判"同一对象出现相反极性"**（含退款 vs 不含退款）。单侧过滤判据不足 → 不判（规格 §7.2）。
    pol_by_obj: dict[str, set[str]] = {}
    for m in _QUALIFIER_RE.finditer(text):
        pol = "exclude" if m.group("pol") in _EXCLUDE_WORDS else "include"
        pol_by_obj.setdefault(m.group("obj"), set()).add(pol)
    conflicts = [o for o, ps in pol_by_obj.items() if len(ps) > 1]
    if conflicts:
        issues.append(CaliberIssue(
            kind="filter_mismatch",
            detail=(f"同一份报告里「{'、'.join(conflicts)}」出现了相反的限定口径"
                    "（含/不含并存），两侧数字不可直接比较"),
            metric=None))

    return CaliberCheck(comparable=not issues, checked_metrics=metrics, issues=issues)


def apply_caliber(check: CaliberCheck,
                  reflection: Optional[ReflectionResult]) -> ReflectionDecision:
    """口径问题施加到 Reflection 决策——同样**只收紧**。

    `iteration_drift` 抬到 REPLAN：口径变了还并列比较**是结论错误**，不是措辞问题。
    其余（期间不等/分母缺失）只要求披露。
    """
    base = reflection.decision if reflection else ReflectionDecision.REPLAN
    if not any(i.kind == "iteration_drift" for i in check.issues):
        return base
    return ReflectionDecision.REPLAN if base == ReflectionDecision.PASS else base


def caliber_notes(check: CaliberCheck) -> list[str]:
    return [f"[口径·{i.kind}] {i.detail}" for i in check.issues]
