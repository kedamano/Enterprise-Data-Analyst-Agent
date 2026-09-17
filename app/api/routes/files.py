"""文件库（企业文件管理）接口：左树右列表那一套。

端点分工：
* ``GET /files/tree``     左侧目录树（一次拉全量，前端本地展开/折叠）
* ``GET /files/list``     右侧当前目录内容 + 面包屑
* ``GET /files/search``   全库按名搜索，结果带完整路径（跨目录定位）
* ``POST /files/folder``  新建文件夹
* ``POST /files/upload``  上传（同目录同名 = 覆盖）
* ``PATCH /files/node/{id}``  重命名（文件与文件夹同一入口）
* ``DELETE /files/node/{id}`` 删除（目录级联）
* ``GET /files/download/{id}`` 下载原始字节
* ``GET /files/preview/{id}`` 预览文本类文件原始内容（自动识别编码，大小截断）

错误语义统一走 400 + 人话 detail（重名、类型不符这类是用户可修正的问题，
不是服务端故障），只有节点不存在才 404。
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from ...core.filestore import get_filestore
from ...models.schemas import (
    FsDeleteResponse,
    FsFolderRequest,
    FsListResponse,
    FsNode,
    FsPreviewResponse,
    FsRenameRequest,
    FsSearchResponse,
    FsTreeResponse,
)

router = APIRouter(prefix="/files", tags=["files"])

MAX_BYTES = 50 * 1024 * 1024

# 文本类文件的 MIME 前缀与扩展名——这类文件可预览原始内容。
# 不在列表里的（PDF / DOCX / XLSX / 图片 / 压缩包等）会返回不可预览提示，
# 前端据此禁用预览按钮或改用其他方式（下载）。
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml",
                       "application/javascript", "application/xhtml+xml",
                       "application/csv", "application/sql")
_TEXT_EXTENSIONS = {
    ".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log", ".env",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".less", ".sass",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql",
    ".r", ".rb", ".php", ".java", ".kt", ".kts", ".scala", ".go", ".rs",
    ".swift", ".m", ".mm", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx",
    ".lua", ".perl", ".pl", ".vim", ".el", ".clj", ".cljs", ".edn",
    ".ex", ".exs", ".erl", ".hrl", ".hs", ".ml", ".mli",
    ".dockerfile", ".makefile", ".cmake", ".gradle", ".sbt",
    ".vue", ".svelte", ".astro",
    ".proto", ".thrift", ".avsc",
    ".diff", ".patch", ".http",
    ".rtf",
}

# 预览内容上限：200 KB 文本。超过则截断并在响应里标记 truncated。
PREVIEW_MAX_BYTES = 200 * 1024


@router.get("/tree", response_model=FsTreeResponse)
def tree():
    fs = get_filestore()
    return FsTreeResponse(nodes=[FsNode(**n) for n in fs.list_all()], stats=fs.stats())


@router.get("/list", response_model=FsListResponse)
def list_dir(parent_id: str = ""):
    fs = get_filestore()
    pid = parent_id or ""
    if pid and not fs.get(pid):
        raise HTTPException(status_code=404, detail="目录不存在")
    return FsListResponse(
        parent_id=pid,
        breadcrumb=[FsNode(**n) for n in fs.breadcrumb(pid)],
        nodes=[FsNode(**n) for n in fs.list_children(pid)],
    )


@router.get("/search", response_model=FsSearchResponse)
def search(q: str = Query(..., min_length=1)):
    return FsSearchResponse(
        query=q, results=[FsNode(**n) for n in get_filestore().search(q)]
    )


@router.post("/folder", response_model=FsNode)
def create_folder(req: FsFolderRequest):
    try:
        node = get_filestore().create_folder(req.parent_id, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FsNode(**node)


@router.post("/upload", response_model=FsNode)
async def upload(parent_id: str = Form(default=""), file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限"
        )
    try:
        node = get_filestore().save_file(
            parent_id, file.filename or "unnamed", raw, file.content_type or ""
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FsNode(**node)


@router.patch("/node/{node_id}", response_model=FsNode)
def rename(node_id: str, req: FsRenameRequest):
    try:
        node = get_filestore().rename(node_id, req.name)
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return FsNode(**node)


@router.delete("/node/{node_id}", response_model=FsDeleteResponse)
def delete(node_id: str):
    deleted = get_filestore().delete(node_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="节点不存在")
    return FsDeleteResponse(deleted=deleted)


@router.get("/download/{node_id}")
def download(node_id: str):
    fs = get_filestore()
    node = fs.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="文件不存在")
    if node["is_dir"]:
        raise HTTPException(status_code=400, detail="文件夹不支持下载")
    path = fs.blob_path(node_id)
    if path is None:
        raise HTTPException(status_code=404, detail="文件内容缺失")
    # FileResponse 会按 RFC 5987 编码中文文件名，中文名下载不会乱码
    return FileResponse(
        path, filename=node["name"],
        media_type=node["mime"] or "application/octet-stream",
    )


def _is_previewable(name: str, mime: str) -> bool:
    """判断文件是否可生成文本预览（按 MIME 前缀 + 扩展名双重检测）。"""
    m = (mime or "").lower()
    if any(m.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    ext = "." + (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    return ext in _TEXT_EXTENSIONS


#: 图像类 MIME 前缀 / 扩展名——走前端 <img> 内嵌渲染。
_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".tiff", ".tif", ".avif", ".heic", ".heap",  # 后者是占位
}
#: PDF
_PDF_EXTS = {".pdf"}
#: 表格类
_TABLE_EXTS = {".csv", ".tsv"}
#: Markdown
_MD_EXTS = {".md", ".markdown"}
#: 代码 / 纯文本类扩展的后缀集合（用于 render 模式判别）。
# _TEXT_EXTENSIONS 已含全部文本扩展，需要从中按用途再分一次。


def _render_mode(name: str, mime: str) -> str:
    """按文件名 / MIME 决定前端渲染模式。

    返回值与 ``FsPreviewResponse.render`` 对齐：
    ``image`` / ``pdf`` / ``table`` / ``markdown`` / ``code`` / ``text`` / ``unsupported``。
    """
    m = (mime or "").lower()
    ext = "." + (name.rsplit(".", 1)[-1] if "." in name else "").lower()

    if m.startswith("image/") or ext in _IMAGE_EXTS:
        return "image"
    if m == "application/pdf" or ext in _PDF_EXTS:
        return "pdf"
    if ext in _TABLE_EXTS or m in ("text/csv", "application/csv", "text/tab-separated-values"):
        return "table"
    if ext in _MD_EXTS:
        return "markdown"
    if _is_previewable(name, mime):
        # 其余文本文件优先当"代码"渲染（保留等宽/<pre>）；但已知纯文本的仍走 text。
        pure_text = {".txt", ".text", ".log", ".rtf", ".ini", ".cfg", ".conf",
                     ".env", ".gitignore", ".dockerignore", ".editorconfig",
                     ".prettierrc", ".eslintrc", ".browserslistrc"}
        if ext in pure_text:
            return "text"
        return "code"
    return "unsupported"


@router.get("/preview/{node_id}", response_model=FsPreviewResponse)
def preview(node_id: str):
    """预览文件。

    根据 MIME / 扩展名返回不同渲染模式：

    * 文本 / Markdown / 表格 / 代码：返回 ``previewable=True`` + 全文（truncated 标截断），
      ``render`` 指明具体渲染器（``text`` / ``markdown`` / ``table`` / ``code``）。
    * 图片 / PDF：返回 ``previewable=True`` + ``render=image|pdf``，text
      为 ``None``——前端改用 ``/files/raw/{id}`` 内嵌原始字节。
    * 其他二进制（zip/exe/docx/xlsx …）与以前一致，``previewable=False`` + reason。

    编码按 UTF-8 → GBK → Latin-1 回退，中文老文件不会乱码；
    超 ``PREVIEW_MAX_BYTES`` 截断并标 ``truncated``。
    """
    fs = get_filestore()
    node = fs.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="文件不存在")
    if node["is_dir"]:
        raise HTTPException(status_code=400, detail="文件夹不可预览")

    mode = _render_mode(node["name"], node["mime"])

    # 图像 / PDF 走原始流内嵌，无需转文本——直接返回渲染模式即可。
    if mode in ("image", "pdf"):
        return FsPreviewResponse(
            id=node_id, name=node["name"], mime=node["mime"],
            previewable=True, truncated=False, text=None, chars=0,
            encoding=None, reason=None,
            render=mode,
        )

    # 不可预览：保留原有的 reason 提示。
    if mode == "unsupported":
        return FsPreviewResponse(
            id=node_id, name=node["name"], mime=node["mime"],
            previewable=False, truncated=False, text=None, chars=0,
            encoding=None,
            reason="该文件类型不支持预览（可下载后查看）",
            render="unsupported",
        )

    # 文本类：读字节 → 解码。
    blob = fs.blob_path(node_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="文件内容缺失")

    raw = blob.read_bytes()
    truncated = len(raw) > PREVIEW_MAX_BYTES
    if truncated:
        raw = raw[:PREVIEW_MAX_BYTES]

    text, encoding = _decode_text(raw)
    return FsPreviewResponse(
        id=node_id, name=node["name"], mime=node["mime"],
        previewable=True, truncated=truncated,
        text=text, chars=len(text),
        encoding=encoding,
        render=mode,
    )


@router.get("/raw/{node_id}")
def raw(node_id: str):
    """把文件原始字节流吐出来——前端拿来内嵌渲染（img / pdf / iframe 都行）。

    与 ``/download/{id}`` 的差别：**不带** ``Content-Disposition: attachment``，
    浏览器会按 MIME inline 渲染（图片直接显示、PDF 内嵌查看器弹出），
    而不是走「另存为」弹窗。
    """
    fs = get_filestore()
    node = fs.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="文件不存在")
    if node["is_dir"]:
        raise HTTPException(status_code=400, detail="文件夹不可预览")
    path = fs.blob_path(node_id)
    if path is None:
        raise HTTPException(status_code=404, detail="文件内容缺失")
    # 注意：不传 filename= 就不会带 Content-Disposition: attachment，
    # 浏览器按 media_type inline 处理。
    return FileResponse(
        path,
        media_type=node["mime"] or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


def _decode_text(raw: bytes) -> tuple[str, str]:
    """按 UTF-8 → GBK → Latin-1 回退解码字节，返回 (text, encoding)。

    Latin-1 永远不会失败（每个字节都有映射），作为最终兜底保证不会 500。
    UTF-8 BOM 会被 startswith 捕获——utf-8-sig 编码自动剥 BOM。
    """
    if raw.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"
