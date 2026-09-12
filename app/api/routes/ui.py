"""Visual console – serves the built Aceternity UI (web/dist)."""
from __future__ import annotations

import pathlib

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(tags=["ui"])

_DIST = pathlib.Path(__file__).resolve().parents[3] / "web" / "dist"
_INDEX = _DIST / "index.html"

_BUILD_HINT = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>前端未构建</title>
<style>body{font-family:ui-sans-serif,system-ui,"Microsoft YaHei",sans-serif;background:#0a0a0a;color:#e5e5e5;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:560px;padding:32px;border:1px solid #262626;border-radius:16px;background:#111}
code{background:#1f1f1f;padding:2px 6px;border-radius:4px;color:#fbbf24}</style></head>
<body><div class="box"><h2>前端产物缺失</h2>
<p>未找到 <code>web/dist/index.html</code>。请先构建前端：</p>
<p><code>cd web &amp;&amp; npm run build</code></p>
<p style="color:#a3a3a3;font-size:13px">构建完成后刷新本页即可。</p></div></body></html>"""


@router.get("/ui", include_in_schema=False)
def ui():
    # 注意：不能对不存在的文件直接 FileResponse —— 会抛异常变成 500。
    if not _INDEX.exists():
        return HTMLResponse(_BUILD_HINT, status_code=503)
    return FileResponse(str(_INDEX), media_type="text/html")
