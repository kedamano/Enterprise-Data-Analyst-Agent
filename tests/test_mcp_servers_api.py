"""MCP 客户端管理 REST 接口契约。

`probe`（真正去连外部 server）用 monkeypatch 替换——接口契约与"能不能连上"无关，
不该让测试依赖本机是否装了 npx / 有没有外网。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "mcp_servers_path", str(tmp_path / "mcp_servers.json"))
    monkeypatch.setattr(s, "mcp_servers_enabled", True)
    from app.main import app

    return TestClient(app)


def _stub_probe(monkeypatch, result):
    def fake_probe(server, timeout_s=None):
        return result

    monkeypatch.setattr("app.core.integrations.mcp_client.probe", fake_probe)


OK_RESULT = {
    "ok": True,
    "tools": [{"name": "get_time", "description": "返回当前时间", "input_schema": {}}],
    "tool_count": 1,
    "error": None,
    "error_kind": None,
    "latency_ms": 12,
    "target": "cmd /c npx",
}

FAIL_RESULT = {
    "ok": False,
    "tools": [],
    "tool_count": 0,
    "error": "连接超时（>30s）",
    "error_kind": "timeout",
    "latency_ms": 30000,
    "target": "cmd /c npx",
}


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_list_get(client):
    r = client.post("/api/v1/mcp/servers", json={
        "name": "time", "transport": "stdio", "command": "cmd", "args": ["/c", "npx"]})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    lst = client.get("/api/v1/mcp/servers").json()
    assert lst["total"] == 1 and lst["servers"][0]["id"] == sid

    got = client.get(f"/api/v1/mcp/servers/{sid}").json()
    assert got["command"] == "cmd" and got["args"] == ["/c", "npx"]


def test_create_stdio_without_command_is_400(client):
    r = client.post("/api/v1/mcp/servers", json={"name": "x", "transport": "stdio"})
    assert r.status_code == 400


def test_create_remote_without_url_is_400(client):
    r = client.post("/api/v1/mcp/servers", json={"name": "x", "transport": "sse"})
    assert r.status_code == 400


def test_update_enabled_delete(client):
    sid = client.post("/api/v1/mcp/servers", json={
        "name": "s", "transport": "http", "url": "http://example/sse"}).json()["id"]

    r = client.put(f"/api/v1/mcp/servers/{sid}", json={"url": "http://example/v2"})
    assert r.status_code == 200 and r.json()["url"] == "http://example/v2"

    r = client.post(f"/api/v1/mcp/servers/{sid}/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    assert client.delete(f"/api/v1/mcp/servers/{sid}").json()["ok"] is True
    assert client.get(f"/api/v1/mcp/servers/{sid}").status_code == 404


def test_unknown_id_404(client):
    assert client.get("/api/v1/mcp/servers/nope").status_code == 404
    assert client.put("/api/v1/mcp/servers/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/v1/mcp/servers/nope").status_code == 404
    assert client.post("/api/v1/mcp/servers/nope/test").status_code == 404


# --------------------------------------------------------------------------- #
# 连接测试
# --------------------------------------------------------------------------- #
def test_test_saved_server_returns_probe_result(client, monkeypatch):
    sid = client.post("/api/v1/mcp/servers", json={
        "name": "time", "transport": "stdio", "command": "cmd"}).json()["id"]
    _stub_probe(monkeypatch, OK_RESULT)

    r = client.post(f"/api/v1/mcp/servers/{sid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["tool_count"] == 1
    assert body["tools"][0]["name"] == "get_time"


def test_test_saved_server_failure_is_200_with_ok_false(client, monkeypatch):
    """连接失败是**结果**，不是服务端错误——必须 200 + ok=false，界面才能显示原因。"""
    sid = client.post("/api/v1/mcp/servers", json={
        "name": "dead", "transport": "stdio", "command": "cmd"}).json()["id"]
    _stub_probe(monkeypatch, FAIL_RESULT)

    r = client.post(f"/api/v1/mcp/servers/{sid}/test")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "超时" in r.json()["error"]


def test_test_ad_hoc_config(client, monkeypatch):
    _stub_probe(monkeypatch, OK_RESULT)
    r = client.post("/api/v1/mcp/servers/test", json={
        "name": "tmp", "transport": "stdio", "command": "cmd"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_test_ad_hoc_invalid_config_is_400(client):
    r = client.post("/api/v1/mcp/servers/test", json={"name": "tmp", "transport": "stdio"})
    assert r.status_code == 400


def test_tools_endpoint(client, monkeypatch):
    sid = client.post("/api/v1/mcp/servers", json={
        "name": "time", "transport": "stdio", "command": "cmd"}).json()["id"]
    _stub_probe(monkeypatch, OK_RESULT)
    r = client.get(f"/api/v1/mcp/servers/{sid}/tools")
    assert r.status_code == 200
    assert r.json()["tools"][0]["name"] == "get_time"


# --------------------------------------------------------------------------- #
# 与既有 MCP 服务端方向共存 + 开关
# --------------------------------------------------------------------------- #
def test_coexists_with_mcp_server_exposure_routes(client):
    """新增的客户端管理面不能顶掉 D50 的暴露面（/mcp/tools、/mcp/call 仍在）。"""
    from app.main import app

    paths = set(app.openapi()["paths"].keys())
    assert "/api/v1/mcp/tools" in paths
    assert "/api/v1/mcp/call" in paths
    assert "/api/v1/mcp/servers" in paths


def test_disabled_gate_returns_503(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mcp_servers_enabled", False)
    assert client.get("/api/v1/mcp/servers").status_code == 503
    assert client.post("/api/v1/mcp/servers", json={"name": "x", "transport": "http", "url": "u"}).status_code == 503
