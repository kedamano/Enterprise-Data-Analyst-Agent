"""物理删除的「不可把服务打挂」回归测试。

**背景（真实故障，非假想）**：在托管环境里，``Path.unlink()`` 可能被安全删除钩子
接管，并在守卫判定失败时 ``raise SystemExit(1)``。``SystemExit`` 继承
``BaseException``，uvicorn 的异常中间件只把 ``Exception`` 转成 500，于是它会**穿透
中间件直接终止整个服务进程**。

实测症状：文件库「删除目录」把整个 API 服务打挂（端口还在但不再响应，
日志里是 ``filestore.delete → blob.unlink → SystemExit``）。
``shutil.rmtree(ignore_errors=True)`` 也挡不住，它只忽略 ``OSError``。

本文件把契约钉死：**删除失败只允许降级，不允许升级为进程级故障**。
"""
from __future__ import annotations

import pathlib

import pytest


class _PoisonedUnlink:
    """上下文管理器：让所有 ``Path.unlink`` 抛 ``SystemExit``，模拟环境删除钩子。"""

    def __enter__(self):
        self._orig = pathlib.Path.unlink
        pathlib.Path.unlink = _raise_system_exit
        return self

    def __exit__(self, *exc):
        pathlib.Path.unlink = self._orig
        return False


def _raise_system_exit(self, *a, **k):
    raise SystemExit(1)


# ---------------------------------------------------------------- safe_fs 单元

def test_purge_file_normal(tmp_path):
    from app.core.safe_fs import purge_file

    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    assert purge_file(f) is True
    assert not f.exists()


def test_purge_file_survives_system_exit(tmp_path):
    """删除被拦截时：不抛异常，降级为 0 字节截断（回收空间、不残留内容）。"""
    from app.core.safe_fs import purge_file

    f = tmp_path / "b.txt"
    f.write_bytes(b"x" * 100)

    with _PoisonedUnlink():
        result = purge_file(f)  # 关键：这行不能抛 SystemExit

    assert result is False, "被拦截时应如实返回 False"
    assert f.exists(), "文件仍在（环境不让删）"
    assert f.stat().st_size == 0, "必须已截断为 0 字节，否则内容会残留"


def test_purge_tree_survives_system_exit(tmp_path):
    from app.core.safe_fs import purge_tree

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.txt").write_bytes(b"y" * 50)

    with _PoisonedUnlink():
        purge_tree(tmp_path)  # 不能抛

    # 目录没能删掉是环境的限制，但绝不能因此崩掉调用方
    assert isinstance(purge_tree(tmp_path), bool)


def test_purge_missing_paths_are_noops(tmp_path):
    from app.core.safe_fs import purge_file, purge_tree

    assert purge_file(tmp_path / "nope.txt") is True
    assert purge_tree(tmp_path / "nodir") is True


# ---------------------------------------------------------------- 存储层

def test_filestore_delete_returns_count_when_unlink_blocked(tmp_path):
    """``FileStore.delete`` 在删除被拦截时仍须返回删除的节点数（逻辑删除照常完成）。"""
    from app.core.filestore import FileStore

    fs = FileStore(db_path=tmp_path / "fs.db", blob_dir=tmp_path / "blobs")
    folder = fs.create_folder("", "目录")
    sub = fs.create_folder(folder["id"], "子目录")
    fs.save_file(sub["id"], "a.csv", b"id,amt\n1,2\n")

    with _PoisonedUnlink():
        n = fs.delete(folder["id"])  # 关键：不能抛 SystemExit 打挂进程

    assert n == 3, "目录 + 子目录 + 文件 = 3 个节点"
    assert fs.get(folder["id"]) is None, "DB 行必须已删除（事实来源是数据库）"


# ---------------------------------------------------------------- API 层

@pytest.fixture
def env(monkeypatch, tmp_path):
    """Mock LLM + 临时文件库（与 test_kb_multi_and_files_api.py 同源的隔离方式）。"""
    monkeypatch.setenv("MOCK_LLM", "true")

    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()

    from app.core import filestore

    fs = filestore.FileStore(db_path=tmp_path / "fs.db", blob_dir=tmp_path / "blobs")
    # 必须 patch 单例本体：模块级函数在 import 期已绑定，patch 函数名无效
    monkeypatch.setattr(filestore, "_STORE", fs)

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client, fs

    get_settings.cache_clear()
    reset_llm()


def test_api_delete_folder_blocked_unlink_still_returns_200(env):
    """复刻原始故障：`DELETE /files/node/{id}` 遇到被拦截的 unlink，
    必须正常返回 200，而不是把服务打挂。"""
    client, _fs = env

    folder = client.post("/api/v1/files/folder",
                         json={"parent_id": "", "name": "端到端目录"}).json()
    sub = client.post("/api/v1/files/folder",
                      json={"parent_id": folder["id"], "name": "2024 Q1"}).json()
    up = client.post("/api/v1/files/upload",
                     data={"parent_id": sub["id"]},
                     files={"file": ("orders.csv", b"region,amt\nEast,120\n", "text/csv")})
    assert up.status_code == 200, up.text

    with _PoisonedUnlink():
        resp = client.delete(f"/api/v1/files/node/{folder['id']}")  # 原始故障点

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 3

    # 服务仍然活着：后续请求照常（原故障下这里会连接失败）
    assert client.get("/api/v1/files/tree").status_code == 200
    assert client.get("/api/v1/health").status_code == 200
