"""ETL – ingest pipeline: parse → chunk → index into the knowledge store."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.rag.retriever import get_store
from ..core.tools.knowledge_tool import KnowledgeStore
from .chunker import chunk_structured
from .parser import parse_file


def ingest_file(
    path: str | Path,
    store: KnowledgeStore | None = None,
    source: str | None = None,
    kb_id: str | None = None,
) -> int:
    """解析文件 → 分块 → 入库，返回入库分块数。

    ``source`` 显式指定入库来源标识；缺省时退化为文件路径本身。

    ⚠️ 浏览器上传场景**必须**传原始文件名：上传的文件先落到服务端临时路径再解析，
    若沿用默认值，入库来源会变成 ``C:\\...\\Temp\\tmpXXXX.md``——前端知识库列表将
    满是无意义临时路径，且与 ``IngestResponse.source`` 报出的名字不一致。

    ``kb_id``：归属的知识库（多知识库）。为空时等价于历史行为（跨库/无归属）。
    """
    store = store or get_store()
    text = parse_file(path)
    return ingest_text(text, source=source or str(path), store=store, kb_id=kb_id)


def ingest_text(text: str, source: str = "inline", store: KnowledgeStore | None = None,
                kb_id: str | None = None) -> int:
    """Parse-free entry point (already-extracted text → chunk → store)."""
    store = store or get_store()
    return store.rebuild_source(source, text, kb_id=kb_id)["added"]
