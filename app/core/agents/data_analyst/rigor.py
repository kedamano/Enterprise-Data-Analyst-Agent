"""E5/01 + E5/02 统计严谨与对抗性数据质量指令。

Spec: docs/specs/E5/01-statistical-rigor.md、docs/specs/E5/02-adversarial-quality.md

与 `gate.py` 同一范式：**纯函数 + 违规清单**，不花 LLM、不发查询、**不依赖 scipy**
（沙箱只有 pandas/numpy/matplotlib）。只检查"该声明的有没有声明"，不判断"算得对不对"——
后者依赖真实模型，标 ``[待真实验证]``。

两条线共用一个要点：**宁可披露，不可静默**。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .gate import GateIssue

# 显式"忽略数据质量问题"的指令（注意：只认显式，不认"先给个粗结论"这类正常需求）
_DQ_OVERRIDE_RE = re.compile(
    r"(别管|不用管|不要管|忽略|无视|跳过)\s*(数据|质量问题|异常|缺失)?"
    r"|数据有问题也|直接给结论|不准也没关系|别标注|别提示|别写局限",
    re.IGNORECASE,
)
# 否定优先：用户同时要求"但也告诉我问题" → 不算覆盖请求
_DQ_NEGATE_RE = re.compile(r"(但|不过|同时|仍|也)\s*(要|请|需)?\s*(告诉|说明|标注|提示|列出)", re.IGNORECASE)

# 两期数值对比的主张
_COMPARE_CLAIM_RE = re.compile(
    r"(环比|同比|较上|相比|对比|增长|下降|提升|降低|上升)\s*[\d.]+\s*%", re.IGNORECASE)
_MULTI_COMPARE_RE = re.compile(r"(环比|同比|相比|对比)", re.IGNORECASE)
# 因果性措辞
_CAUSAL_RE = re.compile(r"(导致|造成|因为|由于|使得|caused by|led to)", re.IGNORECASE)


def _stats_notes(analysis: Any) -> list[Any]:
    return list(getattr(analysis, "stats_notes", None) or [])


def _finding_texts(analysis: Any) -> list[str]:
    out: list[str] = []
    for f in getattr(analysis, "findings", None) or []:
        out.append(f"{getattr(f, 'finding', '')} {getattr(f, 'interpretation', '')}")
    return out


def _numeric_evidence_claims(analysis: Any) -> int:
    n = 0
    for f in getattr(analysis, "findings", None) or []:
        for ev in getattr(f, "evidence", None) or []:
            v = getattr(ev, "value", None)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                n += 1
            elif isinstance(v, str) and _COMPARE_CLAIM_RE.search(v):
                n += 1
    return n


def stats_issues(analysis: Any) -> list[GateIssue]:
    """统计声明缺失类问题（全部 ANNOTATE —— 硬拦会制造报警疲劳）。"""
    issues: list[GateIssue] = []
    texts = _finding_texts(analysis)
    joined = " ".join(texts)
    notes = _stats_notes(analysis)
    seen: set[str] = set()

    def _add(code: str, detail: str) -> None:
        if code in seen:  # 每个 code 每次运行最多报一次
            return
        seen.add(code)
        issues.append(GateIssue(code=code, severity="ANNOTATE", detail=detail))

    # ① 有两期数值对比，却没有任何统计声明
    claims = [t for t in texts if _COMPARE_CLAIM_RE.search(t)]
    if claims and not notes:
        _add("untested_comparison",
             f"结论含 {len(claims)} 处期间对比（{claims[0][:40]}…）但无 stats_notes 声明，"
             "差异可能只是波动；需注明方法/样本量或标注为未检验")

    # ② 声称显著却没给样本量
    for note in notes:
        if getattr(note, "significant", None) is not None and getattr(note, "n", None) is None:
            _add("significance_without_n",
                 "stats_notes 声称显著/不显著但未给出样本量 n，无法判断检验是否有效")
            break

    # ③ 强结论假设缺样本量
    for h in getattr(analysis, "hypotheses", None) or []:
        if getattr(h, "result", "") in ("SUPPORTED", "PARTIALLY_SUPPORTED"):
            if not any(getattr(n, "n", None) for n in notes):
                _add("hypothesis_strong_without_n",
                     "存在 SUPPORTED 的假设但全篇未声明样本量，结论强度无法评估")
                break

    # ④ 多重比较未校正
    if len(_MULTI_COMPARE_RE.findall(joined)) >= 5 and not any(
            "多重" in str(getattr(n, "note", "")) for n in notes):
        _add("multi_comparison_unadjusted",
             "同一报告含 ≥5 组对比且未说明多重比较校正，'至少一组显著'很可能只是偶然")

    # ⑤ 因果措辞 vs 假设结论
    if _CAUSAL_RE.search(joined) and not any(
            getattr(h, "result", "") == "SUPPORTED" for h in
            (getattr(analysis, "hypotheses", None) or [])):
        _add("causal_overreach",
             "结论使用因果措辞（导致/由于…）但没有 SUPPORTED 的假设支撑；"
             "相关不等于因果，需改为假设或补实验证据")

    return issues


def dq_override_requested(user_query: str) -> bool:
    """用户是否**显式**要求忽略数据质量问题（否定优先）。"""
    q = str(user_query or "")
    if not _DQ_OVERRIDE_RE.search(q):
        return False
    if _DQ_NEGATE_RE.search(q):
        return False  # "别管数据问题，但也要告诉我哪里有问题" → 不算覆盖
    return True


def _has_quality_disclosure(analysis: Any) -> bool:
    if getattr(analysis, "quality_notes", None):
        return True
    for lim in getattr(analysis, "limitations", None) or []:
        if re.search(r"(数据质量|缺失|口径|未检验|样本量)", str(lim)):
            return True
    return False


def adversarial_issues(user_query: str, analysis: Any) -> list[GateIssue]:
    """对抗性指令检查。`dq_override_silent` 是本项目**唯一**新增的 BLOCK。

    理由：静默遵从"别管数据问题"不是"分析得不够好"，而是**顺从隐瞒已知问题**，
    与铁律 4（证据优先）直接冲突。
    """
    issues: list[GateIssue] = []
    if not dq_override_requested(user_query):
        return issues

    if _has_quality_disclosure(analysis):
        issues.append(GateIssue(
            code="dq_override_requested", severity="ANNOTATE",
            detail="用户要求忽略数据质量问题；本报告仍标注了已知质量问题（不静默遵从）",
            fixable=False))
    else:
        issues.append(GateIssue(
            code="dq_override_silent", severity="BLOCK",
            detail="用户要求忽略数据质量问题，而报告**完全未提及**任何质量问题——"
                   "不得顺从隐瞒；必须补上数据质量声明",
            fixable=True))
    return issues


def dq_disclosure_note(user_query: str, analysis: Any) -> Optional[str]:
    """命中覆盖请求时强制写进 quality_notes 的披露文本。"""
    if not dq_override_requested(user_query):
        return None
    return ("[dq_override_requested] 用户要求忽略数据质量问题；"
            "本报告仍标注已知的质量问题与口径局限。")


def ensure_dq_disclosure(state: Any) -> Any:
    """轻模式兜底：ROUTE 的 sql_only/quick_answer 跳过 Analyst/Reflection/Reporter，
    因此质量门禁整条不跑 —— 而"用户要求忽略数据问题"恰恰在轻模式下最容易变成**静默顺从**。

    这里只做一件事：命中覆盖请求时，把披露**直接追加进报告文本**
    （轻模式不走 reporter，`quality_notes` 不会被渲染出来）。
    """
    try:
        if not dq_override_requested(getattr(state, "user_query", "")):
            return state
        note = ("> ⚠ **数据质量提示**：你要求忽略数据质量问题；本次仍按原样给出结果，"
                "但未做数据质量校验，结论仅作方向参考。")
        if note not in (getattr(state, "report", "") or ""):
            state.report = (state.report or "") + "\n\n" + note
        notes = list(getattr(getattr(state, "analysis", None), "quality_notes", None) or [])
        if not any("用户要求忽略数据质量问题" in n for n in notes):
            state.analysis.quality_notes = notes + [
                "[dq_override_requested] 用户要求忽略数据质量问题；轻模式未做质量校验，已显式提示。"]
        state.metadata.setdefault("gate_issues", []).append(
            {"code": "dq_override_requested", "severity": "ANNOTATE",
             "detail": "用户要求忽略数据质量问题；轻模式下已显式提示，未静默遵从"})
    except Exception as exc:  # 披露兜底绝不打断主流程
        state.metadata["dq_disclosure_error"] = str(exc)
    return state
