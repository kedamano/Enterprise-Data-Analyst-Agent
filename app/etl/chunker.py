"""ETL – text chunking.

Splits long documents into overlapping windows suitable for embedding/retrieval.
Default: ~600 chars with 100-char overlap, sentence-aware at boundaries.
"""
from __future__ import annotations

import re

_CHUNK = 600
_OVERLAP = 100


def chunk_text(text: str, chunk_size: int = _CHUNK, overlap: int = _OVERLAP) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= chunk_size:
        return [text] if text else []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        window = text[start:end]
        # try to break on a sentence/paragraph boundary
        if end < len(text):
            for sep in ["\n\n", "。", ". ", "\n", " "]:
                idx = window.rfind(sep)
                if idx > chunk_size * 0.5:
                    end = start + idx + len(sep)
                    window = text[start:end]
                    break
        chunks.append(window.strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]
