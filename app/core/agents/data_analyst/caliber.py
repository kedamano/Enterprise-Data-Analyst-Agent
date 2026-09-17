"""E4/03 口径可比性检查 — 基准里 Rubric 权重最大的那一块。

Spec: docs/specs/E4/03-caliber-comparability.md

分析报告里最难自查、也最致命的错误是**口径不可比**：
"Q1 营收 1.2 亿" 与 "Q2 营收 1.5 亿" 若口径不同（含/不含退款、不同区域范围、期间长度不等），
"环比增长 25%" 就是假的。Reflection 现有 6 个维度都不查这个。

本模块只做**结构性判定**（期间长度、分母缺失、迭代口径漂移）——确定性、可测；
"含不含退款"这类语义判读标 ``[待真实验证]``，留给 LLM 维度。
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .state import CaliberCheck, CaliberIssue, ReflectionDecision, ReflectionResult

logger = logging.getLogger("da.caliber")

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


def _parse_iso_date(s: Any) -> Optional[date]:
    """ISO ``YYYY-MM-DD`` / ``YYYY/MM/DD`` → date；解析不出 → None（不猜）。"""
    if not s:
        return None
    t = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _last_day_of_month(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _whole_month(start: date, end: date) -> bool:
    """start..end 是否一个完整自然月（首日=1、末日=月末）。"""
    if start.day != 1:
        return False
    return (end.year == start.year and end.month == start.month
            and end.day == _last_day_of_month(start.year, start.month))


# 环比/同比识别（顺序无关——用子串而非精确匹配，容下"环比增长"等写法）
def _is_mom(ct: str) -> bool:
    return "环比" in ct


def _is_yoy(ct: str) -> bool:
    return "同比" in ct or "去年" in ct or ct.lower() == "yoy"


def infer_baseline_period(current_start: Any, current_end: Any,
                          comparison_type: Any
                          ) -> Optional[tuple[date, date, str, int]]:
    """D48：由本期 + 对比类型**确定性推断**基线期间。

    返回 ``(baseline_start, baseline_end, label, days)``；解析不出 → None。
    - 环比 + 完整自然月 → 上一完整自然月；否则按 (end-start) 等宽回平移。
    - 同比 + 完整自然月 → 去年同月；否则取去年同日期（2/29 → 2/28）。
    - start/end 非日期、type 非环比同比 → None（宁缺勿滥）。
    """
    s = _parse_iso_date(current_start)
    e = _parse_iso_date(current_end)
    if s is None or e is None or e < s:
        return None
    ct = str(comparison_type or "").strip()
    if not ct:
        return None
    mom, yoy = _is_mom(ct), _is_yoy(ct)
    if not (mom or yoy):
        return None

    if mom:
        if _whole_month(s, e):
            y, m = (s.year - 1, 12) if s.month == 1 else (s.year, s.month - 1)
            bs = date(y, m, 1)
            be = date(y, m, _last_day_of_month(y, m))
            return (bs, be, f"{y}年{m}月", be.day)
        width = (e - s).days
        be = s - timedelta(days=1)
        bs = be - timedelta(days=width)
        return (bs, be, f"近{width + 1}天的前一段", width + 1)

    # yoy
    if _whole_month(s, e):
        by, bm = s.year - 1, s.month
        bs = date(by, bm, 1)
        be = date(by, bm, _last_day_of_month(by, bm))
        return (bs, be, f"{by}年{bm}月", be.day)
    try:
        bs = s.replace(year=s.year - 1)
    except ValueError:  # 2/29 在去年不存在
        bs = date(s.year - 1, s.month, 28)
    try:
        be = e.replace(year=e.year - 1)
    except ValueError:
        be = date(e.year - 1, e.month, 28)
    return (bs, be, f"去年同期的近{(be - bs).days + 1}天", (be - bs).days + 1)


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

    # ⑥ 同环比基线缺/错（D48）：声明了环比/同比但基线期间未声明或与推断不符
    #    与 ① period_mismatch 互补——① 判"两期都给了但长度不等"，
    #    ⑥ 判"声明了对比类型但基线缺/错"。current 是 ISO 日期时 ① 解析不出，由 ⑥ 接管。
    cmp_type = str(getattr(cmp_, "type", None) or "")
    if cmp_type:
        inferred = infer_baseline_period(getattr(tr, "start", None),
                                         getattr(tr, "end", None), cmp_type)
        if inferred:
            _, _, blabel, bdays = inferred
            cperiod = str(getattr(cmp_, "period", None) or "")
            actual_days = parse_period_days(cperiod) if cperiod else None
            if not cperiod:
                issues.append(CaliberIssue(
                    kind="baseline_mismatch",
                    detail=(f"声明了 {cmp_type} 但未给出基线期间，"
                            f"{cmp_type} 基期应约为 {blabel}（约 {bdays} 天）"),
                    metric=metrics[0] if metrics else None))
            elif actual_days and abs(actual_days - bdays) / max(actual_days, bdays) > _PERIOD_TOLERANCE:
                issues.append(CaliberIssue(
                    kind="baseline_mismatch",
                    detail=(f"基线期间约 {actual_days} 天，{cmp_type} 应约为 {blabel}"
                            f"（约 {bdays} 天），直接比较会失真"),
                    metric=metrics[0] if metrics else None))
            # actual_days 解析不出 → 不判（宁缺勿滥）

    # ⑦ 报告口径与登记口径冲突（D48）：单位数量级不同 / 限定词极性相反
    #    读注册表故障绝不打断 caliber_check（与 gate 同纪律）。
    try:
        from . import caliber_registry as _crmod
        reg = _crmod._default_registry()
        for spec in reg.list_all():
            if not spec.metric or spec.metric not in text:
                continue
            # 单位冲突：报告里该指标的金额单位 ≠ 登记单位
            if spec.unit:
                for name, unit in _amounts_by_metric(text):
                    if name == spec.metric and unit != spec.unit:
                        issues.append(CaliberIssue(
                            kind="caliber_deviation",
                            detail=(f"指标「{spec.metric}」报告用 {unit}，"
                                    f"但登记口径为 {spec.unit}，单位不一致"),
                            metric=spec.metric))
                        break
            # 限定词极性冲突：报告限定词与登记 filters 极性相反
            reg_pols: dict[str, str] = {}
            for f in spec.filters:
                m = _QUALIFIER_RE.search(f)
                if m:
                    reg_pols[m.group("obj")] = ("exclude"
                                                if m.group("pol") in _EXCLUDE_WORDS
                                                else "include")
            for m in _QUALIFIER_RE.finditer(text):
                pol = "exclude" if m.group("pol") in _EXCLUDE_WORDS else "include"
                obj = m.group("obj")
                if obj in reg_pols and reg_pols[obj] != pol:
                    issues.append(CaliberIssue(
                        kind="caliber_deviation",
                        detail=(f"指标「{spec.metric}」报告限定词"
                                f"「{m.group('pol')}{obj}」与登记口径"
                                f"（{'、'.join(spec.filters)}）极性相反"),
                        metric=spec.metric))
                    break
    except Exception:  # noqa: BLE001
        logger.warning("caliber_deviation 检查跳过（注册表不可达）", exc_info=True)

    # E4/03 LLM 语义判读：默认关（caliber_llm_enabled=False）；打开后由 LLM 给报告里的指标
    # 做口径/限定词语义复核，产出 semantic_mismatch issues（仅收紧，不触 REPLAN）。
    # 与.registry try/except 同纪律：LLM 缺/故障 → 跳过，绝不抛。
    try:
        issues.extend(_semantic_check(analysis, context, report))
    except Exception:  # noqa: BLE001
        logger.warning("_semantic_check 跳过（LLM 不可达或关闭）", exc_info=True)

    return CaliberCheck(comparable=not issues, checked_metrics=metrics, issues=issues)


# E4/03 §semantics：结构性规则能抓"期间不等/单位混用"这种确定性口径问题，
# 但"含不含退款""剔除异常的范围是否跨期一致"这种**语义/限定词歧义**要靠 LLM 读文本才能判。
# 这里只追加 semantic_mismatch issues；apply_caliber 的 REPLAN 触发仍只认同结构类的
# iteration_drift —— 即 LLM 维度**仅收紧**，不改变现有决策走向（铁律：LLM 判错不导致误 REPLAN）。
def _semantic_check(analysis: Any, context: Any, report: str = "") -> list[CaliberIssue]:
    """LLM 语义判读（默认关，caliber_llm_enabled=False）。

    LLM 若缺/抛/返回畸形 JSON —— 全部当"无问题"（宁缺勿滥），**绝不抛**。
    """
    from app.config import get_settings
    from app.infrastructure.llm.router import get_llm

    if not get_settings().caliber_llm_enabled:
        return []

    metrics = _metrics_of(analysis, context)
    text = _text_of(analysis, report)
    if not text.strip():
        return []

    # 系统提示：强制输出固定 JSON，列出语义口径问题（可为空）。
    # 注意：下面中文示例里原本出现「含/不含」与「活跃客户」等引号内容——
    # 为免被 Python 解析器当字符串结尾，统一用全角方括号「」替代西式引号。
    sys_prompt = (
        "你是口径可比性语义判读器（E4/03 补充判定，只收紧、不触 REPLAN）。\n"
        "检查下方报告/发现文本，只判定结构性规则抓不到的语义型口径问题，例如：\n"
        "  - 同一指标前后限定词范围不一致（「含」与「不含」退款、「剔除」异常的范围跨期不同）；\n"
        "  - 同一指标在同段中使用不同口径却直接比较（未披露分母/区域/是否含税不一致）；\n"
        "  - 语义含混到没法判断口径是否可比（如未说明「活跃客户」定义直接给留存率）。\n"
        "直接比较两个数字但未声明口径一致的，视为潜在语义口径问题。\n"
        "输出严格为以下 JSON（不要 Markdown 代码块、不要注释）：\n"
    ) + '{"semantic_issues":[{"metric":"指标名或 null","detail":"一句话问题描述",' \
        '"rationale":"为什么判为语义口径不一致（引用原文片段）"}]}'
    user_prompt = json.dumps({"metrics": metrics, "report": text[:6000]},
                             ensure_ascii=False)
    try:
        raw = get_llm().complete(
            system=sys_prompt, user=user_prompt, stage="caliber",
            json_mode=True, temperature=0.0)
    except Exception:  # noqa: BLE001
        logger.warning("_semantic_check：LLM 调用失败，跳过（默认无问题）", exc_info=True)
        return []

    # 解析 LLM 响应 —— 防御性：畸形即当空
    payload: Any = None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        # 模型可能在 JSON 外包了 ```json ... ```，尝试剥离
        m = re.search(r"\{.*\}", raw or "", re.DOTALL) if isinstance(raw, str) else None
        if m:
            try:
                payload = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                payload = None
    issues: list[CaliberIssue] = []
    if not isinstance(payload, dict):
        return issues
    for item in payload.get("semantic_issues") or []:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("detail") or "").strip()
        rationale = str(item.get("rationale") or "").strip()
        if not detail:
            continue  # 没 detail 的语义项当空
        issues.append(CaliberIssue(
            kind="semantic_mismatch",
            detail=detail,
            metric=(str(item.get("metric") or "").strip() or None),
            rationale=rationale))
    return issues


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
