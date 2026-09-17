"""知识库 / 数据源管理面 API 回归测试。

锁定前端「知识库」与「数据源」两个面板所依赖的契约：

* ``GET    /api/v1/documents``            来源清单 + 整体状态
* ``POST   /api/v1/documents/upload``     浏览器上传（文件或粘贴文本）入库
* ``GET    /api/v1/documents/search``     检索预览
* ``DELETE /api/v1/documents?source=``    按来源删除
* ``GET    /api/v1/datasources``          数据库连接清单（密码必须脱敏）

隔离要点（踩过的坑，勿改回）：
``pipeline.py`` 在 **import 期**就通过 ``from ..core.rag.retriever import get_store``
绑定了 ``knowledge_tool.get_store`` 这个函数对象，所以
``monkeypatch.setattr(knowledge_tool, "get_store", ...)`` **不会**影响入库路径
（入库仍会写真实 ``data/knowledge.db``）。正确做法是 patch 单例本体 ``_store``——
两条路径最终都调同一个函数对象，而它读的是模块全局 ``_store``。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def kb(monkeypatch, tmp_path):
    """Mock LLM + 独立临时知识库；返回 ``(client, store)``。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    # 隔离 Milvus：本测试断言"未配 Milvus 时回退 SQLite"，但 .env 已配 MILVUS_LITE_PATH，
    # 会让 kb_backend() 误判为 sqlite_fallback。清空相关 env（环境变量优先级 > env_file）
    # 还原"未配置"意图。setenv 空串而非 delenv，避免 pydantic 回退读 .env。
    monkeypatch.setenv("MILVUS_LITE_PATH", "")
    monkeypatch.setenv("MILVUS_URI", "")
    monkeypatch.setenv("MILVUS_HOST", "")

    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()

    from app.core.tools import knowledge_tool
    from app.core.tools.knowledge_tool import KnowledgeStore

    # 嵌入置空 → 走纯 BM25：测试不联网、不加载模型（首次加载要等 ~25s 超时）
    monkeypatch.setattr(knowledge_tool, "_embed", lambda _t: None)
    # patch 单例本体（见模块 docstring：patch 函数名对入库路径无效）
    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    monkeypatch.setattr(knowledge_tool, "_store", store)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client, store

    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- 列表

def test_documents_list_reports_status_and_sources(kb):
    client, _store = kb

    r = client.get("/api/v1/documents")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"]["enabled"] is True
    assert body["status"]["backend"] == "sqlite"  # 未配 Milvus → 回退 SQLite
    assert body["status"]["total_chunks"] == 0
    assert body["documents"] == [], "全新临时库不应有来源"


# --------------------------------------------------------------------------- 入库 → 列表 → 检索 → 删除

def test_text_ingest_then_list_search_delete(kb):
    client, _store = kb

    # 1) 粘贴文本入库
    r = client.post(
        "/api/v1/documents/upload",
        data={"text": "营收（revenue）定义为订单净额，不含退货与折扣。",
              "source": "kpi_defs"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["chunks"] >= 1
    assert r.json()["source"] == "kpi_defs"

    # 2) 列表能看到该来源
    listing = client.get("/api/v1/documents").json()
    assert listing["status"]["total_chunks"] >= 1
    assert [d["source"] for d in listing["documents"]] == ["kpi_defs"]

    # 3) 检索能命中
    s = client.get("/api/v1/documents/search", params={"q": "营收口径", "top_k": 3})
    assert s.status_code == 200, s.text
    hits = s.json()["hits"]
    assert hits, "刚入库的内容应能被检索到"
    assert hits[0]["source"] == "kpi_defs"
    assert "营收" in hits[0]["text"]

    # 4) 按来源删除
    d = client.delete("/api/v1/documents", params={"source": "kpi_defs"})
    assert d.status_code == 200, d.text
    assert d.json() == {"source": "kpi_defs", "deleted": 1}

    # 5) 删除后列表清空、检索不再命中
    after = client.get("/api/v1/documents").json()
    assert after["status"]["total_chunks"] == 0
    assert after["documents"] == []
    assert client.get(
        "/api/v1/documents/search", params={"q": "营收口径"}
    ).json()["hits"] == []


def test_upload_file_ingest(kb):
    """浏览器上传文件（走 ETL 解析 → 分块 → 入库）。"""
    client, _store = kb

    r = client.post(
        "/api/v1/documents/upload",
        files={"file": ("口径-华东.md", "华东区域涵盖上海、江苏、浙江。".encode("utf-8"),
                        "text/markdown")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["chunks"] >= 1
    assert r.json()["source"] == "口径-华东.md"

    sources = [d["source"] for d in client.get("/api/v1/documents").json()["documents"]]
    assert "口径-华东.md" in sources


# --------------------------------------------------------------------------- 边界与错误路径

def test_upload_without_file_or_text_is_400(kb):
    client, _store = kb
    r = client.post("/api/v1/documents/upload", data={})
    assert r.status_code == 400
    assert "file" in r.json()["detail"] or "text" in r.json()["detail"]


def test_search_requires_non_empty_query(kb):
    client, _store = kb
    assert client.get("/api/v1/documents/search").status_code == 422
    assert client.get("/api/v1/documents/search", params={"q": ""}).status_code == 422


def test_delete_unknown_source_deletes_zero(kb):
    client, _store = kb
    r = client.delete("/api/v1/documents", params={"source": "does_not_exist"})
    assert r.status_code == 200
    assert r.json()["deleted"] == 0


# --------------------------------------------------------------------------- 数据源（连接配置，密码脱敏）

def test_datasources_lists_masked_connections(kb):
    client, _store = kb

    r = client.get("/api/v1/datasources")
    assert r.status_code == 200, r.text
    sources = r.json()["sources"]
    assert sources, "至少应返回主数据源"

    for s in sources:
        assert {"name", "dialect", "url", "readonly"} <= set(s), s
        # 凭证绝不能出现在响应里
        if "://" in s["url"] and "@" in s["url"]:
            cred = s["url"].split("://", 1)[1]
            assert "***" in cred, f"密码未脱敏: {s['url']}"


def test_masked_dsn_hides_password_only():
    """``_mask_dsn`` 只打码密码，保留用户名 / 主机 / 库名，便于排障定位。"""
    from app.api.routes.health import _mask_dsn

    masked = _mask_dsn("postgresql://alice:s3cr3t@db.internal:5432/warehouse")
    assert "s3cr3t" not in masked
    assert "alice" in masked and "db.internal" in masked and "warehouse" in masked
    assert masked == "postgresql://alice:***@db.internal:5432/warehouse"

    # sqlite 文件路径无凭证，原样返回
    assert _mask_dsn("sqlite:///./data/x.db") == "sqlite:///./data/x.db"
