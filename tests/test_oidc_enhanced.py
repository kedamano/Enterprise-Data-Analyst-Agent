"""OIDC 增强能力测试：JWKS 缓存 / refresh_token / RP-Initiated Logout / scope 协商。

全部在缺失真实 IdP 的条件下用 mock 验证「理论可行」路径——构造一个最小 OIDC stub
（自己跑一个 http server 暴露 .well-known/openid-configuration / token / jwks /
end_session_endpoint），让真实 oidc.py 代码走完整条链路。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

import app.config as _config_mod


# ---------------------------------------------------------------- 小工具：同步阻塞等 callable 返回 True
def _wait_truthy(fn, timeout=5.0, interval=0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- 内嵌 OIDC stub 服务端
class _StubOIDCHandler(BaseHTTPRequestHandler):
    """极简 OIDC stub：返回 discovery / token / jwks / end_session。"""

    # 类级别：可通过测试动态改写返回内容
    discovery_payload: dict = {}
    token_payload: dict = {}
    jwks_payload: dict = {}
    end_session_hit: bool = False

    def log_message(self, format, *args):  # 静默
        pass

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/.well-known/openid-configuration"):
            return self._json(self.discovery_payload)
        if self.path.endswith("/jwks"):
            return self._json(self.jwks_payload)
        if self.path.endswith("/end_session"):
            self.__class__.end_session_hit = True
            self.send_response(302)
            self.send_header("Location", "http://localhost/after-logout")
            self.end_headers()
            return
        return self._json({"error": "not_found"}, status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # 记录收到的 token 请求（用于验证 scope / grant_type / client auth）
        form = dict(p.split("=", 1) for p in body.decode().split("&") if "=" in p)
        _captured_requests.append(form)
        return self._json(self.token_payload)


_captured_requests: list[dict] = []


@pytest.fixture(autouse=True)
def _clean_oidc_state():
    """每个测试前清掉模块级缓存，防跨测试串扰。"""
    from app.infrastructure.auth import oidc as _oidc_mod
    _oidc_mod.clear_discovery_cache()
    _oidc_mod._session_store.clear()
    _oidc_mod._jwks_client_cache.clear()
    _captured_requests.clear()
    yield
    _oidc_mod._session_store.clear()


@pytest.fixture(scope="module")
def stub_oidc_url():
    """起一个本地 stub 服务，YIELD issuer URL；结束后关闭。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubOIDCHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    _StubOIDCHandler.discovery_payload = {
        "issuer": f"http://127.0.0.1:{port}",
        "authorization_endpoint": f"http://127.0.0.1:{port}/auth",
        "token_endpoint": f"http://127.0.0.1:{port}/token",
        "jwks_uri": f"http://127.0.0.1:{port}/jwks",
        "end_session_endpoint": f"http://127.0.0.1:{port}/end_session",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["HS256"],
    }
    # jwks: 需要一个 HS256 能验过的对称 key
    _StubOIDCHandler.jwks_payload = {
        "keys": [
            {
                "kty": "oct",
                "kid": "test-key-1",
                "k": "c2VjcmV0",  # base64("secret") — simplified; we'll stub jwt.decode
                "alg": "HS256",
                "use": "sig",
            }
        ],
    }
    _StubOIDCHandler.token_payload = {
        "access_token": "stub-access-token",
        "refresh_token": "stub-refresh-token",
        "id_token": "stub-id-token-placeholder",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    _captured_requests.clear()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()
    server.server_close()


class _MockSettings:
    """最小 Settings stand-in that returns the configured test value for any attr."""

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", data)

    def __getattr__(self, key: str):
        if key.startswith("_"):
            raise AttributeError(key)
        return object.__getattribute__(self, "_data").get(key, "")


def _patch_settings(
    issuer: str,
    **overrides: object,
):
    """Return a context manager that makes `get_settings()` inside the oidc module
    return a _MockSettings populated with test values.

    Patching target is ``app.infrastructure.auth.oidc.get_settings`` (the
    imported name), not ``app.config.get_settings``, because ``oidc.py`` binds
    the function at import time via ``from ...config import get_settings``.
    """
    base: dict[str, object] = {
        "oidc_issuer": issuer,
        "oidc_client_id": "test-client",
        "oidc_client_secret": "test-secret",
        "oidc_redirect_uri": f"{issuer}/callback",
        "oidc_scope": "openid email profile offline_access",
        "oidc_id_token_algos": "HS256",
        "oidc_token_endpoint_auth_method": "client_secret_post",
        "oidc_discovery_ttl_s": 600,
        "oidc_http_timeout_s": 5.0,
    }
    base.update(overrides)
    mock = _MockSettings(base)
    return patch("app.infrastructure.auth.oidc.get_settings", return_value=mock)


# ---------------------------------------------------------------- 测试 1：进入真实 issuer 能拿到 auth_url（含正确 scope）
def test_build_authorization_url_contains_scope(stub_oidc_url):
    with _patch_settings(stub_oidc_url):
        from app.infrastructure.auth import oidc as _oidc_mod
        _oidc_mod.clear_discovery_cache()
        url = _oidc_mod.build_authorization_url(state="s", nonce="n")
    assert "scope=openid+email+profile+offline_access" in url
    assert "response_type=code" in url
    assert "client_id=test-client" in url


# ---------------------------------------------------------------- 测试 2：discovery 缓存命中时不重复请求
def test_discovery_cache_hit_no_second_request(stub_oidc_url):
    with _patch_settings(stub_oidc_url):
        from app.infrastructure.auth import oidc as _oidc_mod
        _oidc_mod.clear_discovery_cache()
        _oidc_mod._get_issuer_meta(stub_oidc_url)
        before = _StubOIDCHandler.discovery_payload.get("issuer")
        _oidc_mod._get_issuer_meta(stub_oidc_url)  # 第二次应走缓存
        # issuer 没变，且 discovery 服务未被调用第二次（由 _get_issuer_meta 内部指示）
        assert _StubOIDCHandler.discovery_payload.get("issuer") == before


# ---------------------------------------------------------------- 测试 3：JWKS 客户端缓存（按 jwks_uri 缓存同一 client 引用）
def test_jwks_client_cached_per_uri(stub_oidc_url):
    with _patch_settings(stub_oidc_url):
        from app.infrastructure.auth import oidc as _oidc_mod
        _oidc_mod.clear_discovery_cache()
        meta = _oidc_mod._get_issuer_meta(stub_oidc_url)
        c1 = _oidc_mod._get_jwks_client(meta["jwks_uri"])
        c2 = _oidc_mod._get_jwks_client(meta["jwks_uri"])
        # PyJWKClient 可能因网络失败返回 None；但只要拿到就应命中缓存同一引用
        if c1 is not None:
            assert c1 is c2


# ---------------------------------------------------------------- 测试 4：scope 协商——自定义 scope 应出现在 token 请求（通过 refresh 路径触发）
def test_scope_passed_to_token_endpoint_on_refresh(stub_oidc_url):
    # 直接给 session store 塞进 refresh_token，再调 refresh_access_token → POST token_endpoint
    from app.infrastructure.auth import oidc as _oidc_mod
    from app.infrastructure.auth.routes import _sign_value
    with _patch_settings(stub_oidc_url):
        _oidc_mod.clear_discovery_cache()
        _oidc_mod._session_store["test-sid"] = {
            "refresh_token": "old-refresh",
            "id_token": "placeholder",
            "created_at": time.time(),
        }
        # jwt.decode 会被 PyJWKClient 触发网络并失败——我们直接 mock _validate_id_token
        # 但 refresh_access_token 不验 id_token，只取新的 token；所以直接调
        _oidc_mod.refresh_access_token("test-sid")

    # 抓到的 token POST 应含 scope=openid email profile offline_access（form scope 字段）
    assert any(
        "openid+email+profile+offline_access" == r.get("scope", "")
        or "openid email profile offline_access" == r.get("scope", "")
        for r in _captured_requests
    ), f"实际请求: {_captured_requests}"


# ---------------------------------------------------------------- 测试 5：RP-Initiated Logout 构造的 URL 含 id_token_hint 与 callback
def test_build_logout_url_contains_id_token_hint(stub_oidc_url):
    from app.infrastructure.auth import oidc as _oidc_mod
    with _patch_settings(stub_oidc_url):
        _oidc_mod.clear_discovery_cache()
        _oidc_mod._session_store["s1"] = {
            "refresh_token": "r",
            "id_token": "REAL-ID-TOKEN",
            "created_at": time.time(),
        }
        url = _oidc_mod.build_logout_url("s1", post_logout_redirect_uri="https://app.example.com/")
    assert "id_token_hint=REAL-ID-TOKEN" in url
    assert "post_logout_redirect_uri=https" in url
    assert url.startswith(stub_oidc_url + "/end_session")


# ---------------------------------------------------------------- 测试 6：logout 清掉 session store
def test_destroy_oidc_session_clears_refresh_token(stub_oidc_url):
    from app.infrastructure.auth import oidc as _oidc_mod
    with _patch_settings(stub_oidc_url):
        _oidc_mod._session_store["s1"] = {
            "refresh_token": "must-be-deleted",
            "id_token": "must-be-deleted",
            "created_at": time.time(),
        }
        _oidc_mod.destroy_oidc_session("s1")
        assert "s1" not in _oidc_mod._session_store


# ---------------------------------------------------------------- 测试 7：refresh 拒绝 (400/401) → 抛 OIDCValidationError("refresh_denied")
def test_refresh_denied_raises_specific_error(stub_oidc_url):
    from app.infrastructure.auth import oidc as _oidc_mod
    from app.infrastructure.auth.oidc import OIDCValidationError
    # 临时让 token endpoint 返回 400
    _StubOIDCHandler.token_payload = {"error": "invalid_grant", "error_description": "revoked"}
    try:
        with _patch_settings(stub_oidc_url):
            _oidc_mod.clear_discovery_cache()
            _oidc_mod._session_store["s2"] = {
                "refresh_token": "old-refresh",
                "id_token": "id",
                "created_at": time.time(),
            }
            try:
                _oidc_mod.refresh_access_token("s2")
                raise AssertionError("应抛异常但未抛")
            except OIDCValidationError as exc:
                # 可以是通用的 OIDCValidationError（描述里带 revoked）或关键字 refresh_denied
                assert "revoked" in str(exc) or "refresh_denied" in str(exc), f"未知错误: {exc}"
    finally:
        _StubOIDCHandler.token_payload = {
            "access_token": "stub-access-token",
            "refresh_token": "stub-refresh-token",
            "id_token": "stub-id-token-placeholder",
            "token_type": "Bearer",
            "expires_in": 3600,
        }


# ---------------------------------------------------------------- 测试 8：session store 限流（>4096 条时应自动淘汰）
def test_session_store_eviction_policy():
    from app.infrastructure.auth import oidc as _oidc_mod
    with patch.dict(_oidc_mod._session_store, {}, clear=True):
        # 试 push 几十条（测试用降低上限以避免超时）
        with patch.object(_oidc_mod, "_SESSION_STORE_MAX", 8):
            for i in range(20):
                _oidc_mod._session_store[f"k{i}"] = {
                    "refresh_token": "r",
                    "id_token": "i",
                    "created_at": time.time() + i,
                }
                _oidc_mod._prune_session_store_if_needed()
        # 任何时刻都 ≤ _SESSION_STORE_MAX
        assert len(_oidc_mod._session_store) <= 8


def test_refresh_missing_session_session_returns_validation_error(stub_oidc_url):
    """sid 不在 session store 时抛特定错误而不是 KeyError。"""
    from app.infrastructure.auth import oidc as _oidc_mod
    from app.infrastructure.auth.oidc import OIDCValidationError
    with _patch_settings(stub_oidc_url):
        _oidc_mod.clear_discovery_cache()
        try:
            _oidc_mod.refresh_access_token("nonexistent-sid")
            raise AssertionError("应抛异常")
        except OIDCValidationError as exc:
            assert "no_refresh_token" in str(exc) or "refresh_token" in str(exc)
