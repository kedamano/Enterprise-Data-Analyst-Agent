"""LIVE integration tests against a **real Milvus**.

两种后端都支持，任一可用即真跑；都不可用则 skip（绝不让 CI 因缺库而红）：

1. **Milvus Lite**（嵌入式，无需服务端）—— ``MILVUS_LITE_PATH=./data/milvus_lite``
   推荐用于本机/CI 真实验证：能跑到真实的向量写入 + ANN 检索，且不依赖 Docker。
2. **Milvus 服务端**（docker: agent-milvus）—— ``MILVUS_HOST=localhost`` /
   ``MILVUS_PORT=19530``；或 ``MILVUS_URI=http://localhost:19530``。

每次运行使用独立 collection，跑完删除，保持共享实例干净。
Milvus 后端需要真实嵌入模型（sentence-transformers）；嵌入不可用时 skip。
"""
from __future__ import annotations

import os
import socket
import uuid

import pytest

from app.config import get_settings
from app.core.tools import knowledge_tool

_LITE_ENV = os.getenv("MILVUS_LITE_PATH", "").strip()
_URI_ENV = os.getenv("MILVUS_URI", "").strip()


def _server_reachable() -> bool:
    try:
        s = socket.create_connection(("localhost", 19530), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def _resolve_live_uri() -> str | None:
    """确定本次 live 测试要连的 Milvus URI（Milvus Lite 优先）。"""
    if _LITE_ENV:
        return _LITE_ENV
    if _URI_ENV:
        return _URI_ENV
    if _server_reachable():
        return "http://localhost:19530"
    return None


_LIVE_URI = _resolve_live_uri()
# 文件式（Milvus Lite）用独立环境变量，避免污染 pymilvus 自身的 MILVUS_URI
_LITE = bool(_LITE_ENV)

pytestmark = pytest.mark.skipif(
    _LIVE_URI is None,
    reason="Milvus 不可达且未配置 MILVUS_LITE_PATH/MILVUS_URI（跳过 live 测试）",
)


def _require_embedding() -> None:
    """Milvus 后端依赖真实嵌入；不可用则 skip（不误报为失败）。"""
    if knowledge_tool._embed("dimension probe") is None:
        pytest.skip(f"嵌入模型不可用，Milvus 后端不可测: {knowledge_tool._embed_error}")


@pytest.fixture
def milvus_env(monkeypatch):
    collection = f"da_knowledge_it_{uuid.uuid4().hex[:8]}"
    if _LITE:
        monkeypatch.setenv("MILVUS_LITE_PATH", _LIVE_URI)
    else:
        monkeypatch.setenv("MILVUS_URI", _LIVE_URI)
    monkeypatch.setenv("MILVUS_COLLECTION", collection)
    get_settings.cache_clear()
    knowledge_tool._store = None  # 重置单例
    _require_embedding()
    yield collection
    # teardown: drop 测试 collection + 重置单例
    try:
        client = knowledge_tool._milvus_client()
        if client is not None and client.has_collection(collection):
            client.drop_collection(collection)
    except Exception:
        pass
    knowledge_tool._store = None
    get_settings.cache_clear()


def _is_lite() -> bool:
    return _LITE


def test_milvus_store_roundtrip(milvus_env):
    """真实 Milvus 上：写入 → 向量检索 → 最相关命中正确。"""
    store = knowledge_tool.get_store()
    assert isinstance(store, knowledge_tool.MilvusKnowledgeStore), \
        f"配置 Milvus 时应使用 Milvus 后端，实际 {type(store).__name__}"

    store.add("营收（revenue）定义为订单金额总和扣除退款后的净额。", "metric_defs")
    store.add("华北地区涵盖北京、天津、河北、山西、内蒙古。", "region_defs")
    store.add("Git 是分布式版本控制系统。", "unrelated")

    hits = store.search("营收的定义是什么", top_k=2)
    assert hits, "应能检索到结果"
    assert "营收" in hits[0]["text"], f"最相关命中应是营收定义，实际: {hits[0]['text'][:40]}"
    assert hits[0]["score"] > 0


def test_milvus_backend_is_real_not_fallback(milvus_env):
    """明确断言：用的是 Milvus（不是悄悄回退 SQLite）。"""
    store = knowledge_tool.get_store()
    assert isinstance(store, knowledge_tool.MilvusKnowledgeStore)
    assert store.dim == 384, f"all-MiniLM-L6-v2 应为 384 维，实际 {store.dim}"
    client = knowledge_tool._milvus_client()
    assert client is not None and client.has_collection(store.collection)


def test_knowledge_search_tool_end_to_end_on_milvus(milvus_env):
    """经工具层（knowledge_search）在真实 Milvus 上端到端检索。"""
    from app.core.tools import execute_tool

    ingest = execute_tool("e1", "knowledge_search", {"query": "x"}, "milvus_it")  # warm store
    assert ingest.status in ("SUCCESS", "FAILED")
    store = knowledge_tool.get_store()
    store.add("客户留存率 = 期末活跃客户 / 期初活跃客户。", "kpi_defs")

    res = execute_tool("k1", "knowledge_search",
                       {"query": "客户留存率怎么算", "top_k": 3}, "milvus_it")
    assert res.status == "SUCCESS", res.error
    chunks = res.output.get("chunks", [])
    assert any("留存率" in c["text"] for c in chunks), f"应检索到 KPI 定义，实际: {chunks}"


def test_lite_uri_is_isolated_file(milvus_env):
    """Milvus Lite 时，数据落在给定路径（同名目录）里（本地隔离，不污染共享实例）。

    注：pymilvus 要求本地 URI 以 ``.db`` 结尾，适配层会自动补齐，
    故这里用 ``resolve_uri()``（规范化后的实际路径）而不是原始环境变量值。
    """
    if not _is_lite():
        pytest.skip("仅在 Milvus Lite（本地文件 URI）下适用")
    from app.infrastructure.vectorstore.milvus import resolve_uri

    actual = resolve_uri()
    assert actual and actual.endswith(".db"), f"Milvus Lite URI 应以 .db 结尾: {actual}"
    assert os.path.exists(actual), f"Milvus Lite 数据路径应存在: {actual}"


def test_milvus_tenant_isolation_end_to_end(milvus_env):
    """多租户隔离（真实 Milvus）：tenant_a 不得检索到 tenant_b 的语料。

    回归：此前 ``MilvusKnowledgeStore.add/search`` 没有 ``tenant`` 参数，
    工具层 ``knowledge_search`` 传 ``tenant=`` 时直接 TypeError（真实 Milvus 上必现）。
    """
    from app.core.tools import execute_tool

    store = knowledge_tool.get_store()
    store.add("租户A的营收口径：含税。", "a", tenant="tenant_a")
    store.add("租户B的营收口径：不含税。", "b", tenant="tenant_b")

    res = execute_tool("t1", "knowledge_search",
                       {"query": "营收口径", "top_k": 5, "tenant": "tenant_a"}, "milvus_ten")
    assert res.status == "SUCCESS", res.error
    texts = [c["text"] for c in res.output.get("chunks", [])]
    assert texts, "应检索到本租户语料"
    assert any("含税" in t for t in texts), texts
    assert not any("不含税" in t for t in texts), f"跨租户泄漏: {texts}"


def test_fallback_to_sqlite_when_milvus_not_configured(monkeypatch):
    monkeypatch.setenv("MILVUS_HOST", "")
    monkeypatch.setenv("MILVUS_URI", "")
    monkeypatch.setenv("MILVUS_LITE_PATH", "")
    get_settings.cache_clear()
    knowledge_tool._store = None
    try:
        store = knowledge_tool.get_store()
        assert isinstance(store, knowledge_tool.KnowledgeStore), \
            "未配置 Milvus 时应回退 SQLite 后端"
    finally:
        knowledge_tool._store = None
        get_settings.cache_clear()
