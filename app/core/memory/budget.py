"""Context budget helpers — keep injected memory/context bounded.

A token budget has hard ceilings (system > safety > tools > high-priority memory
> recent > history) and must leave ~10-20% headroom for model output. We don't
count tokens exactly here; the heuristic is a char proxy (CJK ≈ 1 token/char,
English ≈ 4 char/token), applied in priority order. Swap in tiktoken when the
LLM SDK doesn't report exact usage.
"""
from __future__ import annotations

from typing import Any

_TRUNC_MARKER = "\n…[截断]"


def truncate(text: Any, max_chars: int, marker: str = _TRUNC_MARKER) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - len(marker))] + marker


def fit_to_budget(ordered: list[tuple[str, str]], max_chars: int,
                  headroom_ratio: float = 0.15) -> dict[str, str]:
    """按优先级顺序分配预算：先到的保满，后到的截断/留空。

    ``ordered`` 为 (key, value) 列表（优先级从高到低）。返回 {key: 截断后文本}。
    """
    usable = max(0, int(max_chars * (1.0 - headroom_ratio)))
    out: dict[str, str] = {}
    remaining = usable
    for key, value in ordered:
        if remaining <= 0:
            out[key] = ""
            continue
        v = str(value or "")
        if len(v) <= remaining:
            out[key] = v
            remaining -= len(v)
        else:
            out[key] = truncate(v, remaining)
            remaining = 0
    return out


def estimate_tokens(text: str) -> int:
    """Char-based token proxy（中文≈1/字，拉丁≈1/4字符）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)
