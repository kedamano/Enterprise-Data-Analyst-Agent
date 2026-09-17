"""E8/03 嵌入向量版本迁移 — TDD red → green。

覆盖 SDD §4 测试矩阵 V1-V13。"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import Settings
from app.core.tools import knowledge_tool as kt
from app.core.tools.knowledge_tool import KnowledgeStore


# 测试用「足够长」文本（沿用 D58 的 quality-gate 实践）
DOC_T = "根据 2024 年度经营分析报告，华东区域年度营收壹仟贰佰肆拾伍万元整。"


def _patch_settings(**overrides):
    """返回 patcher：把 kt.get_settings 替换成 Settings 覆盖实例。"""
    from app.config import Settings
    base = Settings()
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return patch.object(kt, "get_settings", return_value=base)


@pytest.fixture
def store(tmp_path, monkeypatch) -> KnowledgeStore:
    """SQLite + _embed 返回定长向量 → add 走 STATUS_OK。"""
    monkeypatch.setattr(kt, "_embed",
                        lambda text: [0.1, 0.2, 0.3, 0.4])
    base = Settings()
    object.__setattr__(base, "embed_model_version", "v1")
    monkeypatch.setattr(kt, "get_settings", lambda: base)
    return KnowledgeStore(db_path=Path(tempfile.mkdtemp()) / "v.db")


# --------------------------------------------------------------------------- #
# 1. Schema
# --------------------------------------------------------------------------- #

class TestEmbedVersionSchema:
    def test_add_fills_embed_model_version(self, store):
        rowid = store.add(DOC_T, "v1doc")
        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT embed_model_version FROM chunks WHERE id=?",
                (rowid,)).fetchone()
        assert row is not None
        assert row[0] == "v1"


# --------------------------------------------------------------------------- #
# 2. Search 版本隔离
# --------------------------------------------------------------------------- #

class TestSearchVersionIsolation:
    def test_stale_version_excluded_by_default(self, store, monkeypatch):
        """旧版 chunk 升 settings.embed_model_version=v2 后，默认 search 隔离。"""
        old_id = store.add(DOC_T + " 旧版", "old_src")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                      (old_id,))
        new_id = store.add(DOC_T + " 新版", "new_src")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v2' WHERE id=?",
                      (new_id,))

        # 升 settings
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)

        hits = store.search(DOC_T, top_k=10)
        ids = {h["id"] for h in hits}
        assert new_id in ids
        assert old_id not in ids

    def test_stale_version_included_with_fallback(self, store, monkeypatch):
        old_id = store.add(DOC_T + " 旧版", "old2_src")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                      (old_id,))
        new_id = store.add(DOC_T + " 新版", "new2_src")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v2' WHERE id=?",
                      (new_id,))

        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)

        hits = store.search(DOC_T, top_k=10, include_stale_versions=True)
        ids = {h["id"] for h in hits}
        assert new_id in ids
        assert old_id in ids


# --------------------------------------------------------------------------- #
# 3. version_stats
# --------------------------------------------------------------------------- #

class TestVersionStats:
    def test_stats_reports_distribution(self, store, monkeypatch):
        a = store.add(DOC_T + " A", "sa")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?", (a,))
        b = store.add(DOC_T + " B", "sb")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v2' WHERE id=?", (b,))

        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)

        stats = store.version_stats()
        assert stats["current"] == "v2"
        assert stats["distribution"] == {"v1": 1, "v2": 1}
        assert stats["stale_count"] == 1
        assert stats["total"] == 2


# --------------------------------------------------------------------------- #
# 4. reembed 单条
# --------------------------------------------------------------------------- #

class TestReembedSingle:
    def test_reembed_chunk_upgrades_version(self, store, monkeypatch):
        rowid = store.add(DOC_T + " upgrade-me", "rm")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                      (rowid,))

        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)

        res = store.reembed_chunk(rowid)
        assert res["ok"] == 1
        assert res["version_before"] == "v1"
        assert res["version_after"] == "v2"

        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT embed_model_version, vec FROM chunks WHERE id=?",
                (rowid,)).fetchone()
        assert row[0] == "v2"
        assert row[1]  # vec 非空

    def test_reembed_missing_returns_reason(self, store, monkeypatch):
        res = store.reembed_chunk(999999)
        assert res["ok"] == 0
        assert res["reason"] == "missing"


# --------------------------------------------------------------------------- #
# 5. reembed_batch
# --------------------------------------------------------------------------- #

class TestReembedBatch:
    def test_reembed_batch_migrates_multiple(self, store, monkeypatch):
        ids = []
        for i in range(5):
            rid = store.add(f"{DOC_T} batch-{i}", f"b{i}")
            with sqlite3.connect(store.db) as c:
                c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                          (rid,))
            ids.append(rid)

        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)

        res = store.reembed_batch()
        assert res["migrated"] == 5
        assert res["failed"] == 0

    def test_reembed_batch_failed_embed_keeps_old_version(self, store, monkeypatch):
        rid = store.add(DOC_T + " will-fail", "wf")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                      (rid,))

        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        monkeypatch.setattr(kt, "_embed", lambda text: None)

        res = store.reembed_batch()
        assert res["failed"] == 1
        assert res["migrated"] == 0

        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT embed_model_version FROM chunks WHERE id=?",
                (rid,)).fetchone()
        assert row[0] == "v1"  # 保留旧版本


# --------------------------------------------------------------------------- #
# 6. Admin API
# --------------------------------------------------------------------------- #

from fastapi.testclient import TestClient
from app.main import app

API_PREFIX = "/api/v1"


@pytest.fixture
def vstore(tmp_path) -> KnowledgeStore:
    """SQLite 路径（不依赖 Milvus）。"""
    base = Settings()
    object.__setattr__(base, "embed_model_version", "v1")
    return KnowledgeStore(db_path=Path(tempfile.mkdtemp()) / "v2.db")


def _prime(kt_ref, base_text=DOC_T, count=2):
    for i in range(count):
        rid = kt_ref.get_store().add(f"{base_text} prime-{i}", f"p{i}")
        with sqlite3.connect(kt_ref.get_store().db) as c:
            c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?", (rid,))


class TestAdminVersionAPI:
    def test_get_version(self, vstore, monkeypatch):
        monkeypatch.setattr(kt, "_embed",
                            lambda text: [0.1, 0.2, 0.3, 0.4])
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v1")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        monkeypatch.setattr(kt, "_store", vstore, raising=False)
        _prime(kt, DOC_T, count=2)
        with TestClient(app) as client:
            resp = client.get(f"{API_PREFIX}/knowledge-bases/admin/embed-version")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["current"] == "v1"
            assert body["distribution"] == {"v1": 2}
            assert body["stale_count"] == 0

    def test_rotate_version_updates_settings(self, vstore, monkeypatch):
        monkeypatch.setattr(kt, "_embed",
                            lambda text: [0.1, 0.2, 0.3, 0.4])
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v1")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        monkeypatch.setattr(kt, "_store", vstore, raising=False)
        with TestClient(app) as client:
            resp = client.post(
                f"{API_PREFIX}/knowledge-bases/admin/embed-version/rotate",
                json={"new_version": "v2"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["current"] == "v2"
            assert body["previous"] == "v1"

    def test_admin_re_embed(self, vstore, monkeypatch):
        monkeypatch.setattr(kt, "_embed",
                            lambda text: [0.1, 0.2, 0.3, 0.4])
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v1")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        monkeypatch.setattr(kt, "_store", vstore, raising=False)
        with TestClient(app) as client:
            rid = kt.get_store().add(DOC_T + " adm", "adm1")
            with sqlite3.connect(kt.get_store().db) as c:
                c.execute("UPDATE chunks SET embed_model_version='v1' WHERE id=?",
                          (rid,))
            resp = client.post(
                f"{API_PREFIX}/knowledge-bases/admin/chunks/{rid}/re-embed",
                json={})
            assert resp.status_code == 200, resp.text

    def test_admin_migrate(self, vstore, monkeypatch):
        monkeypatch.setattr(kt, "_embed",
                            lambda text: [0.1, 0.2, 0.3, 0.4])
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        monkeypatch.setattr(kt, "_store", vstore, raising=False)
        _prime(kt, DOC_T, count=3)
        with TestClient(app) as client:
            resp = client.post(
                f"{API_PREFIX}/knowledge-bases/admin/embed-version/migrate",
                json={"limit": 100})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["migrated"] == 3


# --------------------------------------------------------------------------- #
# 7. Milvus 后门
# --------------------------------------------------------------------------- #

class TestMilvusStub:
    def test_stub_methods(self):
        from app.core.tools.knowledge_tool import MilvusKnowledgeStore
        class _Stub:
            pass
        try:
            ms = MilvusKnowledgeStore(_Stub(), "dummy")
        except Exception:
            pytest.skip("Milvus 后端不可用（无 pymilvus）")

        assert ms.get_current_embed_version() == Settings().embed_model_version
        vs = ms.version_stats()
        assert "current" in vs
        assert vs.get("distribution") == {}

        res = ms.reembed_chunk(1)
        assert res.get("ok") in (0, 1) or "reason" in res

        res = ms.reembed_batch(limit=3)
        assert "migrated" in res and "failed" in res and "skipped" in res


# --------------------------------------------------------------------------- #
# 8. D58 回归：retry_embed 成功后版本也升
# --------------------------------------------------------------------------- #

class TestD58RegresssWithVersion:
    def test_retry_success_sets_current_version(self, tmp_path, monkeypatch):
        """D58 成功后应把 chunk 的 embed_model_version 置为当前 settings 版本。"""
        monkeypatch.setattr(kt, "_embed",
                            lambda text: [0.1, 0.2, 0.3, 0.4])
        base = Settings()
        object.__setattr__(base, "embed_model_version", "v2")
        monkeypatch.setattr(kt, "get_settings", lambda: base)
        store = KnowledgeStore(db_path=Path(tempfile.mkdtemp()) / "r.db")
        # 用新版写入一条 v1 的 chunk
        rid = store.add(DOC_T + " retry", "retry_src")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET status='embed_failed', "
                      "embed_model_version='v1', failed_count=1 WHERE id=?",
                      (rid,))
        stats = store.retry_embed()
        assert stats["fixed"] == 1
        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT embed_model_version FROM chunks WHERE id=?",
                (rid,)).fetchone()
        assert row[0] == "v2"
