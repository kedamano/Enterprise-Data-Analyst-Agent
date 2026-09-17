"""MCP **客户端管理** REST 接口 —— 管理"我们要去连的"外部 MCP server。

Spec: `docs/对标企业级Gap.md` §八（MCP 一行只覆盖了"出"的方向，本卡补"入"的方向）。

- `GET    /mcp/servers`             —— 已配置的外部 server 清单
- `POST   /mcp/servers`             —— 新增（transport/command/args/env 或 url/headers）
- `PUT    /mcp/servers/{id}`        —— 更新（局部字段合并）
- `DELETE /mcp/servers/{id}`        —— 删除
- `POST   /mcp/servers/{id}/enabled`—— 启停
- `POST   /mcp/servers/test`        —— **未保存前**测试一份配置（界面"测试连接"按钮）
- `POST   /mcp/servers/{id}/test`   —— 测试已保存的 server
- `GET    /mcp/servers/{id}/tools`  —— 拉取该 server 暴露的工具清单

与既有的 `/mcp/tools`、`/mcp/call`（**服务端**方向：把我们的只读工具暴露出去）**互不冲突**，
路径前缀不同。`mcp_servers_enabled=false` 时统一 503。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/mcp/servers", tags=["mcp"])

_DISABLED = HTTPException(
    status_code=503, detail="MCP 客户端管理未启用（mcp_servers_enabled=false）")


class MCPServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    transport: str = Field(default="stdio", description="stdio | sse | http")
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    description: str = ""


class MCPServerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None


class EnabledBody(BaseModel):
    enabled: bool = True


def _enabled() -> bool:
    from ...config import get_settings

    return bool(getattr(get_settings(), "mcp_servers_enabled", True))


def _store():
    from ...core.integrations.mcp_client import get_mcp_server_store

    return get_mcp_server_store()


@router.get("")
def list_servers() -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    servers = _store().list()
    return {"servers": servers, "total": len(servers)}


@router.post("", status_code=201)
def create_server(req: MCPServerCreate) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    try:
        return _store().create(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/test")
def test_ad_hoc(req: MCPServerCreate) -> dict[str, Any]:
    """测试一份**尚未保存**的配置（界面里"测试连接"先于"保存"）。"""
    if not _enabled():
        raise _DISABLED
    from ...core.integrations.mcp_client import normalize_server, probe

    try:
        cfg = normalize_server(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return probe(cfg)


@router.get("/{server_id}")
def get_server(server_id: str) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    item = _store().get(server_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    return item


@router.put("/{server_id}")
def update_server(server_id: str, req: MCPServerUpdate) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        item = _store().update(server_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    return item


@router.delete("/{server_id}")
def delete_server(server_id: str) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    if not _store().delete(server_id):
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    return {"ok": True, "id": server_id}


@router.post("/{server_id}/enabled")
def set_enabled(server_id: str, body: EnabledBody) -> dict[str, Any]:
    if not _enabled():
        raise _DISABLED
    item = _store().set_enabled(server_id, body.enabled)
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    return item


@router.post("/{server_id}/test")
def test_saved(server_id: str) -> dict[str, Any]:
    """连接测试：连上 → initialize → list_tools → 断开。返回工具清单与耗时。"""
    if not _enabled():
        raise _DISABLED
    from ...core.integrations.mcp_client import probe

    item = _store().get(server_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    return probe(item)


@router.get("/{server_id}/tools")
def list_server_tools(server_id: str) -> dict[str, Any]:
    """拉取该 server 暴露的工具清单（连接测试的只读版本）。"""
    if not _enabled():
        raise _DISABLED
    from ...core.integrations.mcp_client import probe

    item = _store().get(server_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_id}")
    result = probe(item)
    return {
        "server_id": server_id,
        "ok": result["ok"],
        "tools": result["tools"],
        "tool_count": result["tool_count"],
        "error": result["error"],
        "latency_ms": result["latency_ms"],
    }
