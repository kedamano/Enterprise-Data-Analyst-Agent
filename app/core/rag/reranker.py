"""Reranker for RAG retrieval.

A real cross-encoder (e.g. BGE-reranker) can be plugged in here; until then the
default ``_deterministic`` strategy refines the candidate list with a query
phrase-affinity score on top of the fused rank score — documents that carry the
query's exact terms/bigrams surface even if reciprocal-rank fusion ranked them
lower. Enabled via ``rerank_enabled`` (default true); when a cross-encoder is
configured and loads within budget it is used instead.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional

from ...config import get_settings

_CJK = "一-鿿"


def _affinity_tokens(text: str) -> set[str]:
    """词级 + CJK 重叠二元组 token，作为查询短语亲和度判据。"""
    words = re.findall(rf"[\w{_CJK}]+", text.lower())
    words = [w for w in words if len(w) > 1]
    out = set(words)
    for w in words:
        if len(w) > 2 and re.search(rf"[{_CJK}]", w):
            out.update(w[i:i + 2] for i in range(len(w) - 1))
    return out


def _phrase_affinity(query: str, text: str) -> float:
    q = _affinity_tokens(query)
    if not q:
        return 0.0
    t = _affinity_tokens(text)
    if not t:
        return 0.0
    return len(q & t) / len(q)


# --- optional cross-encoder (lazy, bounded) -------------------------------- #
_ce_model = None
_ce_error: Optional[str] = None
_ce_lock = threading.Lock()


def _cross_encoder_available() -> bool:
    global _ce_model, _ce_error
    if _ce_model is not None:
        return True
    if _ce_error is not None:
        return False
    settings = get_settings()
    if not settings.rerank_cross_encoder:
        return False
    with _ce_lock:
        if _ce_model is not None or _ce_error is not None:
            return _ce_model is not None
        holder: dict[str, Any] = {}

        def _load() -> None:
            try:
                from sentence_transformers import CrossEncoder  # lazy
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
                holder["m"] = CrossEncoder(settings.rerank_cross_encoder)
            except Exception as exc:  # noqa: PERF203
                holder["e"] = exc

        t = threading.Thread(target=_load, daemon=True)
        t.start()
        t.join(settings.embed_load_timeout_s)
        if "m" in holder:
            _ce_model = holder["m"]
        else:
            _ce_error = f"cross-encoder 不可用: {holder.get('e') or 'timeout'}"
    return _ce_model is not None


def _rerank_cross_encoder(query: str, chunks: list[dict[str, Any]],
                          top_k: int) -> list[dict[str, Any]]:
    pairs = [(query, c.get("text", "")) for c in chunks]
    scores = _ce_model.predict(pairs)  # type: ignore[misc]
    for c, s in zip(chunks, scores):
        c["rerank_score"] = round(float(s), 4)
    chunks.sort(key=lambda c: c["rerank_score"], reverse=True)
    return chunks[:top_k]


def _deterministic(query: str, chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    if not chunks:
        return chunks
    fused = [c.get("score", 0.0) for c in chunks]
    lo, hi = min(fused), max(fused)
    span = (hi - lo) or 1.0
    for c in chunks:
        aff = _phrase_affinity(query, c.get("text", ""))
        norm = (c.get("score", 0.0) - lo) / span
        # 短语亲和主导，fused 分仅作同亲和小数位的微调——否则重排会被原排序吞没
        c["rerank_score"] = round(0.8 * aff + 0.2 * norm, 4)
    chunks.sort(key=lambda c: c["rerank_score"], reverse=True)
    return chunks[:top_k]


def rerank(query: str, chunks: list[dict[str, Any]], top_k: Optional[int] = None) -> list[dict[str, Any]]:
    if not chunks:
        return chunks
    top_k = top_k or len(chunks)
    if _cross_encoder_available():
        return _rerank_cross_encoder(query, chunks, top_k)
    return _deterministic(query, chunks, top_k)
