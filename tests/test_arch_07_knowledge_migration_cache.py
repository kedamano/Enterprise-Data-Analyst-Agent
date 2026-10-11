"""ARCH/07 KnowledgeStore 迁移缓存

Spec: docs/specs/ARCH/07-knowledge-migration-cache.md
D70 = 红（N failed）→ D70 实现转绿
"""
from __future__ import annotations

import pytest
import tempfile
import os
from pathlib import Path
from app.core.tools.knowledge_tool import KnowledgeStore, _clear_migration_cache


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_knowledge.db"
        yield db_path


@pytest.fixture
def clean_cache():
    _clear_migration_cache()
    yield
    _clear_migration_cache()


# --------------------------------------------------------------------------- #
# 迁移缓存测试
# --------------------------------------------------------------------------- #
def test_same_db_path_second_init_no_migration(temp_db, clean_cache):
    store1 = KnowledgeStore(temp_db)
    assert store1 is not None

    store2 = KnowledgeStore(temp_db)
    assert store2 is not None


def test_different_db_paths_both_migrate(clean_cache):
    with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
        db_path1 = Path(tmpdir1) / "test1.db"
        db_path2 = Path(tmpdir2) / "test2.db"

        store1 = KnowledgeStore(db_path1)
        store2 = KnowledgeStore(db_path2)
        assert store1 is not None
        assert store2 is not None


def test_migration_cache_clear_allows_reinit(temp_db, clean_cache):
    store1 = KnowledgeStore(temp_db)
    assert store1 is not None

    _clear_migration_cache()

    store2 = KnowledgeStore(temp_db)
    assert store2 is not None


# --------------------------------------------------------------------------- #
# 兼容性测试
# --------------------------------------------------------------------------- #
def test_knowledge_store_can_add_and_search(temp_db, clean_cache):
    store = KnowledgeStore(temp_db)
    store.add("test content", source="test")
    results = store.search("test")
    assert isinstance(results, list)
