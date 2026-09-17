"""Milvus 知识库后端 add→search 全链路契约（真实 Milvus Lite，无需服务端）。

**为什么需要本文件**：``MilvusKnowledgeStore`` 长期是一段**从未被走到**的代码——
本机没有 Milvus 服务端，``get_client()`` 恒返回 None，于是永远回退 SQLite。
一旦真的启用（Milvus Lite 或服务端），四个缺陷同时暴露，且**全部静默**：

1. ``KB_FIELD`` 常量**根本没有定义**却被 ``__init__`` / ``_kb_filter`` 使用
   → ``AttributeError`` → 初始化失败 → 静默回退 SQLite；
2. ``add()`` 不写 ``kb_id``，而 schema 是 ``enable_dynamic_field=False``
   → ``DataNotMatchException: Insert missed an field 'kb_id'``（入库全废）；
3. ``search()`` 不接受 ``kb_id``，而工具层 ``knowledge_tool.py`` 就是按
   ``store.search(query, top_k, kb_id=...)`` 调的（duck typing）
   → ``TypeError: unexpected keyword argument 'kb_id'``；
4. collection 新建后处于 ``released``，未 ``load()`` 就检索
   → ``MilvusException: Collection ... is in state 'released'``。

这四个都无法靠"读代码"发现，只有真跑向量库才会炸。本文件用嵌入式 Milvus Lite
把它们钉住：任一回归都必须变红。

依赖真实嵌入模型（已缓存时约 1s、离线可用）；模型不可用时**跳过**而非失败。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.core.tools import knowledge_tool as kt
from app.infrastructure.vectorstore import milvus as milvus_mod

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _lite_dir() -> str:
    """Milvus Lite 目录：必须**纯 ASCII**（faiss 用窄字符 fopen，中文路径索引写失败）。"""
    base = os.path.join(tempfile.gettempdir(), "da_milvus_contract")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "contract.db")


@pytest.fixture(scope="module")
def store():
    if kt._embed("探针") is None:
        pytest.skip("嵌入模型不可用（离线缓存缺失）→ 跳过真实向量库契约")
    from pymilvus import MilvusClient

    path = _lite_dir()
    try:
        client = MilvusClient(uri=path)
    except Exception as exc:  # pragma: no cover - 环境缺失
        pytest.skip(f"Milvus Lite 不可用：{type(exc).__name__}: {exc}")
    coll = "contract_test"
    try:
        if client.has_collection(coll):
            client.drop_collection(coll)
    except Exception:
        pass
    try:
        return kt.MilvusKnowledgeStore(client, coll)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"MilvusKnowledgeStore 初始化失败：{type(exc).__name__}: {exc}")


def test_kb_field_constant_is_defined():
    """回归 1：KB_FIELD 必须定义（曾缺失导致初始化 AttributeError）。"""
    assert isinstance(getattr(kt.MilvusKnowledgeStore, "KB_FIELD", None), str), \
        "KB_FIELD 未定义 → __init__/_kb_filter 会 AttributeError 并静默回退 SQLite"
    assert kt.MilvusKnowledgeStore.KB_FIELD == "kb_id", \
        "应与 SQLite 侧列名对齐（chunks.kb_id）"


def test_add_accepts_kb_id_and_persists(store):
    """回归 2：add 必须接受 kb_id 并落库（曾漏写必填字段导致 DataNotMatchException）。"""
    # 不带 kb_id：schema 里 kb_id 必填，必须写空串兜底而不是抛异常
    assert store.add("差旅报销标准为住宿每晚不超过500元", source="policy.txt") == 1
    # 带 kb_id：与 SQLite add 签名对齐，工具层会这么调
    assert store.add("数据库连接池最大连接数为50", source="ops.txt", kb_id="kb_ops") == 1


def test_search_accepts_kb_id(store):
    """回归 3 + 4：search 必须接受 kb_id，且 collection 已 load（不报 released）。"""
    # 回归 4：初始化时应已 load，这里直接检索不应抛 "is in state 'released'"
    hits = store.search("数据库连接池", top_k=3)
    assert hits, "向量检索应返回结果（若报 released 说明未 load）"
    assert any("连接池" in h.get("text", "") for h in hits), \
        f"语义检索应命中连接池文档，实际：{hits}"

    # 回归 3：kb_id 过滤（工具层按 kb_id= 调用，曾 TypeError）
    scoped = store.search("数据库连接池", top_k=3, kb_id="kb_ops")
    assert all(h.get("source") == "ops.txt" for h in scoped), \
        f"按 kb_ops 过滤后应只剩 ops.txt，实际：{scoped}"


def test_backend_reports_milvus_when_reachable(monkeypatch):
    """后端三态：可达时必须报 ``milvus``，不能含糊成 ``sqlite``。"""
    monkeypatch.setenv("MILVUS_LITE_PATH", _lite_dir())
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        kt._store = None  # 重置单例，强制重新解析后端
        backend = kt.kb_backend()
    finally:
        get_settings.cache_clear()
        kt._store = None
    assert backend == "milvus", f"Milvus 可达时应报 milvus，实际 {backend}"


def test_backend_distinguishes_unconfigured_from_broken(monkeypatch):
    """后端三态：'没配' 与 '配了但挂了' 必须可区分（静默降级是铁律 3 禁止的）。"""
    from app.config import get_settings

    # 未配置 → sqlite，且**无**错误
    monkeypatch.setenv("MILVUS_LITE_PATH", "")
    monkeypatch.setenv("MILVUS_URI", "")
    monkeypatch.setenv("MILVUS_HOST", "")
    get_settings.cache_clear()
    try:
        kt._store = None
        assert kt.kb_backend() == "sqlite"
        assert kt.kb_last_error() is None
    finally:
        get_settings.cache_clear()
        kt._store = None

    # 配了但连不上 → sqlite_fallback，且**带**原因
    monkeypatch.setenv("MILVUS_URI", "http://127.0.0.1:1")  # 必然连不上
    get_settings.cache_clear()
    try:
        kt._store = None
        # get_store() 是单例，且只在首次调用时探测 Milvus；显式驱动一次连接尝试，
        # 确保"配了但连不上"这个事实被观测到（否则会误判成"没配"）。
        assert milvus_mod.get_client() is None
        assert kt.kb_backend() == "sqlite_fallback"
        assert kt.kb_last_error(), "降级必须带上原因，否则排障无从下手"
    finally:
        get_settings.cache_clear()
        kt._store = None
