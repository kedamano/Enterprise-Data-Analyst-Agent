"""Debug / observability endpoints – recent trace spans and persisted runs."""
from __future__ import annotations

from fastapi import APIRouter

from ...infrastructure.observability.tracing import load_run, recent_spans

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/traces")
def get_recent_traces(limit: int = 50):
    spans = recent_spans(limit)
    return {
        "count": len(spans),
        "recent": spans,
        "note": "spans 为有界内存缓冲，最新在前；完整持久化见 data/traces/{run_id}.jsonl",
    }


@router.get("/traces/{run_id}")
def get_trace(run_id: str):
    spans = load_run(run_id)
    if not spans:
        return {"error": f"未找到 trace: {run_id}", "spans": []}
    # 最后一行是 summary
    return {"run_id": run_id, "spans": spans[:-1], "summary": spans[-1]}
