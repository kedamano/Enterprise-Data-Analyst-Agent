"""AUTH/03 — OIDC API 路由。

三个端点：
  - ``GET /api/v1/auth/oidc/config``  → 前端探测 OIDC 是否可用
  - ``GET /api/v1/auth/oidc/login``   → 302 到 IdP 授权 URL（写 state/nonce cookie）
  - ``GET /api/v1/auth/oidc/callback`` → IdP 授权后回调：换 token → upsert user → 返 HTML 自动闭窗

**全部 env-gated**：OIDC 未配时 config 仍 200（返回 {configured:false}）；
login 与 callback 返回 501。
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...config import get_settings
from .oidc import (
    OIDCNotConfigured,
    OIDCDiscoveryError,
    OIDCValidationError,
    build_authorization_url,
    handle_callback as oidc_handle_callback,
    is_oidc_configured,
    upsert_oidc_user,
)

logger = logging.getLogger("da.auth.oidc_routes")

router = APIRouter(prefix="/auth/oidc", tags=["auth-oidc"])

_COOKIE_STATE = "oidc_state"
_COOKIE_NONCE = "oidc_nonce"


def _cookie_secret() -> str:
    """取 signed cookie 密钥。没配就按进程启动时间随机——开发够用。"""
    s = get_settings()
    if s.oidc_cookie_secret:
        return s.oidc_cookie_secret
    # 进程级单例即可（重启服务端 state 失效，开发环境容忍）
    if not hasattr(_cookie_secret, "_v"):
        import secrets as _s
        _cookie_secret._v = _s.token_hex(24)
    return _cookie_secret._v  # type: ignore[return-value]


def _sign_value(value: str) -> str:
    """简易 HMAC 签名（itsdangerous 不可用时 fallback）。"""
    import hashlib
    import hmac
    key = _cookie_secret().encode("utf-8")
    sig = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"{value}.{sig}"


def _unsign_value(signed: str) -> Optional[str]:
    if not signed or "." not in signed:
        return None
    value, sig = signed.rsplit(".", 1)
    import hashlib
    import hmac
    key = _cookie_secret().encode("utf-8")
    expected = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return value if hmac.compare_digest(expected, sig) else None


# ---------------------------------------------------------------- 能力探测
@router.get("/config")
def oidc_config():
    """前端用：决定"使用 OIDC 登录"按钮显隐。OIDC 未配也 200。"""
    configured = is_oidc_configured()
    issuer = get_settings().oidc_issuer if configured else None
    return {"configured": configured, "issuer": issuer}


# ---------------------------------------------------------------- 登录跳转
@router.get("/login")
def oidc_login(request: Request):
    """302 重定向到 IdP 授权 URL。OIDC 未配 → 501。"""
    if not is_oidc_configured():
        return JSONResponse(
            {"detail": "OIDC 未配置"},
            status_code=501,
        )

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)

    try:
        auth_url = build_authorization_url(state=state, nonce=nonce)
    except OIDCNotConfigured as exc:
        logger.warning("OIDC login 但未配置: %s", exc)
        return JSONResponse({"detail": "OIDC 未配置"}, status_code=501)
    except OIDCDiscoveryError as exc:
        logger.warning("OIDC discovery 失败: %s", exc)
        return JSONResponse(
            {"detail": f"无法连通 IdP (discovery 失败): {exc}"},
            status_code=502,
        )

    resp = RedirectResponse(auth_url, status_code=302)
    # state / nonce 写 signed cookie，callback 时取回校验
    ttl = max(60, int(get_settings().oidc_state_ttl_s or 600))
    resp.set_cookie(
        _COOKIE_STATE, _sign_value(state),
        max_age=ttl, httponly=True, samesite="lax",
        secure=True,
    )
    resp.set_cookie(
        _COOKIE_NONCE, _sign_value(nonce),
        max_age=ttl, httponly=True, samesite="lax",
        secure=True,
    )
    return resp


# ---------------------------------------------------------------- IdP 回调
@router.get("/callback")
def oidc_callback(
    request: Request,
    code: str = Query(""),
    state: str = Query(""),
):
    """IdP 授权码回调：换 token → upsert user → 返 HTML 自动闭窗（或 JSON token）。

    未配 OIDC → 501；state cookie 缺失 → 400。
    """
    if not is_oidc_configured():
        return JSONResponse(
            {"detail": "OIDC 未配置"},
            status_code=501,
        )

    # ---- 取 state / nonce cookie ----
    state_cookie = _unsign_value(request.cookies.get(_COOKIE_STATE) or "")
    nonce_cookie = _unsign_value(request.cookies.get(_COOKIE_NONCE) or "")

    if not state_cookie:
        logger.warning("OIDC callback 缺 state cookie（或过期）")
        return HTMLResponse(
            _CLOSE_HTML("登录失败：state 已过期，请重新点击 OIDC 登录"),
            status_code=400,
        )

    if state_cookie != state:
        logger.warning("OIDC callback state 不匹配（可能 CSRF）")
        return HTMLResponse(
            _CLOSE_HTML("登录失败：state 不匹配"),
            status_code=400,
        )

    if not nonce_cookie:
        logger.warning("OIDC callback 缺 nonce cookie")
        return HTMLResponse(
            _CLOSE_HTML("登录失败：nonce 丢失"),
            status_code=400,
        )

    # ---- 换 token + 校验 ----
    try:
        profile = oidc_handle_callback(code, expected_state=state, expected_nonce=nonce_cookie)
    except (OIDCValidationError, OIDCDiscoveryError) as exc:
        logger.warning("OIDC handle_callback 失败: %s", exc)
        return HTMLResponse(
            _CLOSE_HTML(f"登录失败：{exc}"),
            status_code=400,
        )

    # ---- upsert user ----
    user = upsert_oidc_user(
        sub=profile["sub"], iss=profile["iss"],
        email=profile["email"], name=profile["name"],
    )

    # ---- 发内部 Bearer token ----
    store = __import__("app.core.security.users", fromlist=["get_store"]).get_store()
    sess = store.issue_token(user["id"], user_agent=request.headers.get("user-agent", "")[:300])

    # ---- 返 HTML（自动闭窗）或 JSON ----
    accepts = request.headers.get("accept", "")
    if "application/json" in accepts:
        resp = JSONResponse({"token": sess["token"], "user_id": user["id"]})
    else:
        resp = HTMLResponse(_SUCCESS_HTML(sess["token"]), status_code=200)

    # 清掉 state cookie
    resp.delete_cookie(_COOKIE_STATE)
    resp.delete_cookie(_COOKIE_NONCE)
    return resp


def _SUCCESS_HTML(token: str) -> str:
    """极简 HTML：把 token 写到 localStorage 后关窗。主体无外部资源引用。"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>登录成功</title></head>
<body style="font-family:system-ui;margin:2rem;text-align:center;background:#f7f8fa;color:#101828">
<h3>登录成功，正在关窗…</h3>
<script>
  try {{
    sessionStorage.setItem('oidc_token', {token!r});
    window.opener && window.opener.dispatchEvent(new Event('oidc:login'));
  }} catch (e) {{}}
  setTimeout(function(){{ window.close(); }}, 600);
</script>
<p style="color:#667085;font-size:13px">如未自动关窗，请手动关闭此页。</p>
</body></html>"""


def _CLOSE_HTML(message: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>登录失败</title></head>
<body style="font-family:system-ui;margin:2rem;text-align:center;background:#f7f8fa;color:#b42318">
<h3>登录失败</h3>
<p>{message}</p>
<p style="color:#667085;font-size:13px">请关闭此页，回到原窗口重试。</p>
</body></html>"""
