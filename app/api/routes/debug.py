"""Debug / observability endpoints – recent trace spans and persisted runs."""
from __future__ import annotations

from fastapi import APIRouter

from ...config import get_settings
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


@router.get("/audit")
def query_audit(kind: str = "", session_id: str = "", since: str = "", limit: int = 200):
    """D46：SQL 审计查询（需 `AUDIT_BACKEND=sqlite|postgres`）。

    `jsonl` 后端下返回空并给出提示——**如实说明**而不是假装查过了
    （与"零 claim ≠ 零幻觉"同一条纪律：没查到要说清是"没有"还是"没接"）。
    """
    from ...core.security import audit_store

    backend = get_settings().audit_backend
    if backend == "jsonl":
        return {"backend": backend, "count": 0, "events": [],
                "note": "当前为 jsonl 后端，无法 SQL 查询；"
                        "设 AUDIT_BACKEND=sqlite 并用 audit_store.import_jsonl() 迁入历史数据"}
    events = audit_store.query(kind=kind or None, session_id=session_id or None,
                               since=since or None, limit=limit)
    return {"backend": backend, "count": len(events), "events": events}
