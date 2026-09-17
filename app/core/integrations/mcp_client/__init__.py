"""MCP **客户端**子模块：管理"我们要去连的"外部 MCP server（配置 + 连接测试 + 工具发现）。

与 `../mcp_server.py`（把我们的工具**暴露出去**）方向相反，见 `store.py` 顶部对照表。
"""
from __future__ import annotations

from .client import MCPClientError, probe
from .store import MCPServerStore, get_mcp_server_store, normalize_server, redact

__all__ = [
    "MCPServerStore",
    "get_mcp_server_store",
    "normalize_server",
    "redact",
    "probe",
    "MCPClientError",
]
