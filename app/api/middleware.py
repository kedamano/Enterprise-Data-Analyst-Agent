"""AUTH/01 鉴权中间件：一次收口，路由不必各自加依赖。

Spec: docs/specs/AUTH/01-user-auth-and-data-permissions.md §2

为什么用中间件而不是 FastAPI 依赖：`Principal` 要能被**工具层**读到（行/列/表权限在
`sql_tool` 里判），而依赖注入只把值给它自己的路由函数。中间件在请求上下文里
`set_current(principal)`，同步端点跑在线程池时 anyio 会复制上下文 → 工具层可见。

放行清单是**白名单**：只有明确列出的路径公开；其余 `/api/v1/**` 一律要 key。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..core.security import auth

# 公开路径（探活/文档/UI 壳与静态资源）
_PUBLIC_PREFIXES = ("/ui", "/docs", "/redoc", "/openapi.json", "/favicon.ico")
_PUBLIC_API = {"/api/v1/health", "/api/v1/health/llm"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_API or path.startswith(_PUBLIC_PREFIXES)


def _is_protected(path: str, api_prefix: str) -> bool:
    return path.startswith(api_prefix) and not _is_public(path)


async def auth_middleware(request: Request, call_next):
    from ..config import get_settings

    path = request.url.path
    settings = get_settings()

    # 默认关：匿名全权限（本地开发与既有用例零影响）
    if not auth.enabled():
        principal = auth.anonymous_principal()
        token = auth.set_current(principal)
        try:
            response = await call_next(request)
        finally:
            auth.reset_current(token)
        return response

    if not _is_protected(path, settings.api_prefix):
        return await call_next(request)

    # 开了鉴权却没配 key → 503（配置错误必须吵，绝不静默放开）
    try:
        keys = auth.parse_keys(settings.auth_keys or "")
    except ValueError as exc:
        auth.audit(None, path, "DENY", f"AUTH_KEYS 配置非法: {exc}")
        return JSONResponse({"detail": f"鉴权配置错误：{exc}"}, status_code=503)
    if not keys:
        auth.audit(None, path, "DENY", "AUTH_ENABLED=true 但未配置 AUTH_KEYS")
        return JSONResponse({"detail": "鉴权已启用但未配置任何 API Key（拒绝所有请求）"},
                            status_code=503)

    principal = auth.resolve_key(request.headers.get("X-API-Key"))
    if principal is None:
        # 不回显"这个 key 不存在"这类差异，防枚举
        auth.audit(None, path, "DENY", "缺失或无效的 API Key")
        return JSONResponse({"detail": "需要有效的 X-API-Key"}, status_code=401)

    if not auth.check_quota(principal):
        auth.audit(principal, path, "RATE_LIMIT", f"超过配额 {principal.quota_per_min}/min")
        return JSONResponse({"detail": "请求过于频繁（超出配额）"}, status_code=429)

    auth.audit(principal, path, "ALLOW")
    token = auth.set_current(principal)
    try:
        response = await call_next(request)
    finally:
        auth.reset_current(token)
    return response
