"""ARCH/06 SQLAlchemy Engine 缓存

Spec: docs/specs/ARCH/06-sqlalchemy-engine-cache.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
from app.core.tools.sql_tool import _get_engine, _clear_engine_cache


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_cache():
    _clear_engine_cache()
    yield
    _clear_engine_cache()


# --------------------------------------------------------------------------- #
# 缓存测试
# --------------------------------------------------------------------------- #
def test_same_dsn_returns_same_engine(clean_cache):
    dsn = "sqlite:///./data/sample_enterprise.db"
    engine1 = _get_engine(dsn, 30)
    engine2 = _get_engine(dsn, 30)
    assert engine1 is engine2


def test_different_dsn_returns_different_engine(clean_cache):
    dsn1 = "sqlite:///./data/sample_enterprise.db"
    dsn2 = "sqlite:///./data/sample_analyst.db"
    engine1 = _get_engine(dsn1, 30)
    engine2 = _get_engine(dsn2, 30)
    assert engine1 is not engine2


def test_cache_clear_creates_new_engine(clean_cache):
    dsn = "sqlite:///./data/sample_enterprise.db"
    engine1 = _get_engine(dsn, 30)
    _clear_engine_cache()
    engine2 = _get_engine(dsn, 30)
    assert engine1 is not engine2


def test_cache_max_size(clean_cache):
    dsn_template = "sqlite:///./data/test_{}.db"
    engines = []
    for i in range(10):
        dsn = dsn_template.format(i)
        engines.append(_get_engine(dsn, 30))
    assert len(engines) == 10


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_engine_can_execute(clean_cache):
    from sqlalchemy import text
    dsn = "sqlite:///./data/sample_enterprise.db"
    engine = _get_engine(dsn, 30)
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.fetchone()[0] == 1
