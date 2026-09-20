"""FastAPI entry point for the Enterprise Data Analyst Agent.

Mounts health / chat / documents routers and wires CORS + logging. Run with::

    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .api.routes import (
    analytics,
    attachments,
    auth,
    budget,
    caliber,
    chat,
    datasources,
    debug,
    document,
    export,
    feedback,
    files,
    health,
    jobs,
    knowledge,
    mcp,
    mcp_servers,
    security,
    skills,
    ui,
)
from .config import get_settings
from .infrastructure.observability.tracing import setup_logging
from .infrastructure.auth.routes import router as oidc_router

logger = logging.getLogger(__name__)

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

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动钩子：后台预热知识库嵌入模型。

    嵌入模型首次加载本机实测 ~17s（离线缓存优先，缓存缺失才在线下载）。放到后台
    daemon 线程里付掉这笔一次性代价，用户第一次「入库 / 检索」就不必干等。

    两点注意：
    * 预热失败不影响服务——混合检索的 BM25 通道可独立工作。
    * **超时 ≠ 永久失败**：加载线程会继续在后台跑，后续调用自动接管结果
      （见 ``knowledge_tool._get_embed_model``）。早期版本把超时写成永久禁用，
      会导致整个进程此后静默退化为纯 BM25，必须重启才能恢复。
    """
    if settings.knowledge_enabled:
        import threading

        from .core.tools import knowledge_tool

        threading.Thread(target=knowledge_tool.warm_up_embedder, daemon=True).start()

    # Workflow Jobs：init DB 路径 + 启动调度
    if settings.workflow_jobs_enabled:
        from .infrastructure.jobs import get_scheduler, init_db_path as _init_jobs_db

        _init_jobs_db(str(pathlib.Path("data") / "jobs" / "jobs.db"))
        pathlib.Path("data").mkdir(exist_ok=True)
        try:
            scheduler = get_scheduler()
            scheduler.start()
        except Exception as exc:
            logger.warning("Job scheduler init skipped: %s", exc)
    yield

    # teardown：flush LangFuse 遥测 → 停调度
    try:
        from .infrastructure.observability.langfuse import flush as _lf_flush
        _lf_flush()
    except Exception:
        pass
    try:
        from .infrastructure.jobs import get_scheduler as _get_shutdown_scheduler
        _get_shutdown_scheduler().shutdown()
    except Exception:
        pass


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)
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

    body = _metrics_mod.metrics.render_prometheus()
    # D60：多跳检索计数器（multihop.py 模块级快照）
    try:
        from .core.rag.multihop import multihop_metrics as _mh_metrics
        snap = _mh_metrics()
        if snap:
            body += "\n# TYPE rag_multi_hop_splits_total counter\n"
            body += f"rag_multi_hop_splits_total {snap.get('rag_multi_hop_splits_total', 0.0)}\n"
    except Exception:
        pass
    # D61：query 改写计数器（rewrite.py 模块级快照）
    try:
        from .core.rag.rewrite import rewrite_metrics as _rw_metrics
        snap = _rw_metrics()
        if snap:
            for _k in ("rag_query_rewrite_total",
                        "rag_query_rewrite_synonym_hits_total",
                        "rag_query_rewrite_fallback_total"):
                body += f"\n# TYPE {_k} counter\n"
                body += f"{_k} {snap.get(_k, 0.0)}\n"
    except Exception:
        pass
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

app.include_router(health.router, prefix=settings.api_prefix)
app.include_router(datasources.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(document.router, prefix=settings.api_prefix)
app.include_router(debug.router, prefix=settings.api_prefix)
app.include_router(attachments.router, prefix=settings.api_prefix)
app.include_router(knowledge.router, prefix=settings.api_prefix)
app.include_router(files.router, prefix=settings.api_prefix)
app.include_router(export.router, prefix=settings.api_prefix)
app.include_router(caliber.router, prefix=settings.api_prefix)
app.include_router(mcp.router, prefix=settings.api_prefix)
app.include_router(mcp_servers.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)
app.include_router(security.router, prefix=settings.api_prefix)
app.include_router(budget.router, prefix=settings.api_prefix)
app.include_router(analytics.router, prefix=settings.api_prefix)
app.include_router(feedback.router, prefix=settings.api_prefix)
if settings.workflow_jobs_enabled:
    app.include_router(jobs.router, prefix=settings.api_prefix)
# AUTH/03：OIDC 路由（env-gated；未配置时 /oidc/config 仍 200，/oidc/login 与 /oidc/callback 返回 501）
app.include_router(oidc_router, prefix=settings.api_prefix)
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


# ---------------------------------------------------------------------------- #
# HTTPS / mTLS 入口（env-gated）
# ---------------------------------------------------------------------------- #
_HTTPS_ENABLED = pathlib.Path(settings.ssl_cert_file).exists() and pathlib.Path(settings.ssl_key_file).exists()


if __name__ == "__main__":
    import uvicorn

    if _HTTPS_ENABLED:
        ssl_ca = settings.ssl_client_ca if pathlib.Path(settings.ssl_client_ca).exists() else None
        logger.info(
            "HTTPS 入口已启用 → https://localhost:%s (mTLS=%s)",
            settings.ssl_https_port, ssl_ca is not None,
        )
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=settings.ssl_https_port,
            ssl_certfile=str(pathlib.Path(settings.ssl_cert_file)),
            ssl_keyfile=str(pathlib.Path(settings.ssl_key_file)),
            ssl_ca_certs=ssl_ca,
            log_level=settings.log_level.lower(),
        )
    else:
        uvicorn.run(
            "app.main:app",
            host="0.0.0.0",
            port=settings.ssl_http_port,
            log_level=settings.log_level.lower(),
        )


# 应用图标：浏览器 favicon + 侧栏/顶栏品牌 Logo（web/public 下同源单图）。
#
# 为什么逐个注册而不是 ``app.mount("/", StaticFiles(...))``：根路径整体挂载会
# 吞掉 /api、/ui、/docs 等路由。favicon 的引用是**绝对路径**（``/favicon.ico``），
# 而 dist 只被挂到 /ui 那一个 HTML 上，所以必须在这里显式暴露根级文件，
# 否则浏览器取不到标签页图标、页面 Logo 也会 404。
def _register_dist_file(route: str, filename: str, media_type: str) -> None:
    def _serve():
        p = _DIST / filename
        if p.exists():
            return FileResponse(str(p), media_type=media_type)
        return _missing()

    _serve.__name__ = f"_dist_{filename.replace('.', '_')}"
    app.get(route, include_in_schema=False)(_serve)


for _route, _filename, _mime in (
    ("/favicon.ico", "favicon.ico", "image/x-icon"),
    ("/favicon-32.png", "favicon-32.png", "image/png"),
    ("/apple-touch-icon.png", "apple-touch-icon.png", "image/png"),
    ("/logo.png", "logo.png", "image/png"),
):
    _register_dist_file(_route, _filename, _mime)
