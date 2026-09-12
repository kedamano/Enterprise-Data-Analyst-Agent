"""Multi-tenant isolation for knowledge & long-term memory.

Contract: default (empty tenant) stays global/no-filter for backwards compat;
once a tenant is set on a record, search scoped to that tenant must not leak
records from another tenant.
"""
from __future__ import annotations

from app.config import get_settings
from app.core.memory import long_term
from app.core.tools.knowledge_tool import KnowledgeStore


def test_knowledge_tenant_isolation(tmp_path, monkeypatch):
    import app.core.tools.knowledge_tool as kt

    monkeypatch.setattr(kt, "_embed", lambda text: None)  # 走 BM25，离线快

    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    store.add("华北营收 下滑 的原因与渠道口径说明", "a", tenant="acme")
    store.add("华北营收 下滑 的原因与渠道口径说明", "b", tenant="globex")

    both = store.search("营收 下滑")
    assert len(both) == 2, "默认(全局)应可见全部"

    a_only = store.search("营收 下滑", tenant="acme")
    assert len(a_only) == 1 and a_only[0]["source"] == "a"

    b_only = store.search("营收 下滑", tenant="globex")
    assert len(b_only) == 1 and b_only[0]["source"] == "b", "租户间不得泄露"


def test_long_term_tenant_isolation(tmp_path, monkeypatch):
    # 路径已可配（INTERVIEW/01 §1）：走配置路由而不是 monkeypatch 模块常量
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "lt.jsonl"))
    get_settings.cache_clear()

    long_term.append({"objective": "acme 的营收口径", "type": "def"}, tenant="acme")
    long_term.append({"objective": "globex 的营收口径", "type": "def"}, tenant="globex")

    assert len(long_term.search("营收口径")) == 2, "全局可见全部"
    acme = long_term.search("营收口径", tenant="acme")
    assert len(acme) == 1 and "acme" in acme[0]["objective"]
    g = long_term.search("营收口径", tenant="globex")
    assert len(g) == 1 and "globex" in g[0]["objective"], "租户间不得泄露"
