"""INTERVIEW/01 ① 记忆三因子打分：相关性 / 时效 / 重要性。

Spec: docs/specs/INTERVIEW/01-gap-fill.md §1

原来的 `long_term.search` 是**子串匹配 + 时间倒序**——八股文 05.6 明确问
「记忆检索怎么排序」，这个答案太朴素：一条三个月前的高相关结论，会输给昨天的无关摘要。

三因子加权（权重可配）：`0.6*相关性 + 0.25*时效 + 0.15*重要性`。
时效用指数衰减；**没有 `ts` 的旧条目给中性值**（不因"没时间戳"被判过期，也不占便宜）。
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from ..text import tokenize  # 分词与「工具路由」共用一份，避免两处漂移

# 重要性：按记忆类型给底分，再用 confidence 缩放
_TYPE_IMPORTANCE = {
    "lesson": 1.0,            # 踩过的坑最值钱
    "analysis_summary": 0.7,
    "kpi_definition": 0.9,
}
_DEFAULT_IMPORTANCE = 0.5
_NEUTRAL_RECENCY = 0.5        # 无 ts 的旧条目
_SECONDS_PER_DAY = 86400.0


def relevance(entry: dict, query: str) -> float:
    """query 词元被 entry 覆盖的比例（0~1）。"""
    q_terms = tokenize(query)
    if not q_terms:
        return 0.0
    text = " ".join(str(v) for v in entry.values() if isinstance(v, (str, int, float)))
    hit = len(q_terms & tokenize(text))
    return hit / len(q_terms)


def recency(entry: dict, *, now: Optional[float] = None,
            half_life_days: float = 30.0) -> float:
    """指数衰减；无 ts → 中性 0.5（旧数据不背锅）。"""
    ts = entry.get("ts")
    if not ts:
        return _NEUTRAL_RECENCY
    try:
        stamp = float(ts) if isinstance(ts, (int, float)) else _parse_iso(str(ts))
    except Exception:
        return _NEUTRAL_RECENCY
    if stamp is None:
        return _NEUTRAL_RECENCY
    age_days = max(0.0, ((now if now is not None else time.time()) - stamp) / _SECONDS_PER_DAY)
    if half_life_days <= 0:
        return 1.0
    return float(math.exp(-age_days / half_life_days))


def _parse_iso(text: str) -> Optional[float]:
    from datetime import datetime, timezone

    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def importance(entry: dict) -> float:
    """显式 `importance` 优先；否则按类型底分 × confidence 缩放。"""
    explicit = entry.get("importance")
    if isinstance(explicit, (int, float)):
        return max(0.0, min(1.0, float(explicit)))
    base = _TYPE_IMPORTANCE.get(str(entry.get("type") or ""), _DEFAULT_IMPORTANCE)
    conf = entry.get("confidence")
    if isinstance(conf, (int, float)):
        # confidence 只做小幅缩放：0.0 → ×0.6，1.0 → ×1.0（不让它盖过类型差异）
        base *= 0.6 + 0.4 * max(0.0, min(1.0, float(conf)))
    return base


def score_entry(entry: dict, query: str, *, now: Optional[float] = None,
                half_life_days: float = 30.0,
                weights: tuple[float, float, float] = (0.6, 0.25, 0.15)) -> float:
    """三因子加权得分（越大越该被召回）。"""
    w_rel, w_rec, w_imp = weights
    return (w_rel * relevance(entry, query)
            + w_rec * recency(entry, now=now, half_life_days=half_life_days)
            + w_imp * importance(entry))


def rank(entries: list[dict], query: str, *, top_k: int = 5,
         now: Optional[float] = None, half_life_days: float = 30.0,
         weights: tuple[float, float, float] = (0.6, 0.25, 0.15)) -> list[dict]:
    """按三因子排序；**同分按新→旧**（稳定，便于测试与复现）。"""
    def _tiebreak(entry: dict) -> str:
        """**输入顺序无关**的稳定标识：不同后端（PG 按 ts 倒序 / JSONL 扫描顺序）
        给出的候选顺序不同，若靠 sort 的稳定性兜底，结果就不可复现。"""
        ident = entry.get("id")
        if ident:
            return str(ident)
        return str(entry.get("text") or "")[:200]

    scored = [(score_entry(e, query, now=now, half_life_days=half_life_days,
                           weights=weights), e) for e in entries]
    # 分数 → 时效 → 标识，全部降序；标识兜底保证任何输入顺序下结果一致
    scored.sort(key=lambda pair: (pair[0], recency(pair[1], now=now,
                                                  half_life_days=half_life_days),
                                  _tiebreak(pair[1])), reverse=True)
    return [e for _, e in scored[:top_k]]
