"""Milvus 接法解析（``resolve_uri``）：离线可测，不需要真实 Milvus。

回归背景（本机真跑时暴露）：
* pymilvus 对本地 URI 有**硬性校验**——必须 "endswith .db"，否则
  ``ConnectionConfigException: uri: ... is illegal, needs start with
  [unix, http, https, tcp] or a local file endswith [.db]``。
  于是 ``MILVUS_LITE_PATH=.../milvus_lite`` 会静默回退 SQLite（看起来"配置了但没生效"）。
  适配层因此自动补 ``.db``。
* ``MILVUS_URI`` 是 pymilvus 的**全局**环境变量，import 时按 http(s):// 解析；
  塞本地路径会让 ``import pymilvus`` 直接崩 → 文件式后端用独立的 ``MILVUS_LITE_PATH``。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.infrastructure.vectorstore.milvus import resolve_uri


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """把 Milvus 相关变量**显式置空**。

    注意不能用 ``delenv``：项目 ``.env`` 里配了 ``MILVUS_HOST=localhost``（指向本机
    docker，实际没起），删掉环境变量会**回落到 .env**，于是"未配置"根本模拟不出来。
    """
    for k in ("MILVUS_LITE_PATH", "MILVUS_URI", "MILVUS_HOST"):
        monkeypatch.setenv(k, "")   # MILVUS_PORT 是 int，置空会触发校验错误，故不动
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lite_path_without_db_suffix_gets_normalized(monkeypatch):
    monkeypatch.setenv("MILVUS_LITE_PATH", "/tmp/milvus_lite")
    get_settings.cache_clear()
    assert resolve_uri() == "/tmp/milvus_lite.db"


def test_lite_path_with_db_suffix_unchanged(monkeypatch):
    monkeypatch.setenv("MILVUS_LITE_PATH", "/tmp/milvus_lite.db")
    get_settings.cache_clear()
    assert resolve_uri() == "/tmp/milvus_lite.db"


@pytest.mark.parametrize("raw,expected", [
    ("/tmp/mlv/", "/tmp/mlv.db"),
    ("/tmp/mlv.db/", "/tmp/mlv.db"),
    ("C:\\tmp\\mlv", "C:\\tmp\\mlv.db"),
    ("/tmp/MLV.DB", "/tmp/MLV.DB"),
])
def test_lite_path_trailing_separators_and_case(monkeypatch, raw, expected):
    monkeypatch.setenv("MILVUS_LITE_PATH", raw)
    get_settings.cache_clear()
    assert resolve_uri() == expected


def test_lite_path_takes_priority_over_uri_and_host(monkeypatch):
    monkeypatch.setenv("MILVUS_LITE_PATH", "/tmp/lite")
    monkeypatch.setenv("MILVUS_URI", "http://server:19530")
    monkeypatch.setenv("MILVUS_HOST", "server")
    get_settings.cache_clear()
    assert resolve_uri() == "/tmp/lite.db"


def test_uri_is_http_passthrough(monkeypatch):
    monkeypatch.setenv("MILVUS_URI", "http://milvus.internal:19530")
    get_settings.cache_clear()
    # http URI 不能被补 .db（否则就变成非法 URI）
    assert resolve_uri() == "http://milvus.internal:19530"


def test_host_port_backward_compatible(monkeypatch):
    monkeypatch.setenv("MILVUS_HOST", "localhost")
    monkeypatch.setenv("MILVUS_PORT", "19531")
    get_settings.cache_clear()
    assert resolve_uri() == "http://localhost:19531"


def test_unconfigured_returns_none(monkeypatch):
    get_settings.cache_clear()
    assert resolve_uri() is None
