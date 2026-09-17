"""直接调用 mcp_client.probe()，对本机最小 stdio MCP server 做真实握手。

不经 HTTP、不 mock：真正 spawn 子进程 → initialize → list_tools → disconnect。
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, r"D:\work\项目\Enterprise Data Analyst Agent")

from app.core.integrations.mcp_client import normalize_server, probe  # noqa: E402

PY = r"D:/work/项目/Enterprise Data Analyst Agent/.venv/Scripts/python.exe"
SERVER = r"D:/work/项目/Enterprise Data Analyst Agent/_e2e_mcp_demo_server.py"


def main() -> int:
    print("== A) 正常 stdio server ==")
    cfg = normalize_server({
        "name": "demo", "transport": "stdio", "command": PY, "args": [SERVER],
    })
    print("  normalize 结果:", json.dumps(cfg, ensure_ascii=False))
    res = probe(cfg)
    print("  probe:", json.dumps(res, ensure_ascii=False, indent=2)[:1200])
    tools = [t["name"] for t in res.get("tools", [])]
    ok_a = res["ok"] is True and {"ping", "echo"} <= set(tools)
    print("  握手成功且工具齐全:", ok_a, tools)

    print("== B) 不存在的命令 → 结构化失败（不抛异常）==")
    bad = normalize_server({
        "name": "bad", "transport": "stdio", "command": "no-such-binary-xyz", "args": [],
    })
    res2 = probe(bad)
    print("  probe:", json.dumps(res2, ensure_ascii=False)[:500])
    ok_b = res2["ok"] is False and bool(res2.get("error_kind"))
    print("  结构化失败:", ok_b)

    print("== C) 非法 transport 应被 normalize 拦截 ==")
    try:
        normalize_server({"name": "x", "transport": "carrier-pigeon"})
        print("  未拦截 ✗")
        ok_c = False
    except ValueError as exc:
        print("  已拦截 ✓:", exc)
        ok_c = True

    print("== D) redact 掩码密钥 ==")
    from app.core.integrations.mcp_client import redact
    masked = redact({
        "name": "s", "transport": "stdio", "command": "x", "args": [], "url": "",
        "env": {"API_KEY": "sk-super-secret-value"},
        "headers": {"Authorization": "Bearer abcdef123456"},
    })
    blob = json.dumps(masked, ensure_ascii=False)
    ok_d = "sk-super-secret-value" not in blob and "abcdef123456" not in blob
    print("  redact:", blob)
    print("  密钥已掩码:", ok_d)

    allok = ok_a and ok_b and ok_c and ok_d
    print("\n== 结论:", "全部通过" if allok else "存在失败", "==")
    return 0 if allok else 2


if __name__ == "__main__":
    raise SystemExit(main())
