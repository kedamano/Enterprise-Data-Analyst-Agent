"""最小 stdio MCP server —— 用来**离线验证** MCP 客户端接入层（不用联网拉 npx 包）。

用途：在「MCP 服务器」页新增一条 stdio 配置，填

    command: <项目根>/.venv/Scripts/python.exe
    args   : scripts/mcp_demo_server.py

点「测试连接」应返回 2 个工具（`ping` / `echo`）。这条链路能验到：
子进程启动 → MCP initialize 握手 → list_tools → 结构化回传（ok/tools/latency_ms）。

⚠️ 本仓 `.venv` 装的是 **mcp 2.x**：`FastMCP` 已改名 `MCPServer`
（`from mcp.server.mcpserver import MCPServer`）。用旧 import 会 `ModuleNotFoundError`，
报错信息里会直接给迁移提示。`run(transport="stdio")` 签名不变。

单独手工调试： ``.venv/Scripts/python.exe scripts/mcp_demo_server.py``
（stdio 下会静默等待 stdin 上的 JSON-RPC，属正常现象，Ctrl+C 退出。）
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

server = MCPServer("da-demo")


@server.tool()
def ping(value: int) -> int:
    """把传入的整数加一返回（连通性探针）。"""
    return value + 1


@server.tool()
def echo(text: str) -> str:
    """原样回显文本（验证参数与返回值能正确往返）。"""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
