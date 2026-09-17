"""多知识库（KB）与企业文件库 API 回归测试。

覆盖两块新增能力：

**多知识库**（``/api/v1/knowledge-bases``）
* 建库 / 列表 / 改名 / 删库（级联清分块）
* 文档入库三条路：上传文件、粘贴文本、导入网页（网页在离线环境不可达，
  故只断言失败时的错误语义，不假装成功）
* **按库检索的隔离性**：A 库入库的内容，B 库检索不到 —— 这是"多库"最核心的
  契约，一旦退化成全局检索，库就白建了
* 上传文件的 ``source`` 必须是**文件名**，不能是服务端临时路径

**企业文件库**（``/api/v1/files``）
* 目录树 / 列表 / 面包屑 / 全库搜索（结果带完整路径）
* 新建文件夹、上传、重命名（文件与文件夹同入口）、下载、删除（目录级联）
* 同目录重名拒绝；危险文件名（``../``）被清洗

隔离要点（与 ``test_kb_management_api.py`` 同源，踩过的坑）：
``pipeline.py`` 在 **import 期**就绑定了 ``knowledge_tool.get_store`` 这个函数对象，
所以 patch 函数名对入库路径无效，必须 patch **单例本体** ``_store``。
同理目录与文件库分别 patch ``_CATALOG`` / ``_STORE``，否则测试会写进
真实 ``data/knowledge.db``、``data/knowledge_meta.db``、``data/filestore.db``。
"""
from __future__ import annotations

import pytest

