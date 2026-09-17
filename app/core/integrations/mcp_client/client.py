"""MCP **客户端**：真正去连外部 MCP server，拉取工具清单（连接测试的唯一真相源）。

本轮只做"连得上吗 / 暴露了哪些工具"，**不把外部工具注册进 Agent 工具表**
（那是下一轮的事）。所以这里刻意保持"一次性会话"：连上 → initialize →
list_tools → 立刻断开。不持有长连接，避免 server 崩溃/僵死拖累主进程。

支持三种传输：
- ``stdio`` —— 本地子进程（`command` + `args` + `env`），如 ``cmd /c npx -y @modelcontextprotocol/server-time``
- ``sse``   —— 远端 Server-Sent Events（`url` + `headers`）
- ``http``  —— Streamable HTTP（`url` + `headers`）

超时是**必给的**：外部 server 冷启动（npx 现拉包）可能几十秒，但也绝不能无限等——
`mcp_client_timeout_s` 封顶，超时返回结构化失败而非挂死请求线程。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional


class MCPClientError(RuntimeError):
    """连接/握手类错误（与"server 返回了错误"区分，便于界面给不同提示）。"""


def _tool_views(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools or []:
        out.append({
            "name": str(getattr(t, "name", "") or ""),
            "description": str(getattr(t, "description", "") or ""),
            "input_schema": getattr(t, "inputSchema", None) or {},
        })
    return out


def _import_sdk():
    try:
        from mcp import ClientSession  # noqa: F401
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        raise MCPClientError(
            "MCP SDK 未安装。请 `pip install .[mcp]`（pyproject.toml 的 optional-dependencies.mcp）"
        ) from exc


async def _list_tools_async(server: dict[str, Any], timeout_s: float) -> list[dict[str, Any]]:
    _import_sdk()
    from mcp import ClientSession

    transport = str(server.get("transport") or "stdio").lower()

    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        if not server.get("command"):
            raise MCPClientError("stdio 传输缺少 command")
        # 继承父进程环境（否则 npx/node/cmd 找不到 PATH），再用用户配置覆盖
        env = {**os.environ, **(server.get("env") or {})}
        params = StdioServerParameters(
            command=str(server["command"]),
            args=[str(a) for a in (server.get("args") or [])],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tool_views(getattr(res, "tools", None))

    if transport == "sse":
        from mcp.client.sse import sse_client

        url = str(server.get("url") or "")
        if not url:
            raise MCPClientError("sse 传输缺少 url")
        headers = server.get("headers") or None
        async with sse_client(url, headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tool_views(getattr(res, "tools", None))

    if transport == "http":
        from mcp.client.streamable_http import streamablehttp_client

        url = str(server.get("url") or "")
        if not url:
            raise MCPClientError("http 传输缺少 url")
        headers = server.get("headers") or None
        async with streamablehttp_client(url, headers=headers) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.list_tools()
                return _tool_views(getattr(res, "tools", None))

    raise MCPClientError(f"不支持的 transport: {transport}")


def probe(server: dict[str, Any], timeout_s: Optional[float] = None) -> dict[str, Any]:
    """连接测试 + 工具发现（**同步**入口，供 FastAPI 同步路由在线程池里调用）。

    返回::

        {"ok": bool, "tools": [...], "tool_count": int,
         "error": str | None, "error_kind": str | None, "latency_ms": int,
         "target": str}

    **永不抛异常**——连接失败是"结果"，不是"服务端错误"。
    """
    if timeout_s is None:
        try:
            from ....config import get_settings

            timeout_s = float(getattr(get_settings(), "mcp_client_timeout_s", 30.0) or 30.0)
        except Exception:
            timeout_s = 30.0

    target = (f"{server.get('command')} {' '.join(server.get('args') or [])}".strip()
              if str(server.get("transport")) == "stdio" else str(server.get("url") or ""))
    started = time.time()

    async def _run() -> list[dict[str, Any]]:
        return await asyncio.wait_for(_list_tools_async(server, timeout_s), timeout=timeout_s)

    try:
        tools = asyncio.run(_run())
        return {
            "ok": True, "tools": tools, "tool_count": len(tools), "error": None,
            "error_kind": None, "latency_ms": int((time.time() - started) * 1000),
            "target": target,
        }
    except asyncio.TimeoutError:
        return {
            "ok": False, "tools": [], "tool_count": 0,
            "error": f"连接超时（>{timeout_s:.0f}s）——server 可能未启动或需要冷启动拉包",
            "error_kind": "timeout",
            "latency_ms": int((time.time() - started) * 1000), "target": target,
        }
    except (MCPClientError, ValueError) as exc:
        return {
            "ok": False, "tools": [], "tool_count": 0, "error": str(exc),
            "error_kind": "config",
            "latency_ms": int((time.time() - started) * 1000), "target": target,
        }
    except Exception as exc:  # noqa: BLE001 —— 连接失败一律结构化返回
        return {
            "ok": False, "tools": [], "tool_count": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": "connection",
            "latency_ms": int((time.time() - started) * 1000), "target": target,
        }
