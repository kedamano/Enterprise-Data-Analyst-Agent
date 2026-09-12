from __future__ import annotations

from fastapi import APIRouter

from ...etl.pipeline import ingest_file, ingest_text
from ...models.schemas import IngestRequest, IngestResponse

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest):
    if req.file_path:
        n = ingest_file(req.file_path)
        return IngestResponse(chunks=n, source=req.file_path)
    if req.text:
        n = ingest_text(req.text, source=req.source)
        return IngestResponse(chunks=n, source=req.source)
    return IngestResponse(chunks=0, source=req.source)
