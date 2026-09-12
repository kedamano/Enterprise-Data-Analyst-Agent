"""文件 / 图片上传接口。

前端 Composer 支持拖拽、粘贴、点选三种方式上传；本模块负责：
1. 接收 multipart 文件
2. 按类型解析（表格 → 列名/行数/样例；文本 → 摘录；图片 → 元信息）
3. 绑定到 session_id，供后续 /chat/analyze 注入上下文
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...core.attachments import (
    DatasetPreview,
    build_preview,
    get_attachment_store,
    image_dir,
    is_image,
    _safe_filename,
)
from ...models.schemas import (
    AttachmentInfo,
    AttachmentListResponse,
    UploadResponse,
)

router = APIRouter(prefix="/attachments", tags=["attachments"])

# 单文件上限 20MB —— 演示环境足够，且避免内存被撑爆
MAX_BYTES = 20 * 1024 * 1024


def _to_info(p: DatasetPreview, kind_override: str | None = None) -> AttachmentInfo:
    return AttachmentInfo(
        name=p.name,
        kind=kind_override or p.kind,
        rows=p.rows or None,
        columns=p.columns or None,
        sample=p.sample[:5] if p.sample else None,
        excerpt=(p.excerpt[:500] or None) if p.kind == "text" else None,
        bytes=p.bytes,
        path=p.path or None,
    )


@router.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    session_id: str = Form(default="default"),
):
    """上传单个文件并绑定到 session。"""
    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限",
        )

    name = file.filename or "unnamed"
    if is_image(name):
        # P2-2：图片接入视觉解析 —— 落地原图到磁盘并记录 path，
        # 供 image_analyze 工具实际读取（不再只是"已登记但不可用"）。
        try:
            img_dir = image_dir(session_id)
            img_dir.mkdir(parents=True, exist_ok=True)
            img_path = img_dir / _safe_filename(name)
            img_path.write_bytes(raw)
            saved_path = str(img_path)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "图片落地失败 session=%s name=%s: %s", session_id, name, exc
            )
            saved_path = ""
        preview = DatasetPreview(
            name=name,
            kind="image",
            bytes=len(raw),
            path=saved_path,
            excerpt=f"[图片 {file.content_type or 'unknown'}，{len(raw)} 字节]",
        )
        store = get_attachment_store()
        store.put(session_id, preview)
        return UploadResponse(
            ok=True,
            session_id=session_id,
            attachment=_to_info(preview, kind_override="image"),
            hint="图片已落地，可用 image_analyze 工具让模型识别图中数据/图表/文字。",
        )

    preview = build_preview(raw, name)
    # ATTACH/01：表格类附件在 put() 内自动物化成边车库表，
    # 让 sql_query / dataset_profile 能直接寻址 upload.<表名>
    get_attachment_store().put(session_id, preview)

    hint = None
    if preview.kind == "table" and preview.rows == 0:
        hint = "已登记表结构，但未解析到数据行，请检查文件格式。"
    elif preview.kind == "text" and not preview.excerpt:
        hint = "未能提取文本内容（可能是扫描版 PDF 或加密文档）。"

    return UploadResponse(
        ok=True,
        session_id=session_id,
        attachment=_to_info(preview),
        hint=hint,
    )


@router.get("/list", response_model=AttachmentListResponse)
def list_attachments(session_id: str = "default"):
    store = get_attachment_store()
    items = store.get(session_id)
    return AttachmentListResponse(
        session_id=session_id,
        attachments=[_to_info(p, kind_override="image" if p.kind == "image" else None) for p in items.values()],
    )


@router.delete("/clear")
def clear_attachments(session_id: str = "default"):
    get_attachment_store().clear(session_id)
    return {"ok": True, "session_id": session_id}
