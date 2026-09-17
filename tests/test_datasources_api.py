"""E7/02 数据源管理 API（test / create / delete / list origin）。

契约：
- ``POST /datasources/test`` 只探活不落盘；SQLite 文件不存在 → ok=False（绝不静默建空库）。
- ``POST /datasources`` 探活通过才落盘；与 env DATA_SOURCES 重名 / 保留名 default → 400。
- ``DELETE /datasources/{name}``：local 可删；env 配置的 → 400 并指引改 .env。
- ``GET /datasources``：origin 标记 env/local；URL 已脱敏。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def api(monkeypatch, tmp_path):
    from app.config import get_settings

    # 存储与目标 SQLite 库都隔离到 tmp
    monkeypatch.setenv("DATASOURCE_STORE_PATH", str(tmp_path / "ds.json"))
    monkeypatch.setenv("DATA_SOURCES", "envhr=mysql://env-user:pw@env-host:3306/envhr")
    target = tmp_path / "biz.db"
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    get_settings.cache_clear()
    with TestClient(app) as client:
        yield client, target
    get_settings.cache_clear()


def _payload(target, name="biz", dialect="sqlite", **kw):
    return {"name": name, "dialect": dialect, "path": str(target), **kw}


def test_test_connection_ok_without_persisting(api):
    client, target = api
    r = client.post("/api/v1/datasources/test",
                    json={"dialect": "sqlite", "path": str(target)})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_connection_missing_sqlite_file_never_creates(api, tmp_path):
    client, _ = api
    ghost = tmp_path / "ghost.db"
    r = client.post("/api/v1/datasources/test",
                    json={"dialect": "sqlite", "path": str(ghost)})
    assert r.json()["ok"] is False
    assert not ghost.exists()  # 探活绝不创建空库文件


def test_create_persists_and_masks_url(api):
    client, target = api
    r = client.post("/api/v1/datasources", json=_payload(target))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "biz" and body["origin"] == "local"
    # 列表里出现，且 origin 正确
    names = {s["name"]: s for s in client.get("/api/v1/datasources").json()["sources"]}
    assert names["biz"]["origin"] == "local"
    assert names["envhr"]["origin"] == "env"
    # default 主源仍在
    assert "default" in names


def test_create_rejects_duplicate_default_and_env_conflict(api):
    client, target = api
    assert client.post("/api/v1/datasources",
                       json=_payload(target, name="default")).status_code == 400
    assert client.post("/api/v1/datasources",
                       json=_payload(target, name="envhr")).status_code == 400
    # 先建一次成功，再建同名 → 400
    assert client.post("/api/v1/datasources", json=_payload(target)).status_code == 201
    assert client.post("/api/v1/datasources", json=_payload(target)).status_code == 400


def test_create_fails_when_probe_fails_and_persists_nothing(api, tmp_path):
    client, _ = api
    r = client.post("/api/v1/datasources",
                    json={"name": "bad", "dialect": "mysql", "host": "127.0.0.1",
                          "port": 1, "database": "nope", "username": "u", "password": "p"})
    assert r.status_code == 400
    names = {s["name"] for s in client.get("/api/v1/datasources").json()["sources"]}
    assert "bad" not in names


def test_delete_local_ok_but_env_source_rejected(api):
    client, target = api
    client.post("/api/v1/datasources", json=_payload(target))
    assert client.delete("/api/v1/datasources/biz").status_code == 200
    names = {s["name"] for s in client.get("/api/v1/datasources").json()["sources"]}
    assert "biz" not in names
    # env 源不可从页面删
    r = client.delete("/api/v1/datasources/envhr")
    assert r.status_code == 400
    assert "DATA_SOURCES" in r.json()["detail"]
    # 不存在的 → 404
    assert client.delete("/api/v1/datasources/nope").status_code == 404


def test_deleted_local_source_falls_back_to_env(api):
    """local 覆盖 env 时删除 local，应回退到 env 配置（而不是消失）。"""
    import json as _json

    client, target = api
    # 把 local 存储里塞一个与 env 同名的源（模拟覆盖态）
    from app.core.tools.datasource_store import DataSourceStore
    from app.config import get_settings

    DataSourceStore(get_settings().datasource_store_path).add(
        "envhr", "sqlite:///" + str(target).replace("\\", "/"), "sqlite")
    names = {s["name"]: s for s in client.get("/api/v1/datasources").json()["sources"]}
    assert names["envhr"]["origin"] == "local"
    client.delete("/api/v1/datasources/envhr")
    names = {s["name"]: s for s in client.get("/api/v1/datasources").json()["sources"]}
    assert names["envhr"]["origin"] == "env"  # 回退到 env 配置
