"""FastAPI entry point for the Enterprise Data Analyst Agent.

Mounts health / chat / documents routers and wires CORS + logging. Run with::

    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import pathlib

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .api.routes import attachments, chat, debug, document, export, health, ui
from .config import get_settings
from .infrastructure.observability.tracing import setup_logging

settings = get_settings()
setup_logging(settings.log_level)
pathlib.Path("data").mkdir(exist_ok=True)

# #3 可观测性：进程内指标（Prometheus 文本格式）→ /metrics 端点 + 计时中间件
from .infrastructure.observability import metrics as _metrics_mod

# AUTH/01：生产环境却没开鉴权 = 对外零鉴权（任何能访问端口的人都能查全部数据、
# 下载任意会话导出）。这是企业级部署最易踩的坑，必须启动时**吵出来**。
if not settings.auth_enabled and settings.environment not in ("development", "dev", "local", "test"):
    import logging as _logging
    _logging.getLogger("da.security").critical(
        "SECURITY: AUTH_ENABLED=false 但 environment=%s —— API 对外零鉴权！"
        "生产务必设置 AUTH_ENABLED=true 并配置 AUTH_KEYS。", settings.environment
    )

_DIST = pathlib.Path(__file__).resolve().parent.parent / "web" / "dist"
_INDEX = _DIST / "index.html"


def _missing() -> HTMLResponse:
    """前端产物缺失时的占位响应。

    注意：不能对不存在的文件直接 FileResponse —— starlette 会抛异常变成 500。
    """
    return HTMLResponse(
        "<h3>前端资源缺失</h3><p>请先执行 <code>cd web &amp;&amp; npm run build</code>。</p>",
        status_code=404,
    )

app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# AUTH/01：鉴权中间件（默认关；开启后除探活/文档外全部要 X-API-Key）
from .api.middleware import auth_middleware  # noqa: E402

app.middleware("http")(auth_middleware)


# #3 可观测性：请求计时 + 计数（Prometheus 文本格式可由 /metrics 抓取）
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    import time as _t

    t0 = _t.time()
    response = await call_next(request)
    dur = _t.time() - t0
    _metrics_mod.observe_request(request.method, request.url.path, response.status_code, dur)
    # 运维事件：鉴权拒绝 / 限流 / 配置错误
    if response.status_code in (401, 429, 503):
        _metrics_mod.record_event(f"http_{response.status_code}")
    return response


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    """Prometheus 抓取端点（文本 exposition 格式，与 prometheus_client 同构）。"""
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(_metrics_mod.metrics.render_prometheus(), media_type="text/plain; version=0.0.4")

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(document.router, prefix=settings.api_prefix)
app.include_router(debug.router, prefix=settings.api_prefix)
app.include_router(attachments.router, prefix=settings.api_prefix)
app.include_router(export.router, prefix=settings.api_prefix)
app.include_router(ui.router)

# 托管前端构建产物（web/dist）的静态资源
if _DIST.exists():
    # 1) 编译后的 JS/CSS（Vite 放在 assets/ 下）
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")


@app.get("/")
def root():
    return RedirectResponse(url="/ui")


# 暴露根级静态资源（favicon.svg / icons.svg / verify.html）
@app.get("/favicon.svg", include_in_schema=False)
def _favicon():
    p = _DIST / "favicon.svg"
    if p.exists():
        return FileResponse(str(p), media_type="image/svg+xml")
    return _missing()


@app.get("/icons.svg", include_in_schema=False)
def _icons():
    p = _DIST / "icons.svg"
    if p.exists():
        return FileResponse(str(p), media_type="image/svg+xml")
    return _missing()


@app.get("/verify.html", include_in_schema=False)
def _verify():
    """修复验证页：渲染真实 SSE 数据，对比修复前后。"""
    p = _DIST / "verify.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html")
    return _missing()


@app.get("/demo.html", include_in_schema=False)
def _demo():
    """完整 UI 演示页：用真实 SSE 数据渲染三栏 + 6 阶段时间线 + 报告 markdown。"""
    p = _DIST / "demo.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html")
    return _missing()


@app.get("/seed.html", include_in_schema=False)
def _seed():
    """注入演示对话到 localStorage，然后跳转到 /ui（便于无人工截图真实 SPA）。"""
    p = _DIST / "seed.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html")
    return _missing()


@app.get("/upload-check.html", include_in_schema=False)
def _upload_check():
    """上传能力自测页（拖拽 / 粘贴 / 文件选择 / accept 白名单）。"""
    p = _DIST / "upload-check.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html")
    return _missing()
