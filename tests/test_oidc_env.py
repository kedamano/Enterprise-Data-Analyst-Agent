"""AUTH/03 OIDC environment-gated / fail-open behaviour tests.

6 cases (no real IdP needed):
  1. No env -> is_oidc_configured() is False
  2. No env -> build_authorization_url raises OIDCNotConfigured
  3. Env set + mocked discovery -> auth URL produced without network
  4. Env set + unreachable issuer -> OIDCDiscoveryError
  5. routes: /oidc/login & /oidc/callback return 501 when not configured
  6. users upsert by (sub, issuer) is idempotent (repeat upsert does not dup)

Plus a test_generate_certs case that exercises the cryptography fallback.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    """Stomp env vars so each test starts from a known state."""
    for k in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
              "OIDC_REDIRECT_URI", "OIDC_COOKIE_SECRET",
              "SSL_CERT_FILE", "SSL_KEY_FILE", "SSL_CLIENT_CA",
              "USER_DB_PATH"):
        monkeypatch.delenv(k, raising=False)


def _fresh_settings(monkeypatch, **overrides):
    """Reload app.config after setting env overrides."""
    for key in list(sys.modules):
        if key.startswith("app"):
            del sys.modules[key]
    for k, v in overrides.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))
    from app.config import get_settings
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------- Case 1 + 2
class TestOidcNotConfigured:
    def test_is_oidc_configured_false_when_no_env(self, monkeypatch):
        s = _fresh_settings(monkeypatch)
        from app.infrastructure.auth.oidc import is_oidc_configured
        assert is_oidc_configured() is False
        assert s.oidc_enabled is False

    def test_build_authorization_url_raises_not_configured(self, monkeypatch):
        _fresh_settings(monkeypatch)
        from app.infrastructure.auth.oidc import (
            OIDCNotConfigured,
            build_authorization_url,
            clear_discovery_cache,
        )
        clear_discovery_cache()
        with pytest.raises(OIDCNotConfigured):
            build_authorization_url("state-abc", "nonce-xyz")


# ---------------------------------------------------------------- Case 3: mocked discovery
class TestOidcWithMockedDiscovery:
    def test_build_url_with_mocked_well_known(self, monkeypatch):
        """Env set + patch _get_issuer_meta -> auth URL produced without network."""
        _fresh_settings(
            monkeypatch,
            OIDC_ISSUER="https://mock-idp.example.com",
            OIDC_CLIENT_ID="test-client",
            OIDC_CLIENT_SECRET="test-secret",
        )

        import app.infrastructure.auth.oidc as oidc_mod

        if hasattr(oidc_mod, "clear_discovery_cache"):
            oidc_mod.clear_discovery_cache()

        fake_meta = {
            "authorization_endpoint": "https://mock-idp.example.com/oauth2/authorize",
            "token_endpoint": "https://mock-idp.example.com/oauth2/token",
            "jwks_uri": "https://mock-idp.example.com/.well-known/jwks",
        }
        monkeypatch.setattr(oidc_mod, "_get_issuer_meta", lambda issuer: fake_meta)

        auth_url = oidc_mod.build_authorization_url("my-state", "my-nonce")
        assert "https://mock-idp.example.com/oauth2/authorize" in auth_url
        assert "response_type=code" in auth_url
        assert "client_id=test-client" in auth_url
        assert "state=my-state" in auth_url
        assert "nonce=my-nonce" in auth_url


# ---------------------------------------------------------------- Case 4: discovery unreachable
class TestOidcDiscoveryError:
    def test_build_url_raises_discovery_error_when_issuer_unreachable(self, monkeypatch):
        _fresh_settings(
            monkeypatch,
            OIDC_ISSUER="https://unreachable-idp.example.invalid",
            OIDC_CLIENT_ID="cid",
            OIDC_CLIENT_SECRET="",
        )
        import app.infrastructure.auth.oidc as oidc_mod

        if hasattr(oidc_mod, "clear_discovery_cache"):
            oidc_mod.clear_discovery_cache()

        with pytest.raises(oidc_mod.OIDCDiscoveryError):
            oidc_mod.build_authorization_url("s", "n")


# ---------------------------------------------------------------- Case 5: routes 501
class TestOidcRoutesShape:
    def test_login_returns_501_when_not_configured(self, monkeypatch):
        _fresh_settings(monkeypatch)
        for key in list(sys.modules):
            if key.startswith("app"):
                del sys.modules[key]
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/v1/auth/oidc/login")
        assert r.status_code == 501
        assert r.json() == {"detail": "OIDC 未配置"}

    def test_callback_returns_501_when_not_configured(self, monkeypatch):
        _fresh_settings(monkeypatch)
        for key in list(sys.modules):
            if key.startswith("app"):
                del sys.modules[key]
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/v1/auth/oidc/callback?code=x&state=y")
        assert r.status_code == 501

    def test_config_endpoint_always_200(self, monkeypatch):
        """config endpoint returns 200 even when not configured (for frontend probe)."""
        _fresh_settings(monkeypatch)
        for key in list(sys.modules):
            if key.startswith("app"):
                del sys.modules[key]
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/v1/auth/oidc/config")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is False


# ---------------------------------------------------------------- Case 6: upsert idempotency
class TestOidcUserUpsert:
    def test_upsert_is_idempotent(self, monkeypatch, tmp_path):
        """Same (sub, iss) upserted twice yields one user row, updated fields."""
        db = tmp_path / "users_test.db"
        _fresh_settings(monkeypatch, USER_DB_PATH=str(db))

        import app.core.security.users as users_mod
        users_mod.reset_store()

        from app.infrastructure.auth.oidc import upsert_oidc_user

        u1 = upsert_oidc_user(sub="user-123", iss="https://iss.example.com",
                              email="a@x.com", name="A")
        u2 = upsert_oidc_user(sub="user-123", iss="https://iss.example.com",
                              email="b@x.com", name="B")

        assert u1["id"] == u2["id"]
        assert u2.get("display_name") == "B"
        assert u2.get("email") == "b@x.com"

        store = users_mod.get_store()
        assert store.count() == 1

        u3 = upsert_oidc_user(sub="user-456", iss="https://iss.example.com",
                              email="c@x.com", name="C")
        assert u3["id"] != u1["id"]
        assert store.count() == 2


# ---------------------------------------------------------------- cert generation fallback
class TestGenerateCerts:
    def test_generate_server_certs_with_cryptography(self, tmp_path):
        """cryptography fallback: produces ca.crt / server.crt / server.key."""
        from scripts.generate_certs import _use_cryptography
        out = tmp_path / "certs_no_trustme"
        _use_cryptography(client=False, out_dir=out)
        assert (out / "ca.crt").exists()
        assert (out / "server.crt").exists()
        assert (out / "server.key").exists()

    def test_generate_client_certs(self, tmp_path):
        """--client should produce client.crt / client.key / client-ca.crt."""
        from scripts.generate_certs import _use_cryptography
        out = tmp_path / "certs_client"
        _use_cryptography(client=True, out_dir=out)
        assert (out / "client.crt").exists()
        assert (out / "client.key").exists()
        assert (out / "client-ca.crt").exists()
