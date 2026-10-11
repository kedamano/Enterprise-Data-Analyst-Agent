"""SEC/02 生产环境鉴权强制

Spec: docs/specs/SEC/02-production-auth-enforcement.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from app.config import get_settings


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_REQUIRED_IN_PRODUCTION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def prod_env_no_force(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("AUTH_REQUIRED_IN_PRODUCTION", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def dev_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def prod_auth_enabled_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", '[{"key": "k1", "user_id": "u1"}]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 配置项测试
# --------------------------------------------------------------------------- #
def test_auth_required_in_production_default():
    from app.config import Settings
    s = Settings()
    assert s.auth_required_in_production is True


def test_auth_required_in_production_read_from_env(prod_env):
    s = get_settings()
    assert s.auth_required_in_production is True


def test_auth_required_in_production_can_be_disabled(prod_env_no_force):
    s = get_settings()
    assert s.auth_required_in_production is False


# --------------------------------------------------------------------------- #
# 启动决策测试
# --------------------------------------------------------------------------- #
def test_production_no_auth_raises(prod_env):
    from app.core.security.auth import check_startup_security
    with pytest.raises(RuntimeError, match="生产环境必须启用鉴权"):
        check_startup_security()


def test_production_no_auth_no_force_allows(prod_env_no_force):
    from app.core.security.auth import check_startup_security
    check_startup_security()


def test_dev_no_auth_allows(dev_env):
    from app.core.security.auth import check_startup_security
    check_startup_security()


def test_production_auth_enabled_allows(prod_auth_enabled_env):
    from app.core.security.auth import check_startup_security
    check_startup_security()


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_existing_auth_enabled_default():
    from app.config import Settings
    s = Settings()
    assert s.auth_enabled is False
