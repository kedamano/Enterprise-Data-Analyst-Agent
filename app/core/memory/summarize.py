"""Rolling-summary condensing for short-term memory (spec §20, 05 记忆 Q8).

Short-term history is bounded; when it overflows ``max_items`` the older part is
condensed into a one-shot LLM summary (kept as ``history_summary``) while the
recent ``keep_recent`` entries are retained verbatim — so early hard constraints
are not silently dropped, they live in the summary.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from ...infrastructure.llm.router import get_llm

_SYSTEM = (
    "You are a memory summarizer for an enterprise data-analysis agent. "
    "Condense the analysis log below into 2-4 concise bullets. Keep concrete "
    "numbers, table/column names, metric names, and decisions. Do not invent facts."
)


def entry_text(e: Any) -> str:
    if isinstance(e, dict):
        role = str(e.get("role", e.get("type", "msg")))
        content = e.get("content", e.get("query", e.get("summary", "")))
        return f"{role}: {content}"
    return str(e)


def _fallback(text: str, limit: int = 400) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def summarize_text(text: str, llm: Any = None) -> str:
    """Summarize raw text via the configured LLM; degrade to truncation on failure."""
    if not text.strip():
        return ""
    llm = llm if llm is not None else get_llm()
    try:
        return (llm.complete(_SYSTEM, f"Log:\n{text}", stage="memory_summary",
                             json_mode=False) or "").strip() or _fallback(text)
    except Exception:
        return _fallback(text)


def condense_history(
    entries: Sequence[Any],
    *,
    max_items: int = 20,
    keep_recent: int = 8,
    llm: Any = None,
) -> tuple[list[Any], Optional[str]]:
    """Overflow policy: keep recent verbatim, summarize the older prefix.

    Returns ``(kept_recent, summary_text_or_None)``. No-op (None summary) when
    the history is within ``max_items``.
    """
    entries = list(entries)
    if len(entries) <= max_items:
        return entries, None
    older = entries[: len(entries) - keep_recent]
    recent = entries[-keep_recent:]
    summary = summarize_text("\n".join(entry_text(e) for e in older), llm=llm)
    return recent, summary
