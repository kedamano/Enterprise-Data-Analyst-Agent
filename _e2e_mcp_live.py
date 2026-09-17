"""Live E2E：MCP 客户端管理（配置 CRUD + 连接测试 + 工具清单）。

用一个真实的最小 stdio MCP server（_e2e_mcp_demo_server.py）验证：
  1. POST /mcp/servers/test        未保存配置的连接测试（界面"测试连接"）
  2. POST /mcp/servers             保存
  3. POST /mcp/servers/{id}/test   已保存 server 的连接测试
  4. GET  /mcp/servers/{id}/tools  工具清单
  5. 坏配置 → ok=false 且 error_kind 有值（probe 不抛异常）
  6. DELETE 清理
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
PY = r"D:/work/项目/Enterprise Data Analyst Agent/.venv/Scripts/python.exe"
SERVER = r"D:/work/项目/Enterprise Data Analyst Agent/_e2e_mcp_demo_server.py"


def call(method: str, path: str, payload: dict | None = None, timeout: int = 120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def show(label: str, value) -> None:
    print(f"  {label}: {json.dumps(value, ensure_ascii=False)[:600]}")


def main() -> int:
    good = {
        "name": "e2e-demo-stdio",
        "transport": "stdio",
        "command": PY,
        "args": [SERVER],
        "description": "live 验证用最小 MCP server",
    }

    print("== 1) 基线清单 ==")
    st, body = call("GET", "/mcp/servers")
    print("  GET /mcp/servers ->", st)
    show("servers", body)
    if st != 200:
        return 1
    before = body["total"]

    print("== 2) 未保存配置的连接测试 ==")
    st, res = call("POST", "/mcp/servers/test", good)
    print("  POST /mcp/servers/test ->", st)
    show("probe", res)
    tools = [t.get("name") for t in (res or {}).get("tools", [])] if isinstance(res, dict) else []
    print("  工具名:", tools)
    ok_probe = isinstance(res, dict) and res.get("ok") is True and "ping" in tools
    print("  连接测试通过并列出 ping:", ok_probe)

    print("== 3) 坏配置（不存在的命令）→ 结构化失败 ==")
    st, bad = call("POST", "/mcp/servers/test", {
        "name": "e2e-bad", "transport": "stdio",
        "command": "definitely-not-a-real-binary-xyz", "args": [],
    })
    print("  ->", st)
    show("probe", bad)
    ok_bad = isinstance(bad, dict) and bad.get("ok") is False and bad.get("error_kind")
    print("  失败被结构化返回（未抛异常）:", ok_bad)

    server_id = None
    try:
        print("== 4) 保存 + 已保存 server 测试 ==")
        st, saved = call("POST", "/mcp/servers", good)
        print("  POST /mcp/servers ->", st)
        show("saved", saved)
        if st != 201:
            return 1
        server_id = saved["id"]

        st, res2 = call("POST", f"/mcp/servers/{server_id}/test")
        print("  POST /{id}/test ->", st)
        show("probe", res2)

        st, res3 = call("GET", f"/mcp/servers/{server_id}/tools")
        print("  GET /{id}/tools ->", st)
        show("tools", res3)

        st, body2 = call("GET", "/mcp/servers")
        print("  清单 total:", before, "->", body2["total"])

        ok_saved = (isinstance(res3, dict) and res3.get("ok") is True
                    and res3.get("tool_count", 0) >= 2)
        print("  已保存 server 工具数 >=2:", ok_saved)
        return 0 if (ok_probe and ok_bad and ok_saved) else 2
    finally:
        if server_id:
            print("== 5) 清理 ==")
            st, body3 = call("DELETE", f"/mcp/servers/{server_id}")
            print("  DELETE ->", st, body3)


if __name__ == "__main__":
    sys.exit(main())
