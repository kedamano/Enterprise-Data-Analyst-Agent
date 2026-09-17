"""AUTH/02 账号路由：注册 / 登录 / 资料 / 头像 / 角色 / 微信扫码。

路由层的职责边界（别把逻辑写进来）
--------------------------------
- 校验与业务规则在 `core/security/users.py`（可单测、不依赖 HTTP）。
- 这里只做三件事：**取身份**、**转错误**、**控权限**。
- 身份来源有两个，都通向同一个 `Principal`：
  1. `Authorization: Bearer <token>` —— 人（本模块签发，可吊销）
  2. `X-API-Key`                     —— 机器（AUTH/01 的静态配置）
  两者在 `middleware.py` 里统一收敛，所以工具级 RBAC 与数据权限一行都不用改。

为什么 `/auth/me` 不返回 401：前端每次启动都要问"我是谁"，用 200 +
`authenticated=false` 表达未登录，比让它区分 401/网络错误更省事，控制台也干净。
真正需要身份的写操作（改资料/改密/管理用户）仍由依赖显式返回 401/403。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from ...config import get_settings
from ...core.security import auth as auth_core
from ...core.security import users as users_core
from ...core.security import wechat as wechat_core
from ...core.security.users import UserError
from ...models.schemas import (
    AdminUpdateUserRequest,
    AuthConfigResponse,
    AuthMeResponse,
    AuthSessionResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    RoleListResponse,
    SessionListResponse,
    UpdateProfileRequest,
    UserListResponse,
    UserPublic,
    WeChatPollResponse,
    WeChatQrResponse,
    WeChatSimulateRequest,
    WeChatStatus,
)

logger = logging.getLogger("da.auth.routes")

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# 身份依赖
# --------------------------------------------------------------------------- #
def bearer_token(request: Request) -> str:
    """从 Authorization 头取 Bearer token（大小写不敏感）。"""
    raw = request.headers.get("Authorization", "") or ""
    if raw[:7].lower() == "bearer ":
        return raw[7:].strip()
    return ""


def current_user_optional(request: Request) -> dict | None:
    """有有效令牌 → 用户；否则 None。**不抛异常**。"""
    tok = bearer_token(request)
    if not tok:
        return None
    try:
        return users_core.get_store().resolve_token(tok)
    except Exception:
        logger.exception("解析登录令牌失败")
        return None


def require_user(user: dict | None = Depends(current_user_optional)) -> dict:
    if not user:
        raise HTTPException(status_code=401, detail="需要登录后才能执行此操作")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _http(exc: UserError) -> HTTPException:
    return HTTPException(status_code=exc.status_code,
                         detail={"code": exc.code, "message": exc.message})


def _http_wechat(exc: wechat_core.WeChatError) -> HTTPException:
    return HTTPException(status_code=exc.status_code,
                         detail={"code": exc.code, "message": exc.message})


def avatar_url(user: dict) -> str:
    """头像可访问地址。外链（微信头像）原样返回；本地上传拼本站路由 + 版本号破缓存。"""
    name = str(user.get("avatar") or "")
    if not name:
        return ""
    if name.startswith("http://") or name.startswith("https://"):
        return name
    return f"/api/v1/auth/avatar/{name}?v={user.get('avatar_version', 0)}"


def _public(user: dict) -> UserPublic:
    data = {k: user.get(k) for k in UserPublic.model_fields}
    data["avatar"] = avatar_url(user)
    return UserPublic(**data)


def _session_payload(user: dict, sess: dict, *, created: bool = False) -> AuthSessionResponse:
    return AuthSessionResponse(
        token=sess["token"], expires_at=sess.get("expires_at", ""),
        user=_public(user), created=created,
    )


# --------------------------------------------------------------------------- #
# 能力探测（前端决定"显示什么、能不能点"）
# --------------------------------------------------------------------------- #
@router.get("/config", response_model=AuthConfigResponse)
def auth_config():
    settings = get_settings()
    store = users_core.get_store()
    wx = wechat_core.get_login().status()
    return AuthConfigResponse(
        user_auth_enabled=bool(settings.user_auth_enabled),
        enforcement=auth_core.enabled(),
        registration_open=bool(settings.user_registration_open),
        password_min_length=int(settings.password_min_length),
        has_users=store.count() > 0,
        wechat=WeChatStatus(**wx),
    )


@router.get("/roles", response_model=RoleListResponse)
def roles():
    """角色 → 权限矩阵（设置页的「权限划分」表直接渲染）。"""
    return RoleListResponse(roles=users_core.role_matrix())


# --------------------------------------------------------------------------- #
# 注册 / 登录 / 登出
# --------------------------------------------------------------------------- #
@router.post("/register", response_model=AuthSessionResponse)
def register(req: RegisterRequest, request: Request):
    settings = get_settings()
    store = users_core.get_store()

    if not settings.user_auth_enabled:
        raise HTTPException(status_code=503,
                            detail="用户账号体系已关闭（USER_AUTH_ENABLED=false）")
    # 允许首用户注册，否则新部署永远进不去
    if not settings.user_registration_open and store.count() > 0:
        raise HTTPException(status_code=403,
                            detail="本服务已关闭公开注册，请联系管理员开通账号")
    try:
        user = store.create_user(
            req.username, req.password,
            email=req.email, display_name=req.display_name,
        )
    except UserError as exc:
        raise _http(exc) from exc

    sess = store.issue_token(user["id"], user_agent=request.headers.get("user-agent", ""),
                             ip=_client_ip(request))
    store.touch_login(user["id"])
    auth_core.audit(users_core.user_to_principal(user), "/auth/register", "ALLOW",
                    "新用户注册")
    return _session_payload(user, sess, created=True)


@router.post("/login", response_model=AuthSessionResponse)
def login(req: LoginRequest, request: Request):
    store = users_core.get_store()
    try:
        user = store.authenticate(req.username, req.password, ip=_client_ip(request))
    except UserError as exc:
        auth_core.audit(None, "/auth/login", "DENY", f"{exc.code}: {exc.message}")
        raise _http(exc) from exc

    sess = store.issue_token(user["id"], user_agent=request.headers.get("user-agent", ""),
                             ip=_client_ip(request))
    store.touch_login(user["id"])
    auth_core.audit(users_core.user_to_principal(user), "/auth/login", "ALLOW")
    return _session_payload(user, sess)


@router.post("/logout")
def logout(request: Request, user: dict | None = Depends(current_user_optional)):
    """吊销当前令牌。已登出/无令牌也返回 ok（登出必须幂等）。"""
    tok = bearer_token(request)
    revoked = users_core.get_store().revoke_token(tok) if tok else False
    if user:
        auth_core.audit(users_core.user_to_principal(user), "/auth/logout", "ALLOW")
    return {"ok": True, "revoked": revoked}


# --------------------------------------------------------------------------- #
# 我的资料
# --------------------------------------------------------------------------- #
@router.get("/me", response_model=AuthMeResponse)
def me(user: dict | None = Depends(current_user_optional)):
    settings = get_settings()
    return AuthMeResponse(
        authenticated=user is not None,
        user=_public(user) if user else None,
        enforcement=auth_core.enabled(),
        user_auth_enabled=bool(settings.user_auth_enabled),
    )


@router.patch("/me", response_model=UserPublic)
def update_me(req: UpdateProfileRequest, user: dict = Depends(require_user)):
    try:
        updated = users_core.get_store().update_profile(
            user["id"],
            display_name=req.display_name,
            email=req.email,
            phone=req.phone,
            bio=req.bio,
        )
    except UserError as exc:
        raise _http(exc) from exc
    return _public(updated)


@router.post("/me/password")
def change_password(req: ChangePasswordRequest, request: Request,
                    user: dict = Depends(require_user)):
    """改密并踢掉**其他**设备的会话（当前设备保留，否则用户立刻掉线）。"""
    store = users_core.get_store()
    try:
        revoked = store.change_password(
            user["id"], req.old_password, req.new_password,
            keep_token=bearer_token(request),
        )
    except UserError as exc:
        auth_core.audit(users_core.user_to_principal(user), "/auth/me/password",
                        "DENY", exc.code)
        raise _http(exc) from exc
    auth_core.audit(users_core.user_to_principal(user), "/auth/me/password", "ALLOW",
                    f"撤销其他会话 {revoked} 个")
    return {"ok": True, "revoked_sessions": revoked}


@router.get("/me/sessions", response_model=SessionListResponse)
def my_sessions(user: dict = Depends(require_user)):
    return SessionListResponse(sessions=users_core.get_store().list_sessions(user["id"]))


@router.get("/me/logins")
def my_login_events(user: dict = Depends(require_user),
                    limit: int = Query(10, ge=1, le=50)):
    """最近登录流水（含失败）——用户自查"谁动过我的号"的唯一入口。"""
    return {"events": users_core.get_store().login_event_summary(user["id"], limit)}


# --------------------------------------------------------------------------- #
# 头像
# --------------------------------------------------------------------------- #
@router.post("/me/avatar", response_model=UserPublic)
async def upload_avatar(file: UploadFile = File(...), user: dict = Depends(require_user)):
    data = await file.read()
    suffix = ""
    if file.filename and "." in file.filename:
        suffix = "." + file.filename.rsplit(".", 1)[1].lower()
    try:
        updated = users_core.get_store().set_avatar_bytes(user["id"], data, suffix)
    except UserError as exc:
        raise _http(exc) from exc
    return _public(updated)


@router.delete("/me/avatar", response_model=UserPublic)
def remove_avatar(user: dict = Depends(require_user)):
    store = users_core.get_store()
    prev = (store.get(user["id"]) or {}).get("avatar", "")
    updated = store.update_profile(user["id"], avatar="")
    store._remove_avatar_file(prev)  # noqa: SLF001 —— 同模块内的清理动作
    return _public(updated)


@router.get("/avatar/{name}")
def serve_avatar(name: str):
    """按**文件名**取头像。

    刻意不按 user_id 取：文件名校验后只许落在 `data/avatars/` 内，
    这样既不泄露用户 id，也没有路径穿越面。
    """
    path = users_core.get_store().avatar_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="头像不存在")
    ext = path.suffix.lower()
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(ext, "application/octet-stream")
    # 头像内容不可变（文件名带随机串），可长缓存
    return Response(
        content=path.read_bytes(), media_type=media,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# --------------------------------------------------------------------------- #
# 用户管理（管理员）
# --------------------------------------------------------------------------- #
@router.get("/users", response_model=UserListResponse)
def list_users(q: str = Query(""), role: str = Query(""), status: str = Query(""),
               limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
               _admin: dict = Depends(require_admin)):
    store = users_core.get_store()
    rows = store.list_users(q=q, role=role, status=status, limit=limit, offset=offset)
    return UserListResponse(users=[_public(r) for r in rows], total=store.count())


@router.patch("/users/{user_id}", response_model=UserPublic)
def admin_update_user(user_id: str, req: AdminUpdateUserRequest,
                      admin: dict = Depends(require_admin)):
    store = users_core.get_store()
    try:
        if req.role is not None:
            updated = store.set_role(user_id, req.role, actor_id=admin["id"])
            # 降级即时生效：旧角色权限不该等到下次登录
            auth_core.audit(users_core.user_to_principal(admin),
                            f"/auth/users/{user_id}/role", "ALLOW",
                            f"{user_id} → {req.role}")
        if req.status is not None:
            updated = store.set_status(user_id, req.status, actor_id=admin["id"])
            auth_core.audit(users_core.user_to_principal(admin),
                            f"/auth/users/{user_id}/status", "ALLOW",
                            f"{user_id} → {req.status}")
    except UserError as exc:
        raise _http(exc) from exc
    if req.role is None and req.status is None:
        raise HTTPException(status_code=400, detail="未提供任何可修改字段")
    return _public(updated)


@router.delete("/users/{user_id}")
def admin_delete_user(user_id: str, admin: dict = Depends(require_admin)):
    try:
        users_core.get_store().delete_user(user_id, actor_id=admin["id"])
    except UserError as exc:
        raise _http(exc) from exc
    auth_core.audit(users_core.user_to_principal(admin),
                    f"/auth/users/{user_id}", "ALLOW", "管理员删除用户")
    return {"ok": True, "deleted": user_id}


# --------------------------------------------------------------------------- #
# 微信扫码登录
# --------------------------------------------------------------------------- #
@router.get("/wechat/qrcode", response_model=WeChatQrResponse)
def wechat_qrcode():
    """新建一次扫码会话。未配置凭据时 `configured=false`，`qr_url` 为空。"""
    try:
        return WeChatQrResponse(**wechat_core.get_login().start())
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成微信二维码失败")
        raise HTTPException(status_code=500, detail=f"生成二维码失败：{exc}") from exc


@router.get("/wechat/poll", response_model=WeChatPollResponse)
def wechat_poll(state: str = Query(..., min_length=8)):
    return WeChatPollResponse(**wechat_core.get_login().poll(state))


@router.post("/wechat/simulate", response_model=WeChatPollResponse)
def wechat_simulate(req: WeChatSimulateRequest):
    """**仅开发联调**：把扫码会话直接置成功。三重闸门见 wechat.simulate()。"""
    login = wechat_core.get_login()
    try:
        result = login.simulate(req.state, nickname=req.nickname)
    except wechat_core.WeChatError as exc:
        raise _http_wechat(exc) from exc
    user = result["user"]
    auth_core.audit(users_core.user_to_principal(user), "/auth/wechat/simulate",
                    "ALLOW", "模拟扫码（非真实微信授权）")
    return WeChatPollResponse(
        status=wechat_core.STATE_CONFIRMED, simulated=True,
        token=login.poll(req.state).get("token", ""),
        user_id=user["id"],
    )


@router.get("/wechat/callback", response_class=HTMLResponse)
def wechat_callback(code: str = Query(""), state: str = Query("")):
    """微信授权后的落点（在**用户手机的微信浏览器**里打开）。

    这个页面拿不到桌面页面的上下文，所以它只做一件事：把登录结果写进库；
    桌面页面通过 `/wechat/poll` 取回。返回极简 HTML，不引任何外部资源。
    """
    try:
        result = wechat_core.get_login().handle_callback(code, state)
    except wechat_core.WeChatError as exc:
        return HTMLResponse(wechat_core.callback_html(False, exc.message),
                            status_code=200)  # 状态码给 200，让人看到可读页面
    except Exception as exc:  # noqa: BLE001
        logger.exception("微信回调处理失败")
        return HTMLResponse(wechat_core.callback_html(False, f"登录失败：{exc}"))
    name = result["user"].get("display_name") or result["user"].get("username")
    return HTMLResponse(wechat_core.callback_html(True, f"{name}，登录成功"))
