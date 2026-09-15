"""AUTH/01+02 鉴权中间件：一次收口，路由不必各自加依赖。

Spec: docs/specs/AUTH/01-user-auth-and-data-permissions.md §2

为什么用中间件而不是 FastAPI 依赖：`Principal` 要能被**工具层**读到（行/列/表权限在
`sql_tool` 里判），而依赖注入只把值给它自己的路由函数。中间件在请求上下文里
`set_current(principal)`，同步端点跑在线程池时 anyio 会复制上下文 → 工具层可见。

身份来源有两个，**都收敛成同一个 `Principal`**（所以数据权限与工具 RBAC 只有一份实现）：
  1. `Authorization: Bearer <token>` —— AUTH/02 的用户会话（可吊销）
  2. `X-API-Key`                      —— AUTH/01 的静态 key（机器对机器）

优先级：Bearer 优先。两者都无效 → 401（不回显"是哪种凭据错的"，防枚举）。

`AUTH_ENABLED=false`（默认）时是匿名全权限。**但用户体系不受这个开关影响**——
`/auth/*` 一律放行，否则本地开发想注册个账号试试都进不去。
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..core.security import auth

# 公开路径（探活/文档/UI 壳与静态资源）
_PUBLIC_PREFIXES = ("/ui", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
                    "/logo.png", "/apple-touch-icon.png")
# 头像按**文件名**取（文件名含随机串），是公开静态资源，不是"按用户 id 查资料"
_PUBLIC_PREFIX_AUTH = ("/api/v1/auth/avatar/",)

# 公开端点：登录相关接口必须免鉴权，否则没人能登进来
# （管理类端点 /auth/users 不在此列，仍走下面的强制鉴权 + 路由内 require_admin）
_PUBLIC_API = {
    "/api/v1/health", "/api/v1/health/llm",
    "/api/v1/auth/config", "/api/v1/auth/roles",
    "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/logout",
    "/api/v1/auth/me", "/api/v1/auth/me/password", "/api/v1/auth/me/avatar",
    "/api/v1/auth/me/sessions", "/api/v1/auth/me/logins",
    "/api/v1/auth/wechat/qrcode", "/api/v1/auth/wechat/poll",
    "/api/v1/auth/wechat/callback", "/api/v1/auth/wechat/simulate",
}


def _is_public(path: str) -> bool:
    return (path in _PUBLIC_API
            or path.startswith(_PUBLIC_PREFIXES)
            or path.startswith(_PUBLIC_PREFIX_AUTH))


def _is_protected(path: str, api_prefix: str) -> bool:
    return path.startswith(api_prefix) and not _is_public(path)


def _bearer(request: Request) -> str:
    raw = request.headers.get("Authorization", "") or ""
    return raw[7:].strip() if raw[:7].lower() == "bearer " else ""


def _principal_from_bearer(request: Request):
    """Bearer → 用户 → Principal。任何异常都吞掉（鉴权失败不该 500）。"""
    tok = _bearer(request)
    if not tok:
        return None
    try:
        from ..core.security import users as users_core

        user = users_core.get_store().resolve_token(tok)
        return users_core.user_to_principal(user) if user else None
    except Exception:
        return None


async def auth_middleware(request: Request, call_next):
    from ..config import get_settings

    path = request.url.path
    settings = get_settings()

    # 默认关：匿名全权限（本地开发与既有用例零影响）
    # 注意：这里**仍然尝试**解析 Bearer——否则登录了也拿不到自己的身份。
    if not auth.enabled():
        principal = _principal_from_bearer(request) or auth.anonymous_principal()
        token = auth.set_current(principal)
        try:
            response = await call_next(request)
        finally:
            auth.reset_current(token)
        return response

    if not _is_protected(path, settings.api_prefix):
        return await call_next(request)

    # 先试用户登录态
    principal = _principal_from_bearer(request)

    if principal is None:
        # 再试静态 API Key
        try:
            keys = auth.parse_keys(settings.auth_keys or "")
        except ValueError as exc:
            auth.audit(None, path, "DENY", f"AUTH_KEYS 配置非法: {exc}")
            return JSONResponse({"detail": f"鉴权配置错误：{exc}"}, status_code=503)

        if not keys and not getattr(settings, "user_auth_enabled", False):
            # 开了鉴权、既没有 key 也没开用户体系 → 配置错误必须吵，绝不静默放开
            auth.audit(None, path, "DENY",
                       "AUTH_ENABLED=true 但既未配置 AUTH_KEYS 也未启用用户体系")
            return JSONResponse(
                {"detail": "鉴权已启用但没有任何可用身份来源（需配置 AUTH_KEYS 或启用用户体系）"},
                status_code=503,
            )
        principal = auth.resolve_key(request.headers.get("X-API-Key"))

    if principal is None:
        # 不回显"这个 key/token 不存在"这类差异，防枚举
        auth.audit(None, path, "DENY", "缺失或无效的凭据")
        return JSONResponse(
            {"detail": "需要登录或提供有效的 X-API-Key"}, status_code=401
        )

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
