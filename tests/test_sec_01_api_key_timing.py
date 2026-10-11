"""SEC/01 API Key 时序攻击防护与 parse_keys 缓存

Spec: docs/specs/SEC/01-api-key-timing-and-cache.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from app.core.security.auth import resolve_key, parse_keys, _parse_keys_cached
from app.config import get_settings


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", '[{"key": "test-key-123", "user_id": "user1", "roles": ["analyst"]}]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# parse_keys 缓存测试
# --------------------------------------------------------------------------- #
def test_parse_keys_cached_returns_same_result(auth_env):
    raw = '[{"key": "k1", "user_id": "u1"}]'
    result1 = _parse_keys_cached(raw)
    result2 = _parse_keys_cached(raw)
    assert result1 == result2
    assert len(result1) == 1
    assert result1[0][0] == "k1"


def test_parse_keys_cached_different_inputs(auth_env):
    raw1 = '[{"key": "k1", "user_id": "u1"}]'
    raw2 = '[{"key": "k2", "user_id": "u2"}]'
    result1 = _parse_keys_cached(raw1)
    result2 = _parse_keys_cached(raw2)
    assert result1[0][0] == "k1"
    assert result2[0][0] == "k2"


def test_parse_keys_cached_empty_string(auth_env):
    result = _parse_keys_cached("")
    assert result == []


def test_parse_keys_cached_invalid_json(auth_env):
    with pytest.raises(ValueError, match="AUTH_KEYS 不是合法 JSON"):
        _parse_keys_cached("not valid json")


def test_parse_keys_cached_not_list(auth_env):
    with pytest.raises(ValueError, match="AUTH_KEYS 必须是数组"):
        _parse_keys_cached('{"key": "k1"}')


def test_parse_keys_cached_missing_key(auth_env):
    with pytest.raises(ValueError, match="每项必须含 key"):
        _parse_keys_cached('[{"user_id": "u1"}]')


# --------------------------------------------------------------------------- #
# resolve_key 时序安全测试
# --------------------------------------------------------------------------- #
def test_resolve_key_correct_key(auth_env):
    principal = resolve_key("test-key-123")
    assert principal is not None
    assert principal.user_id == "user1"


def test_resolve_key_wrong_key(auth_env):
    principal = resolve_key("wrong-key")
    assert principal is None


def test_resolve_key_empty_key(auth_env):
    principal = resolve_key("")
    assert principal is None


def test_resolve_key_none_key(auth_env):
    principal = resolve_key(None)
    assert principal is None


def test_resolve_key_uses_hmac_compare(auth_env):
    with patch("app.core.security.auth.hmac.compare_digest", return_value=True) as mock_compare:
        principal = resolve_key("test-key-123")
        assert principal is not None
        mock_compare.assert_called()


def test_resolve_key_does_not_use_equality(auth_env):
    with patch("app.core.security.auth.hmac.compare_digest", return_value=False) as mock_compare:
        principal = resolve_key("test-key-123")
        assert principal is None
        mock_compare.assert_called()


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_parse_keys_backward_compatible(auth_env):
    raw = '[{"key": "k1", "user_id": "u1", "roles": ["analyst"], "allowed_tables": ["fact_sales"]}]'
    result = parse_keys(raw)
    assert len(result) == 1
    key, principal = result[0]
    assert key == "k1"
    assert principal.user_id == "u1"
    assert principal.roles == ["analyst"]
    assert principal.allowed_tables == ["fact_sales"]
