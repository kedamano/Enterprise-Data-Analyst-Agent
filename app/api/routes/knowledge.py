"""知识库（多库 RAG）管理接口 —— 「我的知识库」后端。

一个知识库 = 一份独立的 RAG 资产：有名字、类型（通用 / 网站）、可见性、
创建者，内部装若干文档（文件 / 粘贴文本 / 网页），可单独检索与删除。

**设计要点**：
* 库与文档的元信息在 ``core/knowledge_catalog.py``（独立 SQLite），
  分块与向量在 ``KnowledgeStore`` / Milvus —— 换向量后端不丢库结构。
* 文档入库统一走 ``etl.pipeline``（parse → chunk → store），并**显式传文件名
  作为 source**：上传文件先落临时路径，若沿用路径当来源，库里会存成
  ``C:\\...\\Temp\\tmpXXXX.md``，列表上全是无意义的临时路径。
* 删库/删文档都按 ``kb_id`` 限定范围，绝不误伤其他库的同名来源。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from ...config import Settings
from ...core.knowledge_catalog import get_catalog
from ...core.safe_fs import purge_tree
from ...core.tools.knowledge_tool import (
    get_store,
    kb_backend,
    search_documents,
)
from ...core.web_ingest import fetch_website
from ...etl.pipeline import ingest_file, ingest_text
from ...models.schemas import (
    KBDeleteResponse,
    KBHit,
    KBSearchResponse,
    KbBase,
    KbBaseListResponse,
    KbCreateRequest,
    KbDocument,
    KbDocumentListResponse,
    KbIngestResponse,
    KbPreviewResponse,
    KbTextRequest,
    KbUpdateRequest,
    KbWebsiteRequest,
)

# D59 运行时版本覆盖：rotate 只改当前进程的内存态；进程重启恢复 settings 默认。
_runtime_emv_override: str | None = None

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])

MAX_BYTES = 20 * 1024 * 1024


def _backend() -> str:
    """知识库后端三态（观测事实）：milvus / sqlite_fallback / sqlite。

    交给 ``knowledge_tool.kb_backend()`` 统一判定，避免各接口各判一套。
    """
    try:
        return kb_backend()
    except Exception:
        return "sqlite"


def _require_base(kb_id: str) -> dict[str, Any]:
    base = get_catalog().get_base(kb_id)
    if not base:
        raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")
    return base


def _with_live_counts(base: dict[str, Any]) -> dict[str, Any]:
    """用向量库的真实分块数覆盖目录里的登记值（登记可能因重建索引而过期）。"""
    try:
        base["chunks"] = get_store().total_chunks(base["id"])
    except Exception:
        pass
    return base


@router.get("", response_model=KbBaseListResponse)
def list_bases():
    catalog = get_catalog()
    bases = [_with_live_counts(b) for b in catalog.list_bases()]
    total = 0
    try:
        total = get_store().total_chunks()
    except Exception:
        pass
    return KbBaseListResponse(
        bases=[KbBase(**b) for b in bases], backend=_backend(), total_chunks=total
    )


@router.post("", response_model=KbBase)
def create_base(req: KbCreateRequest):
    if not (req.name or "").strip():
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    return KbBase(**get_catalog().create_base(
        req.name, req.description, req.kb_type, req.visibility
    ))


@router.patch("/{kb_id}", response_model=KbBase)
def update_base(kb_id: str, req: KbUpdateRequest):
    _require_base(kb_id)
    updated = get_catalog().update_base(
        kb_id, name=req.name, description=req.description,
        kb_type=req.kb_type, visibility=req.visibility,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return KbBase(**_with_live_counts(updated))


@router.delete("/{kb_id}")
def delete_base(kb_id: str):
    """删库：级联清空该库的全部分块（其他库不受影响）。"""
    _require_base(kb_id)
    removed = 0
    try:
        removed = get_store().delete_kb_chunks(kb_id)
    except Exception:
        pass
    get_catalog().delete_base(kb_id)
    return {"ok": True, "kb_id": kb_id, "deleted_chunks": removed}


@router.get("/{kb_id}/documents", response_model=KbDocumentListResponse)
def list_documents(kb_id: str):
    _require_base(kb_id)
    return KbDocumentListResponse(
        kb_id=kb_id,
        documents=[KbDocument(**d) for d in get_catalog().list_documents(kb_id)],
    )


@router.post("/{kb_id}/documents/upload", response_model=KbIngestResponse)
async def upload_document(kb_id: str, file: UploadFile = File(...)):
    _require_base(kb_id)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限"
        )
    name = file.filename or "unnamed"
    suffix = Path(name).suffix.lower()
    tmp_dir = Path(tempfile.mkdtemp(prefix="kbupload_"))
    tmp = tmp_dir / f"payload{suffix}"
    tmp.write_bytes(raw)
    try:
        chunks = ingest_file(tmp, source=name, kb_id=kb_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析入库失败：{exc}") from exc
    finally:
        # 走 safe_fs 而非 shutil.rmtree(ignore_errors=True)：后者只忽略 OSError，
        # 挡不住环境安全删除钩子抛出的 SystemExit（BaseException），会导致整个
        # 服务进程退出。见 app/core/safe_fs.py 的说明。
        purge_tree(tmp_dir)

    doc = get_catalog().add_document(
        kb_id, name=name, source=name, doc_type="file",
        mime=file.content_type or "", bytes=len(raw), chunks=chunks,
    )
    hint = None
    if chunks == 0:
        hint = "文件已登记，但未解析出可入库文本（可能是空文件或扫描版 PDF）。"
    return KbIngestResponse(document=KbDocument(**doc), hint=hint)


@router.post("/{kb_id}/documents/text", response_model=KbIngestResponse)
def add_text(kb_id: str, req: KbTextRequest):
    _require_base(kb_id)
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="文本内容为空")
    name = (req.name or "").strip() or "粘贴文本"
    # source 用文档名：同库同名 = 更新该文档（重建索引），符合"再贴一次就是改"
    chunks = ingest_text(text, source=name, kb_id=kb_id)
    doc = get_catalog().add_document(
        kb_id, name=name, source=name, doc_type="text",
        bytes=len(text.encode("utf-8")), chunks=chunks,
    )
    return KbIngestResponse(document=KbDocument(**doc))


@router.post("/{kb_id}/documents/website", response_model=KbIngestResponse)
def add_website(kb_id: str, req: KbWebsiteRequest):
    """网站知识库：抓取页面 → 抽正文 → 切块入库。"""
    _require_base(kb_id)
    url = (req.url or "").strip()
    fetched = fetch_website(url)
    if not fetched.get("ok"):
        raise HTTPException(
            status_code=400, detail=fetched.get("error") or "抓取失败"
        )
    title = (req.title or "").strip() or fetched.get("title") or url
    chunks = ingest_text(fetched["text"], source=url, kb_id=kb_id)
    doc = get_catalog().add_document(
        kb_id, name=title, source=url, doc_type="website",
        bytes=int(fetched.get("bytes") or 0), chunks=chunks,
    )
    hint = "页面内容过长，已截断后入库。" if fetched.get("truncated") else None
    return KbIngestResponse(document=KbDocument(**doc), hint=hint)


@router.delete("/{kb_id}/documents/{doc_id}", response_model=KBDeleteResponse)
def delete_document(kb_id: str, doc_id: str):
    _require_base(kb_id)
    catalog = get_catalog()
    doc = catalog.get_document(doc_id)
    if not doc or doc["kb_id"] != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        deleted = get_store().delete_source(doc["source"], kb_id)
    except Exception:
        deleted = 0
    catalog.delete_document(doc_id)
    return KBDeleteResponse(source=doc["source"], deleted=deleted)


@router.get("/{kb_id}/search", response_model=KBSearchResponse)
def search_in_base(kb_id: str, q: str = Query(..., min_length=1), top_k: int = 5):
    _require_base(kb_id)
    hits = search_documents(q, max(1, min(top_k, 20)), kb_id=kb_id)
    return KBSearchResponse(
        query=q,
        hits=[KBHit(source=h.get("source", ""), text=h.get("text", ""),
                    score=float(h.get("score", 0.0))) for h in hits],
    )


def _kb_render_mode(source: str) -> str:
    """按 source 扩展名推断知识库文档的渲染模式。

    知识库只保留文本分块（二进制原始字节在入库时已清除），因此渲染器集合
    比文件库少：``text`` / ``markdown`` / ``table`` / ``code`` / ``unsupported``。
    """
    ext = "." + ((source or "").rsplit(".", 1)[-1] if "." in (source or "") else "").lower()
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in {".csv", ".tsv"}:
        return "table"
    # 文本分块一律当 code 渲染等宽区；纯文本类仍走 text。
    pure_text = {".txt", ".text", ".log", ".rtf", ".ini", ".cfg", ".conf", ".env"}
    return "text" if ext in pure_text else "code"


@router.get("/{kb_id}/documents/{doc_id}/preview", response_model=KbPreviewResponse)
def preview_document(kb_id: str, doc_id: str):
    """预览知识库文档的原始全文（按分块拼回，截断到 ~50 KB）。

    文件类型的上传原始字节已不再保留（入库时经 ETL 切块后临时文件即被清除），
    因此预览内容 = 全部分块按入库顺序拼接；与上传原文等价（分块不丢字，只切分）。
    图片 / 扫描版 PDF 等解析出 0 段文本的文档会返回 ``previewable=False``。
    ``render`` 按 source 扩展名推断前端渲染器（text / markdown / table / code）。
    """
    _require_base(kb_id)
    catalog = get_catalog()
    doc = catalog.get_document(doc_id)
    if not doc or doc["kb_id"] != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    store = get_store()
    try:
        prev = store.preview_source(doc["source"], kb_id=kb_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"预览失败：{exc}") from exc

    if not prev.get("found"):
        return KbPreviewResponse(
            kb_id=kb_id, doc_id=doc_id, name=doc["name"],
            doc_type=doc.get("doc_type", "file"),
            source=doc["source"], previewable=False,
            text=None, chars=0, chunks=0,
            reason="该文档无可预览内容（可能是扫描版 PDF、图片或空文件）",
            render="unsupported",
        )
    if prev.get("unsupported"):
        return KbPreviewResponse(
            kb_id=kb_id, doc_id=doc_id, name=doc["name"],
            doc_type=doc.get("doc_type", "file"),
            source=doc["source"], previewable=False,
            text=None, chars=0, chunks=0,
            reason="Milvus 后端暂不支持文档预览",
            render="unsupported",
        )
    return KbPreviewResponse(
        kb_id=kb_id, doc_id=doc_id, name=doc["name"],
        doc_type=doc.get("doc_type", "file"), source=doc["source"],
        previewable=True, truncated=bool(prev.get("truncated")),
        text=prev.get("text"), chars=int(prev.get("chars", 0)),
        chunks=int(prev.get("chunks", 0)),
        render=_kb_render_mode(doc["source"]),
    )


# ── D58：嵌入失败自愈运维面 ──────────────────────────────────────────────────
# 路径用 /admin/ 前缀区分这些是管理操作（vs 普通 kb 操作在 /{kb_id}/ 下）。
# 这些端点调 KnowledgeStore 的 retry_embed / abandon_expired / list_embed_failed，
# Milvus 后端均返回 0/[]，无副作用。


@router.get("/{kb_id}/diagnostics")
def kb_diagnostics(kb_id: str):
    """嵌入质量诊断：总数 / 各状态 / embed_failed aging 直方图。"""
    _require_base(kb_id)
    return get_store().chunk_diagnostics(kb_id=kb_id)


@router.post("/admin/embed-failed/retry")
def admin_retry_embed(body: dict | None = None):
    """手动触发一次 embed_failed 重试跑批。

    body: { "kb_id": <optional>, "max_chunks": <optional int> }
    """
    body = body or {}
    kb_id = body.get("kb_id")
    max_chunks = body.get("max_chunks")
    if max_chunks is not None:
        max_chunks = max(1, int(max_chunks))
    return get_store().retry_embed(kb_id=kb_id, limit=max_chunks)


@router.post("/admin/embed-failed/abandon")
def admin_abandon_failed(body: dict | None = None):
    """手动放弃超过 TTL 的 embed_failed chunk。

    body: { "kb_id": <optional>, "ttl_s": <optional int, 默认 embed_failed_ttl_s> }
    """
    body = body or {}
    ttl_s = body.get("ttl_s")
    if ttl_s is not None:
        ttl_s = int(ttl_s)
    kb_id = body.get("kb_id")
    n = get_store().abandon_expired(ttl_s=ttl_s, kb_id=kb_id)
    return {"abandoned": n}


@router.get("/admin/embed-failed/list")
def admin_list_embed_failed(
    kb_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    """列出当前 embed_failed 的 chunk（限 source + text 片段 + failed_count + retried_at）。"""
    rows = get_store().list_embed_failed(kb_id=kb_id, limit=limit)
    return {"rows": rows, "count": len(rows)}


# ── D59：嵌入版本迁移运维面 ───────────────────────────────────────────────────
# 版本号是字符串标签（"v1"/"bge-v1.5"），rotate 只改进程内存态；进程重启恢复
# settings 默认。新 add 自动挂新标签，旧 chunk 通过 re-embed/migrate 接口升级。

from ...core.tools.knowledge_tool import set_emv_override, _resolve_emv


@router.get("/admin/embed-version")
def admin_get_embed_version(kb_id: str | None = None):
    """当前活跃 embed_model_version + 版本分布 + 旧版 stale count。"""
    store = get_store()
    stats = store.version_stats(kb_id=kb_id)
    return stats


@router.post("/admin/embed-version/rotate")
def admin_rotate_embed_version(body: dict | None = None):
    """切换当前活跃版本（不自动 re-embed，旧 chunk 由 migrate 接口升级）。

    body: { "new_version": "..." } — 必填。返回 { "current", "previous" }。
    """
    body = body or {}
    new_version = (body.get("new_version") or "").strip()
    if not new_version:
        raise HTTPException(status_code=400, detail="new_version required")
    previous = _resolve_emv()
    current = set_emv_override(new_version)
    return {"current": current, "previous": previous}


@router.post("/admin/chunks/{chunk_id}/re-embed")
def admin_reembed_chunk(chunk_id: int, body: dict | None = None):
    """单条 chunk 升级到当前版本。返回 { status_before, version_before, version_after, ok }。"""
    res = get_store().reembed_chunk(chunk_id)
    return res


@router.post("/admin/embed-version/migrate")
def admin_migrate_embed_version(body: dict | None = None):
    """批量把旧版本 chunk 升级到当前版本。

    body: { "kb_id": <optional>, "limit": <optional int> }
    """
    body = body or {}
    kb_id = body.get("kb_id")
    limit = body.get("limit")
    if limit is not None:
        limit = max(1, int(limit))
    return get_store().reembed_batch(kb_id=kb_id, limit=limit)
