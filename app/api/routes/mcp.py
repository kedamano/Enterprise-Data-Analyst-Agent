"""D50：MCP 接入层的 REST 面（无 MCP client 时也能验证）。

Spec: docs/specs/MCP/01-adapter.md §3

- `GET  /mcp/tools` —— 发现（暴露集 + schema + 权限 + data_scope）
- `POST /mcp/call`  —— 调用（走 `execute_tool`，鉴权/限流/审计全在里面）

`mcp_enabled` 默认 false → 两端点 **503**（配置未启用要吵，不静默空跑）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["mcp"])

_DISABLED = HTTPException(status_code=503, detail="MCP 接入层未启用（mcp_enabled=false）")


class McpCallBody(BaseModel):
    tool: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _enabled() -> bool:
    from ...config import get_settings

    return bool(getattr(get_settings(), "mcp_enabled", False))


def _import_mcp_server():
    """加载接入层；依赖未装时给**明确**的 503（而不是 500 ModuleNotFoundError）。"""
    try:
        from ...core.integrations import mcp_server
        return mcp_server
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=(
            f"MCP 接入层依赖缺失（{exc.name}）。请 `pip install .[mcp]` "
            f"（见 pyproject.toml 的 optional-dependencies.mcp）")) from exc


@router.get("/mcp/tools")
def list_mcp_tools():
    """发现：列出暴露的只读工具。"""
    if not _enabled():
        raise _DISABLED
    mod = _import_mcp_server()

    return {
        "enabled": True,
        "transport": "mcp（stdio / SSE / streamable HTTP）；本端点为 REST 发现面",
        "tools": mod.exposed_tools(),
    }


@router.post("/mcp/call")
def call_mcp_tool(body: McpCallBody):
    """调用一个暴露的只读工具。非暴露工具 → 403（不静默放行）。"""
    if not _enabled():
        raise _DISABLED
    mod = _import_mcp_server()
    exposed = mod.exposed_tool_names()

    if body.tool not in exposed:
        raise HTTPException(status_code=403, detail=(
            f"工具 {body.tool!r} 不暴露：MCP 只暴露只读工具"
            f"（{', '.join(sorted(exposed))}）"))
    return mod.invoke_tool(body.tool, body.arguments)
