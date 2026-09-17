"""一个最小的 stdio MCP server，仅用于验证 MCP 客户端接入层。

注意：本仓 .venv 装的是 **mcp 2.x**，`FastMCP` 已改名 `MCPServer`
（`from mcp.server.mcpserver import MCPServer`），`run(transport="stdio")` 签名不变。

运行： python _e2e_mcp_demo_server.py
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("e2e-demo")


@server.tool()
def ping(value: int) -> int:
    """把传入的整数加一返回。"""
    return value + 1


@server.tool()
def echo(text: str) -> str:
    """原样回显文本。"""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
