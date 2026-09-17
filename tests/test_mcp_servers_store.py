"""MCP 客户端配置存储契约（CRUD / 校验 / 原子持久化 / 敏感字段遮蔽）。"""
from __future__ import annotations

import json

import pytest

from app.core.integrations.mcp_client.store import (
    MCPServerStore,
    normalize_server,
    redact,
)


@pytest.fixture
def store(tmp_path) -> MCPServerStore:
    return MCPServerStore(tmp_path / "mcp_servers.json")


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #
def test_normalize_stdio_requires_command():
    with pytest.raises(ValueError):
        normalize_server({"name": "x", "transport": "stdio"})


def test_normalize_remote_requires_url():
    with pytest.raises(ValueError):
        normalize_server({"name": "x", "transport": "sse"})
    with pytest.raises(ValueError):
        normalize_server({"name": "x", "transport": "http"})


def test_normalize_rejects_unknown_transport():
    with pytest.raises(ValueError):
        normalize_server({"name": "x", "transport": "carrier-pigeon", "url": "u"})


def test_normalize_requires_name():
    with pytest.raises(ValueError):
        normalize_server({"transport": "stdio", "command": "cmd"})


def test_normalize_accepts_multiline_args_and_env():
    cfg = normalize_server({
        "name": "time",
        "transport": "stdio",
        "command": "cmd",
        "args": "/c\nnpx\n-y\n@modelcontextprotocol/server-time",
        "env": "API_KEY=abc\nDEBUG=1",
    })
    assert cfg["args"] == ["/c", "npx", "-y", "@modelcontextprotocol/server-time"]
    assert cfg["env"] == {"API_KEY": "abc", "DEBUG": "1"}


def test_normalize_clears_irrelevant_fields():
    cfg = normalize_server({"name": "r", "transport": "sse", "url": "http://x", "command": "cmd",
                            "args": ["a"], "env": {"K": "v"}})
    assert cfg["command"] == "" and cfg["args"] == [] and cfg["env"] == {}


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_list_get(store: MCPServerStore):
    item = store.create({"name": "time", "transport": "stdio", "command": "cmd", "args": ["/c", "npx"]})
    assert item["id"] == "time"
    assert item["created_at"] and item["updated_at"]
    assert store.get("time")["command"] == "cmd"
    assert len(store.list()) == 1


def test_create_duplicate_name_gets_unique_id(store: MCPServerStore):
    a = store.create({"name": "time", "transport": "stdio", "command": "cmd"})
    b = store.create({"name": "time", "transport": "stdio", "command": "cmd"})
    assert a["id"] != b["id"]


def test_update_merges_partial(store: MCPServerStore):
    store.create({"name": "s1", "transport": "stdio", "command": "cmd", "args": ["a"]})
    upd = store.update("s1", {"args": ["b", "c"]})
    assert upd["args"] == ["b", "c"]
    assert upd["command"] == "cmd"  # 未传则保留
    assert upd["name"] == "s1"


def test_update_unknown_returns_none(store: MCPServerStore):
    assert store.update("ghost", {"name": "x"}) is None


def test_set_enabled_and_delete(store: MCPServerStore):
    store.create({"name": "s2", "transport": "stdio", "command": "cmd"})
    assert store.set_enabled("s2", False)["enabled"] is False
    assert store.get("s2")["enabled"] is False
    assert store.delete("s2") is True
    assert store.delete("s2") is False


def test_persistence_across_instances(store: MCPServerStore, tmp_path):
    store.create({"name": "s3", "transport": "http", "url": "http://example/sse"})
    again = MCPServerStore(tmp_path / "mcp_servers.json")
    assert [s["id"] for s in again.list()] == ["s3"]


def test_file_shape_is_stable(store: MCPServerStore):
    store.create({"name": "s4", "transport": "stdio", "command": "cmd"})
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and "servers" in raw
    assert raw["servers"][0]["name"] == "s4"


def test_corrupt_file_degrades_to_empty(store: MCPServerStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.list() == []


# --------------------------------------------------------------------------- #
# redact
# --------------------------------------------------------------------------- #
def test_redact_masks_secret_env_and_headers():
    out = redact({
        "name": "x",
        "env": {"API_KEY": "secret-value", "DEBUG": "1"},
        "headers": {"Authorization": "Bearer abc", "X-Trace": "t"},
    })
    assert out["env"]["API_KEY"] == "***"
    assert out["env"]["DEBUG"] == "1"
    assert out["headers"]["Authorization"] == "***"
    assert out["headers"]["X-Trace"] == "t"
