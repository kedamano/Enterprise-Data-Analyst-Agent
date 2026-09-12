"""ETL – ingest pipeline: parse → chunk → index into the knowledge store."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.rag.retriever import get_store
from ..core.tools.knowledge_tool import KnowledgeStore
from .chunker import chunk_text
from .parser import parse_file


def ingest_file(path: str | Path, store: KnowledgeStore | None = None) -> int:
    store = store or get_store()
    text = parse_file(path)
    return ingest_text(text, source=str(path), store=store)


def ingest_text(text: str, source: str = "inline", store: KnowledgeStore | None = None) -> int:
    store = store or get_store()
    chunks = chunk_text(text)
    n = 0
    for c in chunks:
        store.add(c, source)
        n += 1
    return n