# 假向量：让 ``add`` 时 status='ok'（否则 embed_unavailable 会被检索过滤掉），
# 同时避免真的加载 sentence-transformers（首次 ~25s 且会联网）。
_FAKE_VEC = [0.1] * 8


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Mock LLM + 临时知识库目录 + 临时文件库；返回 ``(client, store, catalog)``。"""
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

    from app.core import filestore, knowledge_catalog
    from app.core.tools import knowledge_tool
    from app.core.tools.knowledge_tool import KnowledgeStore

    monkeypatch.setattr(knowledge_tool, "_embed", lambda _t: list(_FAKE_VEC))

    store = KnowledgeStore(db_path=tmp_path / "kb.db")
    monkeypatch.setattr(knowledge_tool, "_store", store)

    catalog = knowledge_catalog.KnowledgeCatalog(db_path=tmp_path / "meta.db")
    monkeypatch.setattr(knowledge_catalog, "_CATALOG", catalog)

    fs = filestore.FileStore(db_path=tmp_path / "fs.db", blob_dir=tmp_path / "blobs")
    monkeypatch.setattr(filestore, "_STORE", fs)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client, store, catalog

    get_settings.cache_clear()
    reset_llm()


def _mk_base(client, name: str, **kw) -> dict:
    r = client.post("/api/v1/knowledge-bases", json={"name": name, **kw})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- 知识库：建 / 列 / 改 / 删

def test_create_list_update_delete_base(env):
    client, store, _cat = env

    assert client.get("/api/v1/knowledge-bases").json()["bases"] == []

    base = _mk_base(client, "人事制度", description="HR 相关口径", kb_type="general")
    assert base["name"] == "人事制度"
    assert base["documents"] == 0 and base["chunks"] == 0
    assert base["visibility"] == "private"

    listing = client.get("/api/v1/knowledge-bases").json()
    assert [b["name"] for b in listing["bases"]] == ["人事制度"]
    assert listing["backend"] == "sqlite"

    r = client.patch(f"/api/v1/knowledge-bases/{base['id']}",
                     json={"name": "人事制度（新）", "visibility": "public"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "人事制度（新）"
    assert r.json()["visibility"] == "public"

    assert client.delete(f"/api/v1/knowledge-bases/{base['id']}").status_code == 200
    assert client.get("/api/v1/knowledge-bases").json()["bases"] == []
    assert store.total_chunks() == 0


def test_missing_base_returns_404(env):
    client, _store, _cat = env
    assert client.get("/api/v1/knowledge-bases/kb_nope/documents").status_code == 404
    assert client.get("/api/v1/knowledge-bases/kb_nope/search?q=x").status_code == 404
    assert client.delete("/api/v1/knowledge-bases/kb_nope").status_code == 404


def test_empty_name_rejected(env):
    client, _store, _cat = env
    r = client.post("/api/v1/knowledge-bases", json={"name": "   "})
    assert r.status_code == 400


# --------------------------------------------------------------------------- 知识库：检索隔离（核心契约）

def test_search_is_scoped_to_its_own_base(env):
    """A 库的内容 B 库检索不到 —— 多知识库存在的意义所在。"""
    client, _store, _cat = env
    a = _mk_base(client, "库A")
    b = _mk_base(client, "库B")

    r = client.post(
        f"/api/v1/knowledge-bases/{a['id']}/documents/text",
        json={"text": "华东区域口径：包含江苏、浙江、上海、安徽四省市，用于区域营收拆分。",
              "name": "华东口径"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["document"]["chunks"] >= 1

    hits_a = client.get(
        f"/api/v1/knowledge-bases/{a['id']}/search", params={"q": "华东区域", "top_k": 3}
    ).json()["hits"]
    assert hits_a, "A 库应检索到自己的内容"
    assert hits_a[0]["source"] == "华东口径"

    hits_b = client.get(
        f"/api/v1/knowledge-bases/{b['id']}/search", params={"q": "华东区域", "top_k": 3}
    ).json()["hits"]
    assert hits_b == [], "B 库不得召回 A 库的内容（库隔离被破坏）"

    # 文档清单同样隔离
    assert len(client.get(f"/api/v1/knowledge-bases/{a['id']}/documents").json()["documents"]) == 1
    assert client.get(f"/api/v1/knowledge-bases/{b['id']}/documents").json()["documents"] == []


def test_ingest_after_version_rotate_is_immediately_searchable(env):
    """版本闸门的「写」与「读」必须用同一个 resolved 版本（回归锁）。

    缺陷背景：``add()`` 按 ``_resolve_emv()``（运行时覆盖优先）写版本快照，而
    ``search()`` 曾用裸 ``settings.embed_model_version`` 过滤。于是 rotate 过一次版本
    之后，**新入库的文档立刻检索不到**——写 ``bge-v1.5``、读按 ``v1`` 过滤，命中恒空，
    且完全静默。这和 ``version_stats`` 漏写 ``{where} {'AND' if where else 'WHERE'}``
    是同一类病：多处并行调用点里只错一处。
    """
    client, store, _cat = env
    base = _mk_base(client, "版本闸门库")

    # rotate 前入库 → 打上旧版本标签
    assert client.post(
        f"/api/v1/knowledge-bases/{base['id']}/documents/text",
        json={"text": "旧版口径：华东区域包含江苏、浙江、上海。", "name": "旧版口径"},
    ).status_code == 200

    r = client.post("/api/v1/knowledge-bases/admin/embed-version/rotate",
                    json={"new_version": "bge-v1.5"})
    assert r.status_code == 200, r.text
    assert r.json()["current"] == "bge-v1.5"
    assert r.json()["previous"] and r.json()["previous"] != "bge-v1.5", \
        "rotate 应回报切换前的活跃版本"

    # rotate 后入库 —— 必须立刻可检索（原缺陷点）
    assert client.post(
        f"/api/v1/knowledge-bases/{base['id']}/documents/text",
        json={"text": "新版口径：库存周转天数等于三百六十五除以周转次数。", "name": "新版口径"},
    ).status_code == 200

    hits = client.get(f"/api/v1/knowledge-bases/{base['id']}/search",
                      params={"q": "库存周转天数", "top_k": 3}).json()["hits"]
    assert hits, (
        "rotate 后新入库的文档检索不到 —— 写入与检索用了不同的版本口径"
        "（写 _resolve_emv / 读裸 settings.embed_model_version）"
    )
    assert hits[0]["source"] == "新版口径"

    # 旧版本分块必须被隔离：跨嵌入空间算出的相似度混进 RRF 会让排序失去意义
    old_sources = [
        h["source"]
        for h in client.get(f"/api/v1/knowledge-bases/{base['id']}/search",
                            params={"q": "华东区域包含", "top_k": 3}).json()["hits"]
    ]
    assert "旧版口径" not in old_sources, "stale 版本的分块仍被召回"

    # 运维侧看到的现状要与检索口径一致（供 migrate 决策）
    stats = client.get("/api/v1/knowledge-bases/admin/embed-version").json()
    assert stats["current"] == "bge-v1.5"
    assert stats["stale_count"] >= 1, "旧版本分块应被统计为 stale"

    # 兼容兜底开关：显式允许时才回退到含 stale 的检索
    fallback = store.search("华东区域包含", 3, kb_id=base["id"],
                            include_stale_versions=True)
    assert "旧版口径" in [h["source"] for h in fallback], \
        "include_stale_versions=True 应能召回旧版本分块（升级期的兜底通道）"


def test_same_source_name_in_two_bases_does_not_collide(env):
    """两个库上传同名文档，各自独立存在（版本/去重按库隔离）。"""
    client, _store, _cat = env
    a = _mk_base(client, "库A")
    b = _mk_base(client, "库B")
    payload = {"text": "季度营收口径说明：以发货净额为准，扣除退货。", "name": "口径说明"}

    client.post(f"/api/v1/knowledge-bases/{a['id']}/documents/text", json=payload)
    client.post(f"/api/v1/knowledge-bases/{b['id']}/documents/text", json=payload)

    for kb in (a, b):
        docs = client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents").json()["documents"]
        assert len(docs) == 1, "同库同名应合并为一条（覆盖语义）"
        assert docs[0]["chunks"] >= 1


# --------------------------------------------------------------------------- 知识库：上传文件（source 修复）

def test_upload_file_uses_filename_as_source(env):
    client, _store, _cat = env
    base = _mk_base(client, "文档库")

    r = client.post(
        f"/api/v1/knowledge-bases/{base['id']}/documents/upload",
        files={"file": ("指标-营收.md", "营收定义为订单净额，按季度统计。".encode("utf-8"),
                        "text/markdown")},
    )
    assert r.status_code == 200, r.text
    doc = r.json()["document"]
    assert doc["name"] == "指标-营收.md"
    assert doc["source"] == "指标-营收.md", "source 必须是文件名，不能是服务端临时路径"
    assert "tmp" not in doc["source"].lower()
    assert doc["chunks"] >= 1

    docs = client.get(f"/api/v1/knowledge-bases/{base['id']}/documents").json()["documents"]
    assert [d["source"] for d in docs] == ["指标-营收.md"]


def test_upload_empty_file_rejected(env):
    client, _store, _cat = env
    base = _mk_base(client, "空文件库")
    r = client.post(
        f"/api/v1/knowledge-bases/{base['id']}/documents/upload",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert r.status_code == 400


def test_empty_text_rejected(env):
    client, _store, _cat = env
    base = _mk_base(client, "空文本库")
    r = client.post(f"/api/v1/knowledge-bases/{base['id']}/documents/text",
                    json={"text": "   ", "name": "x"})
    assert r.status_code == 400


def test_website_ingest_reports_error_instead_of_faking_success(env):
    """离线/非法链接必须给出可读错误，不能假装入库成功。"""
    client, _store, _cat = env
    base = _mk_base(client, "网站库", kb_type="website")

    r = client.post(f"/api/v1/knowledge-bases/{base['id']}/documents/website",
                    json={"url": "not-a-url"})
    assert r.status_code == 400
    assert "http" in r.json()["detail"].lower()


# --------------------------------------------------------------------------- 知识库：删文档 / 删库级联

def test_delete_document_removes_only_its_chunks(env):
    client, store, _cat = env
    base = _mk_base(client, "混合库")
    for name, text in (("甲", "甲文档内容：关于库存周转率的说明。"),
                       ("乙", "乙文档内容：关于应收账款账龄的说明。")):
        client.post(f"/api/v1/knowledge-bases/{base['id']}/documents/text",
                    json={"text": text, "name": name})

    docs = client.get(f"/api/v1/knowledge-bases/{base['id']}/documents").json()["documents"]
    assert len(docs) == 2
    target = next(d for d in docs if d["name"] == "甲")

    r = client.delete(f"/api/v1/knowledge-bases/{base['id']}/documents/{target['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] >= 1

    left = client.get(f"/api/v1/knowledge-bases/{base['id']}/documents").json()["documents"]
    assert [d["name"] for d in left] == ["乙"]

    # 断言"被删的那个来源不再出现"，而不是断言命中集为空：本测试把 ``_embed``
    # 换成了固定假向量（避免加载模型），向量通道会给所有分块相同相似度，
    # 于是无关文档也可能被 RRF 带进候选——那是测试替身的副作用，不是删除没生效。
    hits = client.get(f"/api/v1/knowledge-bases/{base['id']}/search",
                      params={"q": "库存周转率"}).json()["hits"]
    assert all(h["source"] != "甲" for h in hits), "被删文档的内容不应还能检索到"


def test_delete_base_cascades_chunks(env):
    client, store, _cat = env
    base = _mk_base(client, "待删库")
    client.post(f"/api/v1/knowledge-bases/{base['id']}/documents/text",
                json={"text": "这是一段会被级联删除的知识内容，用于验证删库行为。", "name": "临时"})
    assert store.total_chunks(base["id"]) >= 1

    r = client.delete(f"/api/v1/knowledge-bases/{base['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted_chunks"] >= 1
    assert store.total_chunks(base["id"]) == 0
    assert store.total_chunks() == 0, "级联清理不得留下孤儿分块"


# --------------------------------------------------------------------------- 文件库

def _folder(client, parent_id: str, name: str) -> dict:
    r = client.post("/api/v1/files/folder", json={"parent_id": parent_id, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _upload(client, parent_id: str, name: str, content: bytes) -> dict:
    r = client.post("/api/v1/files/upload", data={"parent_id": parent_id},
                    files={"file": (name, content, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_folder_tree_list_breadcrumb(env):
    client, _store, _cat = env
    root_folder = _folder(client, "", "数据集")
    sub = _folder(client, root_folder["id"], "2024")

    tree = client.get("/api/v1/files/tree").json()
    assert {n["name"] for n in tree["nodes"]} == {"数据集", "2024"}
    assert tree["stats"]["folders"] == 2

    listing = client.get("/api/v1/files/list", params={"parent_id": sub["id"]}).json()
    assert [b["name"] for b in listing["breadcrumb"]] == ["数据集", "2024"]
    assert listing["nodes"] == []

    assert client.get("/api/v1/files/list", params={"parent_id": "n_nope"}).status_code == 404


def test_upload_list_search_rename_download_delete(env):
    client, _store, _cat = env
    folder = _folder(client, "", "业务资料")
    node = _upload(client, folder["id"], "orders.csv", b"id,amt\n1,10\n2,20\n")
    assert node["name"] == "orders.csv"
    assert node["is_dir"] is False
    assert node["bytes"] == 17
    assert node["size_label"].endswith("B")

    listing = client.get("/api/v1/files/list", params={"parent_id": folder["id"]}).json()
    assert [n["name"] for n in listing["nodes"]] == ["orders.csv"]

    # 全库搜索：结果必须带完整路径（跨目录定位靠它）
    res = client.get("/api/v1/files/search", params={"q": "orders"}).json()["results"]
    assert len(res) == 1
    assert res[0]["path"] == "/业务资料/orders.csv"

    # 重命名
    r = client.patch(f"/api/v1/files/node/{node['id']}", json={"name": "订单.csv"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "订单.csv"

    # 下载：字节数一致
    dl = client.get(f"/api/v1/files/download/{node['id']}")
    assert dl.status_code == 200
    assert dl.content == b"id,amt\n1,10\n2,20\n"

    # 删除
    assert client.delete(f"/api/v1/files/node/{node['id']}").json()["deleted"] == 1
    assert client.get("/api/v1/files/list", params={"parent_id": folder["id"]}).json()["nodes"] == []


def test_duplicate_name_rejected_in_same_folder(env):
    client, _store, _cat = env
    _folder(client, "", "唯一目录")
    r = client.post("/api/v1/files/folder", json={"parent_id": "", "name": "唯一目录"})
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]


def test_upload_same_name_overwrites(env):
    """同目录同名上传 = 覆盖（用户心里的"再传一次就是更新"）。"""
    client, _store, _cat = env
    first = _upload(client, "", "data.csv", b"a\n1\n")
    second = _upload(client, "", "data.csv", b"a\n1\n2\n3\n")
    assert first["id"] == second["id"], "同名应复用同一节点而不是新建"
    assert second["bytes"] == 8


def test_dangerous_filename_is_sanitized(env):
    """路径穿越名必须被清洗，不能落到父目录。"""
    client, _store, _cat = env
    node = _upload(client, "", "../evil.sh", b"#!/bin/sh\n")
    assert "/" not in node["name"] and "\\" not in node["name"]
    assert ".." not in node["name"]
    names = [n["name"] for n in client.get("/api/v1/files/list").json()["nodes"]]
    assert node["name"] in names


def test_folder_delete_cascades(env):
    client, _store, _cat = env
    outer = _folder(client, "", "外层")
    inner = _folder(client, outer["id"], "内层")
    _upload(client, inner["id"], "deep.csv", b"x,y\n1,2\n")

    r = client.delete(f"/api/v1/files/node/{outer['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 3, "应级联删除 外层/内层/deep.csv"

    assert client.get("/api/v1/files/tree").json()["nodes"] == []
    assert client.get(f"/api/v1/files/download/{inner['id']}").status_code == 404


def test_download_folder_rejected(env):
    client, _store, _cat = env
    folder = _folder(client, "", "目录不可下载")
    r = client.get(f"/api/v1/files/download/{folder['id']}")
    assert r.status_code == 400
