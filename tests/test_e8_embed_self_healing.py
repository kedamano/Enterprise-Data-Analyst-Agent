"""E8/02 知识库嵌入失败自愈 + chunk 质量运维面板 — TDD 红 → 绿。

四类用例：
  1. KnowledgeStore 方法：embed_failed 状态机 (重试/放弃)
  2. embed_scheduler：单次跑批 + 后台线程
  3. 运维 admin API：retry / abandon / diagnostics / list
  4. Milvus 鸭子类型：retry_embed / abandon_expired 无 AttributeError
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.tools import knowledge_tool as kt
from app.core.tools.knowledge_tool import KnowledgeStore


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _patch_settings(**overrides):
    """返回 patcher：把 kt.get_settings 替换成一个带覆盖字段的 Settings 实例。"""
    from app.config import Settings
    base = Settings()
    # 用 object.__setattr__ 绕过 pydantic frozen（仅测试用）
    for k, v in overrides.items():
        object.__setattr__(base, k, v)
    return patch.object(kt, "get_settings", return_value=base)


# 测试用「足够长」文本：长度 ≥ 10 非空白 + 标点 < 80%，能通过 quality gate
# （避免短文本被 _classify_chunk 归为 noise 导致不触发 embed 逻辑）
LONG_TEXT = "根据 2024 年度经营分析报告，华东区域年度营收壹仟贰佰肆拾伍万元整，同比增长百分之十八点五"
LONG_TEXT_2 = "华南区域同期营收捌佰玖拾万元整，同比增长百分之十二点三，增速略低于集团平均水平线"


@pytest.fixture
def store(tmp_path, monkeypatch) -> KnowledgeStore:
    """强制走 SQLite：_embed mock 为 None → 所有 add 都走 embed_failed。"""
    monkeypatch.setattr(kt, "_embed", lambda text: None)
    return KnowledgeStore(db_path=tmp_path / "kb.db")


@pytest.fixture
def store_with_embed(tmp_path, monkeypatch) -> KnowledgeStore:
    """模拟：第 1 次 embed 返回 None，第 2 次起返回向量。"""
    calls = {"n": 0}
    VEC = [0.1, 0.2, 0.3, 0.4]

    def fake_embed(text: str):
        calls["n"] += 1
        return None if calls["n"] == 1 else VEC

    monkeypatch.setattr(kt, "_embed", fake_embed)
    return KnowledgeStore(db_path=tmp_path / "kb.db")


# --------------------------------------------------------------------------- #
# 1. KnowledgeStore 方法
# --------------------------------------------------------------------------- #

class TestEmbedFailedStateMachine:
    """嵌入失败 → 重试 → (ok / abandoned) 状态机。"""

    def test_embed_failed_tracks_count_and_time(self, store):
        rowid = store.add(LONG_TEXT, "s1")
        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT status, failed_count, retried_at FROM chunks WHERE id=?",
                (rowid,)).fetchone()
        assert row is not None
        assert row[0] == KnowledgeStore.STATUS_EMBED_FAILED
        assert row[1] == 1
        assert row[2] is not None

    def test_retry_fixed_flips_status_to_ok(self, store_with_embed):
        rowid = store_with_embed.add(LONG_TEXT, "s1")
        stats = store_with_embed.retry_embed()
        assert stats["fixed"] == 1
        assert stats["retried"] == 1
        assert stats["still_failed"] == 0
        assert stats["abandoned_now"] == 0

        with sqlite3.connect(store_with_embed.db) as c:
            row = c.execute(
                "SELECT status, vec FROM chunks WHERE id=?",
                (rowid,)).fetchone()
        assert row[0] == KnowledgeStore.STATUS_OK
        # 不要 is True —— SQLite 返回整数 1；直接验证 vec 非空
        assert row[1] is not None and len(row[1]) > 0

    def test_retry_failure_increments_count(self, store):
        rowid = store.add(LONG_TEXT + " always fails", "s1")
        store.retry_embed()
        store.retry_embed()

        with sqlite3.connect(store.db) as c:
            row = c.execute(
                "SELECT status, failed_count FROM chunks WHERE id=?",
                (rowid,)).fetchone()
        # max_retries=3（默认），共失败 1+2=3 次 = max，仍未超过 → 仍 embed_failed
        assert row[0] == KnowledgeStore.STATUS_EMBED_FAILED
        assert row[1] == 3

    def test_retry_exceeds_max_marks_abandoned(self, store, monkeypatch):
        with _patch_settings(embed_retry_max=2):
            rowid = store.add(LONG_TEXT + " will be abandoned", "s1")
            # 初始 failed_count=1
            store.retry_embed()  # count=2 → 仍 embed_failed（未超过 max=2）
            store.retry_embed()  # count=3 > 2 → abandoned

            with sqlite3.connect(store.db) as c:
                row = c.execute(
                    "SELECT status FROM chunks WHERE id=?", (rowid,)).fetchone()
            assert row[0] == KnowledgeStore.STATUS_ABANDONED

    def test_retry_respects_kb_filter(self, store):
        row_a = store.add(LONG_TEXT, "s1", kb_id="kb_a")
        row_b = store.add(LONG_TEXT_2, "s2", kb_id="kb_b")

        stats = store.retry_embed(kb_id="kb_a")
        assert stats["retried"] == 1
        # kb_b 这条不应该被重试，状态保持 embed_failed（不受影响）
        with sqlite3.connect(store.db) as c:
            b = c.execute("SELECT status FROM chunks WHERE id=?", (row_b,)).fetchone()[0]
        assert b == KnowledgeStore.STATUS_EMBED_FAILED

    def test_retry_does_not_retry_abandoned(self, store):
        rowid = store.add(LONG_TEXT + " to be abandoned", "s1")
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET status='abandoned', failed_count=99 WHERE id=?",
                      (rowid,))
        stats = store.retry_embed()
        assert stats["retried"] == 0

    def test_abandon_expired_marks_old_chunks(self, store):
        rowid = store.add(LONG_TEXT + " old one", "s1")
        old_ts = int(time.time()) - 86400 * 40  # 40 天前
        with sqlite3.connect(store.db) as c:
            c.execute("UPDATE chunks SET retried_at=? WHERE id=?", (old_ts, rowid))
        n = store.abandon_expired(ttl_s=86400 * 30)  # TTL 30 天
        assert n == 1
        with sqlite3.connect(store.db) as c:
            row = c.execute("SELECT status FROM chunks WHERE id=?", (rowid,)).fetchone()
        assert row[0] == KnowledgeStore.STATUS_ABANDONED

    def test_abandon_keeps_fresh_chunks(self, store):
        store.add(LONG_TEXT + " fresh one", "s1")
        n = store.abandon_expired(ttl_s=86400 * 30)
        assert n == 0


# --------------------------------------------------------------------------- #
# 2. embed_scheduler 模块
# --------------------------------------------------------------------------- #

class TestEmbedScheduler:
    """单次跑批 + 后台调度。"""

    def test_retry_once_respects_batch_limit(self, store, monkeypatch):
        with _patch_settings(embed_retry_batch=3):
            for i in range(10):
                store.add(f"{LONG_TEXT} doc #{i}", f"s{i}")
            from app.core.tools.embed_scheduler import retry_once
            stats = retry_once(store)
            assert stats["retried"] == 3

    def test_retry_once_returns_correct_counts(self, store, monkeypatch):
        with _patch_settings(embed_retry_batch=100):
            for i in range(5):
                store.add(f"{LONG_TEXT} doc #{i}", f"s{i}")
            from app.core.tools.embed_scheduler import retry_once
            stats = retry_once(store)
            assert stats["retried"] == 5
            assert stats["still_failed"] == 5
            assert stats["fixed"] == 0  # embed mock = None → 全部失败

    def test_background_scheduler_starts_and_stops(self, store, monkeypatch):
        """启动 daemon 线程跑 scheduler，验证不会挂起。"""
        from app.core.tools.embed_scheduler import start_background_scheduler, stop_background_scheduler
        t = start_background_scheduler(store, interval_s=1)
        assert t is not None
        assert t.is_alive()
        time.sleep(2.5)  # 至少跑 2 个 tick
        stop_background_scheduler()
        t.join(timeout=5)
        assert not t.is_alive()


# --------------------------------------------------------------------------- #
# 3. 运维 admin API
# --------------------------------------------------------------------------- #

from fastapi.testclient import TestClient

from app.main import app


API_PREFIX = "/api/v1"  # 来自 settings.api_prefix


@pytest.fixture
def admin_client(store, monkeypatch) -> TestClient:
    """注入 _store 单例 bypass Milvus。"""
    monkeypatch.setattr(kt, "_store", store, raising=False)
    return TestClient(app)


class TestAdminSelfHealingAPI:

    def test_admin_retry_endpoint_returns_stats(self, admin_client):
        for i in range(3):
            kt.get_store().add(f"{LONG_TEXT} admin doc #{i}", f"a{i}")
        resp = admin_client.post(f"{API_PREFIX}/knowledge-bases/admin/embed-failed/retry",
                                json={"max_chunks": 100})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["retried"] == 3
        assert "fixed" in body
        assert "still_failed" in body
        assert "abandoned_now" in body

    def test_admin_abandon_endpoint_returns_count(self, admin_client):
        kt.get_store().add(LONG_TEXT + " to abandon", "ab1")
        old_ts = int(time.time()) - 86400 * 40
        with sqlite3.connect(kt.get_store().db) as c:
            c.execute("UPDATE chunks SET retried_at=? WHERE source='ab1'", (old_ts,))
        resp = admin_client.post(f"{API_PREFIX}/knowledge-bases/admin/embed-failed/abandon",
                                json={"ttl_s": 86400 * 30})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"abandoned": 1}

    def test_kb_diagnostics_includes_embed_failed_aging(self, admin_client):
        kt.get_store().add(LONG_TEXT + " diag doc", "diag1")
        from app.core.knowledge_catalog import get_catalog
        created = get_catalog().create_base("诊断测试库")
        kb_diag_id = created["id"]
        resp = admin_client.get(f"{API_PREFIX}/knowledge-bases/{kb_diag_id}/diagnostics")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "embed_failed_aging" in body
        aging = body["embed_failed_aging"]
        assert isinstance(aging, dict)
        for k in ("1h", "1d", "7d", "older"):
            assert k in aging, f"missing bucket {k}"

    def test_list_embed_failed_returns_rows(self, admin_client):
        kt.get_store().add(LONG_TEXT + " list me", "list1")
        resp = admin_client.get(f"{API_PREFIX}/knowledge-bases/admin/embed-failed/list",
                               params={"limit": 50})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "rows" in body
        assert isinstance(body["rows"], list)
        if body["rows"]:
            row = body["rows"][0]
            assert "source" in row
            assert "failed_count" in row


# --------------------------------------------------------------------------- #
# 4. Milvus 鸭子类型兼容
# --------------------------------------------------------------------------- #

class TestMilvusDuckTyping:
    """Milvus 不抛 AttributeError。"""

    def test_milvus_stub_methods_exist(self):
        from app.core.tools.knowledge_tool import MilvusKnowledgeStore
        class _Stub:
            pass
        try:
            ms = MilvusKnowledgeStore(_Stub(), "dummy")
        except Exception:
            pytest.skip("Milvus 后端不可用（无 pymilvus）")
        assert ms.retry_embed(kb_id="x") is not None or ms.retry_embed(kb_id="x") == {}
        assert ms.abandon_expired(ttl_s=1) == 0
        assert ms.list_embed_failed(limit=10) == []
