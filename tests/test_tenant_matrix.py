"""D44：**多租户隔离测试矩阵**。

Gap 原文（五）
------------
> 多租户列（懒迁移，空=全局）…**默认单租户、浅实现；缺租户配额 / 隔离的接口强制与测试矩阵**。

配额与接口强制此前已做（`quota_per_min` → 429；越权读别人的会话 → 403，见
`test_auth_permissions.py`）。本文件补的是**矩阵**——把"哪些面必须隔离、怎么证明"钉在一处，
而不是散落在各自的套件里、让人无法一眼看清隔离story。

矩阵的两条腿（缺一不可）
------------------------
1. **跨租户不可见**：A 写的，B 读不到；
2. **同租户可见**（正向对照）：A 写的，A 读得到。

只测第 1 条是**假绿重灾区**——把功能整个弄坏（比如 store 永远返回空、键名拼错）
也能让"跨租户不可见"全绿。**隔离必须与可用性同时成立**。

覆盖面（含指路）
----------------
| 面 | 本文件 | 备注 |
|---|---|---|
| 长期记忆 `long_term` | ✅ | JSONL 后端 |
| 知识库 `knowledge`(SQLite) | ✅ | BM25 离线路径（不走 embedding） |
| 短期记忆 `short_term` | ✅ | 会话键隔离（内存兜底） |
| 响应缓存 `response_cache` | ✅ | 跨会话不命中 |
| API 会话归属（export/trace） | 见 `test_auth_permissions.py` | 同租户也不默认开放 → 403 |
| Milvus 租户过滤 | 见 `test_milvus_live.py` | 需真实向量库，live 门控 |
| 租户配额 | 见 `test_auth_permissions.py` | 超限 429 |
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst import response_cache
from app.core.memory import long_term, short_term

TENANT_A = "tenant-acme"
TENANT_B = "tenant-globex"
PAYLOAD = "华北营收下滑的渠道口径说明"


# --------------------------------------------------------------------------- #
# 各面的"写 / 读"适配器——矩阵的每一行就是一对
# --------------------------------------------------------------------------- #
def _lt_write(tenant: str) -> None:
    long_term.append({"objective": f"{tenant} {PAYLOAD}", "type": "def"}, tenant=tenant)


def _lt_read(tenant: str) -> list:
    return long_term.search("渠道口径", tenant=tenant)


def _kb_write(store, tenant: str) -> None:
    store.add(f"{PAYLOAD}（{tenant}）", source=tenant, tenant=tenant)


def _kb_read(store, tenant: str) -> list:
    return store.search("渠道口径", tenant=tenant)


def _st_write(session: str) -> None:
    short_term.put(session, "principal", {"tenant": session})


def _st_read(session: str):
    return short_term.get(session, "principal", None)


def _rc_write(session: str) -> None:
    from app.core.agents.data_analyst.state import AgentState

    state = AgentState(session_id=session, user_query=PAYLOAD)
    state.status = "FINISH"          # 只缓存可信的 FINISH（见 response_cache 契约）
    state.report = "## 报告\n华北营收下滑…"   # 无报告不入缓存
    response_cache.put_cached(session, PAYLOAD, state)


def _rc_read(session: str):
    return response_cache.get_cached(session, PAYLOAD)


@pytest.fixture
def kb_store(tmp_path, monkeypatch):
    import app.core.tools.knowledge_tool as kt

    monkeypatch.setattr(kt, "_embed", lambda text: None)   # 走 BM25，离线快
    return kt.KnowledgeStore(db_path=tmp_path / "kb.db")


@pytest.fixture
def lt_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LONG_TERM_PATH", str(tmp_path / "lt.jsonl"))
    get_settings.cache_clear()
    response_cache.clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 矩阵：3 个数据面 × 2 条腿
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("surface", ["long_term", "knowledge", "short_term", "response_cache"])
def test_cross_tenant_is_invisible(surface, kb_store, lt_env):
    """**第 1 条腿**：A 写的，B 读不到。"""
    if surface == "long_term":
        _lt_write(TENANT_A)
        assert _lt_read(TENANT_B) == [], "长期记忆跨租户泄露"
    elif surface == "knowledge":
        _kb_write(kb_store, TENANT_A)
        assert _kb_read(kb_store, TENANT_B) == [], "知识库跨租户泄露"
    elif surface == "short_term":
        _st_write("tenant-a|sess-1")
        assert _st_read("tenant-b|sess-1") is None, "短期记忆跨租户泄露"
    else:
        _rc_write("tenant-a|sess-1")
        assert _rc_read("tenant-b|sess-1") is None, "响应缓存跨租户泄露"


@pytest.mark.parametrize("surface", ["long_term", "knowledge", "short_term", "response_cache"])
def test_same_tenant_sees_its_own(surface, kb_store, lt_env):
    """**第 2 条腿（正向对照）**：A 写的，A 读得到。

    没有这条，"隔离"可以靠"功能全坏"来满足——即典型的**假绿**。
    """
    if surface == "long_term":
        _lt_write(TENANT_A)
        hits = _lt_read(TENANT_A)
        assert hits and TENANT_A in hits[0]["objective"], "同租户读不到自己的长期记忆"
    elif surface == "knowledge":
        _kb_write(kb_store, TENANT_A)
        hits = _kb_read(kb_store, TENANT_A)
        assert hits and hits[0]["source"] == TENANT_A, "同租户读不到自己的语料"
    elif surface == "short_term":
        _st_write("tenant-a|sess-1")
        assert _st_read("tenant-a|sess-1") == {"tenant": "tenant-a|sess-1"}
    else:
        _rc_write("tenant-a|sess-1")
        assert _rc_read("tenant-a|sess-1") is not None, "同会话读不到自己的缓存"


# --------------------------------------------------------------------------- #
# 兼容性：空租户 = 全局（既有行为不变）
# --------------------------------------------------------------------------- #
def test_empty_tenant_stays_global(kb_store, lt_env):
    """**空租户=全局不过滤**是既有契约，不能被"加强隔离"顺手改掉。"""
    long_term.append({"objective": f"全局 {PAYLOAD}", "type": "def"})
    _lt_write(TENANT_A)
    assert len(_lt_read("")) == 2, "空租户应当看到全部（向后兼容）"
