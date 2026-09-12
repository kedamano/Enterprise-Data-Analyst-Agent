"""RAG retriever – thin wrapper over the knowledge store used by ``knowledge_search``."""
from __future__ import annotations

from typing import Any

from ..tools.knowledge_tool import KnowledgeStore, get_store


def retrieve(query: str, top_k: int = 4, store: KnowledgeStore | None = None) -> list[dict[str, Any]]:
    return (store or get_store()).search(query, top_k)
