from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from ...config import get_settings
from ...core.safe_fs import purge_file
from ...core.tools.knowledge_tool import (
    delete_document,
    kb_status,
    list_documents,
    search_documents,
)
from ...etl.pipeline import ingest_file, ingest_text
from ...models.schemas import (
    IngestRequest,
    IngestResponse,
    KBDeleteResponse,
    KBListResponse,
    KBSearchResponse,
    KBStatus,
)

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest):
    """服务端文件路径 / 内联文本入库（CLI / 自动化用）。"""
    if req.file_path:
        n = ingest_file(req.file_path)
        return IngestResponse(chunks=n, source=req.file_path)
    if req.text:
        n = ingest_text(req.text, source=req.source)
        return IngestResponse(chunks=n, source=req.source)
    return IngestResponse(chunks=0, source=req.source)


@router.get("", response_model=KBListResponse)
def list_docs():
    """列出知识库全部来源及其分块数 + 整体状态（管理面板用）。"""
    return KBListResponse(status=KBStatus(**kb_status()), documents=list_documents())


@router.post("/upload", response_model=IngestResponse)
async def upload_ingest(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    source: str | None = Form(default=None),
):
    """浏览器上传入库：接收文件（落临时文件后解析）或粘贴文本。

    文件类走 ``ingest_file``（ETL 解析 → 分块 → 入库）；文本类走 ``ingest_text``。
    这是前端「知识库」面板真正的入库入口——此前只有服务端文件路径版本，
    前端无法使用，导致知识库只能做成占位。
    """
    if file is not None and (file.filename or "").strip():
        raw = await file.read()
        if len(raw) == 0:
            raise HTTPException(status_code=400, detail="文件为空")
        suffix = Path(file.filename).suffix or ".txt"
        tmp = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as tf:
                tf.write(raw)
                tmp = tf.name
            n = ingest_file(tmp, source=file.filename)
        finally:
            if tmp:
                purge_file(tmp)
        return IngestResponse(chunks=n, source=file.filename)

    if text and text.strip():
        src = (source or "").strip() or "inline"
        n = ingest_text(text, source=src)
        return IngestResponse(chunks=n, source=src)

    raise HTTPException(status_code=400, detail="必须提供 file 或 text")


@router.get("/search", response_model=KBSearchResponse)
def search(q: str = Query(..., min_length=1), top_k: int = Query(5, ge=1, le=20)):
    """检索预览：返回与 query 最相关的分块（管理面板验证用）。"""
    return KBSearchResponse(query=q, hits=search_documents(q, top_k))


@router.delete("", response_model=KBDeleteResponse)
def delete(source: str = Query(..., min_length=1)):
    """删除某个来源的全部分块（管理面板清理用）。"""
    n = delete_document(source)
    return KBDeleteResponse(source=source, deleted=n)
