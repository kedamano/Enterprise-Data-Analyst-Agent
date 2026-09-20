"""AUTH/03 — OIDC Authorization Code Flow 客户端层。

env-gated / fail-open
--------------------
- 未配 ``OIDC_ISSUER`` / ``OIDC_CLIENT_ID`` → ``is_oidc_configured()`` 返回 False，
  ``build_authorization_url`` 抛 ``OIDCNotConfigured`` —— 不上网络、不挂请求。
- 网络错误时抛 ``OIDCDiscoveryError``；token 校验失败抛 ``OIDCValidationError`` ——
  **绝不上抛到主流程**，调用方应当 try/except 静默承接并退回既有登录。

依赖::
- AuthLib（OAuth2Session 换 token）
- PyJWT（校验 id_token）
- httpx（.well-known 发现）

全部 import 都在函数内；包未装时 **不破坏 import**，只在调用现场 raise ImportError 提示装包。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

from ...config import get_settings

logger = logging.getLogger("da.auth.oidc")


# ---------------------------------------------------------------- 异常金字塔
class OIDCError(Exception):
    """OIDC 链路底层异常。路由层应全部 catch 并 fall through 到既有登录流程。"""
    pass


class OIDCNotConfigured(OIDCError):
    """OIDC 关键配置缺失（issuer / client_id）。"""
    pass


class OIDCDiscoveryError(OIDCError):
    """.well-known/openid-configuration 拉不到或解析失败。"""
    pass


class OIDCValidationError(OIDCError):
    """id_token 签名 / nonce / aud / iss 任一校验失败。"""
    pass


# ---------------------------------------------------------------- 配置探测
def is_oidc_configured() -> bool:
    """是否配齐 OIDC 最小可用集（issuer + client_id）。"""
    s = get_settings()
    return bool(s.oidc_issuer and s.oidc_client_id)


# ---------------------------------------------------------------- Discovery 缓存
# 进程内简单 TTL 缓存：避免每次 /login 请求都去拉 .well-known（默认 1h）。
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _get_issuer_meta(issuer: str) -> dict[str, Any]:
    """拉 .well-known/openid-configuration，TTL 缓存。"""
    now = time.monotonic()
    ttl = max(60, int(get_settings().oidc_discovery_ttl_s or 3600))

    cached = _discovery_cache.get(issuer)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx 未安装，无法进行 OIDC discovery：pip install httpx") from exc

    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    timeout = float(get_settings().oidc_http_timeout_s or 10.0)

    try:
        resp = httpx.get(url, timeout=timeout, verify=True)
        resp.raise_for_status()
        meta = resp.json()
    except Exception as exc:
        raise OIDCDiscoveryError(f"无法获取 OIDC configuration ({url}): {exc}") from exc

    if not isinstance(meta, dict) or "authorization_endpoint" not in meta:
        raise OIDCDiscoveryError(f"OIDC configuration 缺 authorization_endpoint: {meta!r}")

    _discovery_cache[issuer] = (now, meta)
    return meta


def clear_discovery_cache() -> None:
    """测试用：清 discovery 缓存。"""
    _discovery_cache.clear()
    with _discovery_lock:
        _jwks_client_cache.clear()


# ---------------------------------------------------------------- JWKS 客户端缓存（按 issuer，带 TTL）
# 每次验签都新建 PyJWKClient 会重复拉取 JWKS，缓存连接与已拉取 key set 能显著
# 降低 IdP 负载并加快校验（authlib/jose 默认也会缓存，这里显式 hold 一个长引用）。
_discovery_lock = threading.Lock()
_jwks_client_cache: dict[str, Any] = {}
_JWKS_CLIENT_TTL_S = max(60, int(os.getenv("OIDC_JWKS_CACHE_TTL_S", str(3600))))


def _get_jwks_client(jwks_uri: str) -> Any:
    """按 jwks_uri 缓存一个 PyJWKClient；缓存 miss / 过期时新建。

    返回 None 表示 PyJWT 不可用（调用方应在 None 时走 fallback 快速失败）。
    """
    try:
        from jwt import PyJWKClient
    except ImportError:
        return None

    now = time.monotonic()
    cached = _jwks_client_cache.get(jwks_uri)
    if cached is not None:
        client, ts = cached
        if (now - ts) < _JWKS_CLIENT_TTL_S:
            return client

    client = PyJWKClient(jwks_uri, cache_keys=True)
    with _discovery_lock:
        _jwks_client_cache[jwks_uri] = (client, now)
    return client


# ---------------------------------------------------------------- Auth URL
def build_authorization_url(state: str, nonce: str) -> str:
    """构造 IdP 授权 URL。缺配置 → OIDCNotConfigured；网络不可达 → OIDCDiscoveryError。"""
    if not is_oidc_configured():
        raise OIDCNotConfigured("OIDC 未配置（缺 OIDC_ISSUER 或 OIDC_CLIENT_ID）")

    settings = get_settings()
    meta = _get_issuer_meta(settings.oidc_issuer)
    auth_endpoint = str(meta["authorization_endpoint"])

    redirect_uri = settings.oidc_redirect_uri or ""
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.oidc_scope or "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    # 某些 IdP 需要 prompt=login 强制重新登录（可选，咱不强制）
    return f"{auth_endpoint}?{urlencode(params)}"


# ---------------------------------------------------------------- Callback 处理
def handle_callback(code: str, expected_state: str, expected_nonce: str) -> dict[str, Any]:
    """用授权码换 token，校验 id_token，返回 {sub, email, name, id_token}。

    任何环节失败 → OIDCValidationError（或 OIDCDiscoveryError）。
    """
    if not is_oidc_configured():
        raise OIDCNotConfigured("OIDC 未配置")

    settings = get_settings()
    meta = _get_issuer_meta(settings.oidc_issuer)
    token_endpoint = meta.get("token_endpoint")
    if not token_endpoint:
        raise OIDCDiscoveryError("OIDC configuration 缺 token_endpoint")

    # ---- 换 token ----
    try:
        token_response = _exchange_code(
            token_url=str(token_endpoint),
            code=code,
            redirect_uri=settings.oidc_redirect_uri or "",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret or "",
        )
    except Exception as exc:
        raise OIDCValidationError(f"换 token 失败: {exc}") from exc

    id_token = token_response.get("id_token")
    if not id_token:
        raise OIDCValidationError("token 响应缺 id_token")

    # ---- 校验 id_token ----
    claims = _validate_id_token(
        id_token,
        expected_nonce=expected_nonce,
        expected_issuer=settings.oidc_issuer,
        expected_audience=settings.oidc_client_id,
    )

    return {
        "sub": str(claims.get("sub") or ""),
        "email": str(claims.get("email") or ""),
        "name": str(claims.get("name") or claims.get("preferred_username") or claims.get("sub") or ""),
        "id_token": id_token,
        # 保留 refresh_token 供后续 /refresh 端点用；路由层负责暂存到 session
        "refresh_token": str(token_response.get("refresh_token") or ""),
        # 保留令牌的过期时间，便于审计
        "exp": claims.get("exp"),
        "iss": claims.get("iss", settings.oidc_issuer),
    }


# ---------------------------------------------------------------- 内部：换 token
def _exchange_code(
    *,
    token_url: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """用 AuthLib 的 OAuth2Session 换 token；包不可用时退回纯 httpx 实现。"""
    try:
        from authlib.integrations.httpx_client import OAuth2Session
        client = OAuth2Session(client_id, client_secret=client_secret)
        token = client.fetch_token(
            token_url,
            code=code,
            redirect_uri=redirect_uri,
        )
        return dict(token)
    except ImportError:
        # 退回纯 httpx 实现（不引 AuthLib）
        import httpx
        resp = httpx.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json"},
            auth=(client_id, client_secret) if client_secret else None,
            timeout=float(get_settings().oidc_http_timeout_s or 10.0),
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------- 内部：校验 id_token
def _validate_id_token(
    token: str,
    *,
    expected_nonce: str,
    expected_issuer: str,
    expected_audience: str,
) -> dict[str, Any]:
    """校验 id_token 签名（JWKS）+ nonce + aud + iss。失败 → OIDCValidationError。"""
    try:
        import jwt
    except ImportError as exc:
        raise RuntimeError("PyJWT 未安装，无法校验 id_token：pip install PyJWT") from exc

    settings = get_settings()

    # 取 JWKS 端点
    try:
        meta = _get_issuer_meta(expected_issuer)
    except OIDCDiscoveryError:
        raise

    jwks_uri = meta.get("jwks_uri")
    if not jwks_uri:
        raise OIDCValidationError("OIDC configuration 缺 jwks_uri，无法校验 id_token 签名")

    try:
        jwks_client = _get_jwks_client(jwks_uri)
        if jwks_client is None:
            raise OIDCValidationError("PyJWT 不可用，无法构建 JWKS client")
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=settings.oidc_id_token_algos.split(","),
            audience=expected_audience,
            issuer=expected_issuer,
            options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except OIDCValidationError:
        raise
    except Exception as exc:
        raise OIDCValidationError(f"id_token 校验失败（签名/iss/aud/exp）: {exc}") from exc

    # nonce 必须匹配（对应 /login 时塞的 nonce）
    token_nonce = claims.get("nonce")
    if expected_nonce and token_nonce and token_nonce != expected_nonce:
        raise OIDCValidationError(
            f"nonce mismatch: cookie={expected_nonce!r} id_token={token_nonce!r}"
        )

    return claims


# ---------------------------------------------------------------- 用户 upsert 辅助
def _sanitize_for_username(s: str) -> str:
    """把 url 安全的化成 username 允许的字符集 [A-Za-z0-9_.-]。"""
    import re
    # 去掉 scheme:// 前缀，然后把剩下所有非法字符压成 .
    stripped = re.sub(r"^https?://", "", s.strip("/"))
    safe = re.sub(r"[^A-Za-z0-9_.-]", ".", stripped)
    return safe[:40]


def upsert_oidc_user(sub: str, iss: str, email: str, name: str) -> dict[str, Any]:
    """按 (sub, issuer) 唯一键 upsert users 表，返回已落库的用户 dict。

    - 已存在（同 sub+iss）→ 更新 email/name，touch_login
    - 不存在 → 新建（source="oidc"，随机密码占位）

    建号用 store 的 create_user，密码占位用 secrets.token_urlsafe —— OIDC 来的用户
    不会用密码登录，这个哈希只是满足表 NOT NULL 约束。
    """
    import secrets as _secrets

    from ...core.security import users as users_core

    store = users_core.get_store()
    iss_safe = _sanitize_for_username(iss)
    username = f"oidc.{iss_safe}.{sub}"[:60]
    existing = _find_user_by_oidc(sub, iss)

    if existing:
        updated = store.update_profile(
            existing["id"], display_name=name or existing.get("display_name") or "",
            email=email or existing.get("email") or "",
        )
        store.touch_login(existing["id"])
        return updated

    password_placeholder = _secrets.token_urlsafe(48)
    return store.create_user(
        username,
        password_placeholder,
        email=email,
        display_name=name or sub,
        source="oidc",
        wechat_openid="",         # OIDC 与微信通道独立
        wechat_unionid="",
    )


def _find_user_by_oidc(sub: str, iss: str) -> Optional[dict[str, Any]]:
    """按 (sub, iss) 找用户。实现：username = oidc:{sanitized_iss}:{sub} 查 users 表。"""
    import threading as _threading

    from ...core.security import users as users_core

    store = users_core.get_store()
    iss_safe = _sanitize_for_username(iss)
    username = f"oidc.{iss_safe}.{sub}"[:60]
    lock = _threading.RLock()
    # 查 users 表的 `source='oidc' AND username=?` 直接命中
    with lock, store._connect() as c:
        row = c.execute(
            "SELECT * FROM users WHERE username=? AND source='oidc' LIMIT 1",
            (username,),
        ).fetchone()
    return store._row_to_public(row) if row else None


# ---------------------------------------------------------------- 服务端会话（refresh_token / id_token 暂存）
# 进程内、有容量上限的 LRU-ish map：OIDC 回调后把 refresh_token / id_token 挂在
# session_id 上，供 /refresh / /logout 后续取出。多副本部署应换 Redis/DB。
_session_store: dict[str, dict[str, Any]] = {}
_SESSION_STORE_MAX = 4096
_SESSION_LOCK = threading.Lock()


def _session_set(session_id: str, *, refresh_token: str = "", id_token: str = "") -> None:
    with _SESSION_LOCK:
        _session_store[session_id] = {
            "refresh_token": refresh_token,
            "id_token": id_token,
            "created_at": time.time(),
        }
    # 超限时裁剪（锁外执行，避免持锁过长）
    _prune_session_store_if_needed()


def _session_get(session_id: str) -> dict[str, Any] | None:
    return _session_store.get(session_id)


def _session_pop(session_id: str) -> dict[str, Any] | None:
    return _session_store.pop(session_id, None)


def _prune_session_store_if_needed() -> None:
    """超过上限时按 created_at 淘汰最老的 25%（由 _session_set 调用）。"""
    with _SESSION_LOCK:
        if len(_session_store) <= _SESSION_STORE_MAX:
            return
        sorted_keys = sorted(_session_store, key=lambda k: _session_store[k]["created_at"])
        for k in sorted_keys[: max(1, _SESSION_STORE_MAX // 4)]:
            _session_store.pop(k, None)


# ---------------------------------------------------------------- Refresh Token
def refresh_access_token(session_id: str) -> dict[str, Any]:
    """用 refresh_token 换新的 access_token。

    取服务暂存的 refresh_token → POST token_endpoint with grant_type=refresh_token
     → 返回 {access_token, expires_in, refresh_token?, id_token?}。

    缺 session_id 暂存 → OIDCValidationError；网络/校验失败 → OIDCValidationError；
    用户被 IdP 撤权（refresh 被拒）→ OIDCValidationError("refresh_denied")。
    """
    if not is_oidc_configured():
        raise OIDCNotConfigured("OIDC 未配置")
    session = _session_get(session_id)
    if not session or not session.get("refresh_token"):
        raise OIDCValidationError("no_refresh_token", "该会话无有效 refresh_token，需重新登录")

    settings = get_settings()
    meta = _get_issuer_meta(settings.oidc_issuer)
    token_endpoint = meta.get("token_endpoint")
    if not token_endpoint:
        raise OIDCDiscoveryError("缺 token_endpoint")

    body = {
        "grant_type": "refresh_token",
        "refresh_token": session["refresh_token"],
        "scope": settings.oidc_scope or "openid email profile",
    }
    # client auth: 优先 client_secret_post，fallback 到 basic
    headers = {"Accept": "application/json"}
    auth = None
    if settings.oidc_token_endpoint_auth_method == "client_secret_basic":
        auth = (settings.oidc_client_id, settings.oidc_client_secret or "")
    else:
        body["client_id"] = settings.oidc_client_id
        if settings.oidc_client_secret:
            body["client_secret"] = settings.oidc_client_secret

    try:
        import httpx
        resp = httpx.post(
            str(token_endpoint),
            data=body,
            headers=headers,
            auth=auth,
            timeout=float(settings.oidc_http_timeout_s or 10.0),
            verify=True,
        )
        resp.raise_for_status()
        new_tok = resp.json()
    except Exception as exc:
        raise OIDCValidationError(f"refresh 请求失败: {exc}") from exc

    if new_tok.get("error"):
        raise OIDCValidationError(f"refresh_denied: {new_tok.get('error_description', new_tok['error'])}")

    # 轮换暂存：IdP 通常会轮转 refresh_token（否则原 token 继续有效）
    with _SESSION_LOCK:
        cur = _session_store.get(session_id)
        if cur:
            if new_tok.get("refresh_token"):
                cur["refresh_token"] = new_tok["refresh_token"]
            if new_tok.get("id_token"):
                cur["id_token"] = new_tok["id_token"]
    return new_tok


# ---------------------------------------------------------------- RP-Initiated Logout
def build_logout_url(session_id: str, *, post_logout_redirect_uri: str = "") -> str:
    """构造 IdP 登出 URL（RP-Initiated Logout / Front-Channel）。

    需要 IdP 暴露 end_session_endpoint（discovery 返回）；取暂存的 id_token 作为
    id_token_hint 提高 IdP 侧登出命中率。缺暂存时仍返回基础 URL（无 hint）。
    """
    if not is_oidc_configured():
        raise OIDCNotConfigured("OIDC 未配置")
    settings = get_settings()
    meta = _get_issuer_meta(settings.oidc_issuer)
    end_session = meta.get("end_session_endpoint")
    if not end_session:
        raise OIDCValidationError("no_end_session_endpoint", "IdP 未暴露 end_session_endpoint")

    params: dict[str, str] = {
        "client_id": settings.oidc_client_id,
    }
    session = _session_get(session_id)
    if session and session.get("id_token"):
        params["id_token_hint"] = session["id_token"]
    if post_logout_redirect_uri:
        params["post_logout_redirect_uri"] = post_logout_redirect_uri
    return f"{end_session}?{urlencode(params)}"


def destroy_oidc_session(session_id: str) -> None:
    """服务端清掉该 session 暂存的 refresh_token / id_token。IdP 登出回调后调用。"""
    _session_pop(session_id)
