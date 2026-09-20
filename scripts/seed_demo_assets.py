"""为演示/截图准备示例数据：3 个示例技能 + 一条可连通的最小 MCP server 配置。

幂等：已存在的同名技能 / server 会跳过，可重复执行。

用法（需后端已在 8000 端口运行）::

    .venv/Scripts/python.exe scripts/seed_demo_assets.py
    # 自定义地址
    DA_BASE=http://127.0.0.1:9000/api/v1 .venv/Scripts/python.exe scripts/seed_demo_assets.py

它同时也是「技能 / MCP 客户端」两组接口的最小调用范例：
- `POST /skills`            —— 名称 + 描述 + 正文（Markdown）
- `POST /mcp/servers`       —— stdio 配置（command + args）
- `POST /mcp/servers/{id}/test` —— 连接测试 + 拉取工具清单
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request

BASE = os.environ.get("DA_BASE", "http://127.0.0.1:8000/api/v1")
ROOT = pathlib.Path(__file__).resolve().parent.parent  # 项目根（本文件在 scripts/ 下）

SKILLS = [
    {
        "name": "经营指标口径统一规范",
        "description": "锁定 GMV / 营收 / 活跃的口径边界，避免同一次分析里多种口径混用。",
        "body": """## 口径定义（本组织唯一权威）

| 指标 | 口径 | 排除项 |
|---|---|---|
| GMV | 下单金额合计，含未支付 | — |
| 营收 | **已支付**且未退款金额 | 未支付、已退款、测试单 |
| 活跃用户 | 自然日内有 ≥1 次有效会话 | 内部账号（user_id < 1000） |

## 输出约束

1. **每个金额类结论必须标注口径**（如「营收（已支付口径）」），不得裸写「营收」。
2. 涉及 GMV 与营收同时出现时，必须说明两者差额来自未支付/退款。
3. 口径与上表不一致时，**先说明差异再给结论**，不要静默替换。
""",
    },
    {
        "name": "同比环比表述规范",
        "description": "统一 YoY/QoQ 的基期选择与话术，禁止在样本不足时给出趋势判断。",
        "body": """## 基期选择

- **同比（YoY）**：与去年同周期比；基期缺失 → 标「无同比」而非填 0。
- **环比（MoM/QoQ）**：与紧邻上一周期比；周期含不完整月份时必须在报告中注明。

## 话术红线

- 基期数据行数 < 30 → 结论必须写「样本不足，趋势不具统计意义」。
- 变化幅度 < 5% 且无显著性检验 → 只用「基本持平」，不得写「显著增长/下滑」。
- 任何趋势结论都要带一句**可能的口径变化干扰**说明。
""",
    },
    {
        "name": "异常值与稳健统计",
        "description": "均值被极值带偏时改用中位数；去极值必须声明方法且同时给出原始口径。",
        "body": """## 处理规则

1. 分布明显右偏（均值 > 中位数 1.5 倍）→ **同时给中位数**，不只给均值。
2. 需要去极值时用 **IQR（1.5 倍四分位距）**，并注明剔除条数。
3. 去极值后的结论必须与原始口径**并列展示**，让读者看到差异。

## 禁止

- 禁止静默截尾（如用户未要求的 `clip` / `quantile(0.99)`）。
- 禁止在样本 < 20 条时使用去极值（剔除后剩不下什么）。
""",
    },
]

MCP_SERVER = {
    "name": "da-demo (本地 stdio)",
    "transport": "stdio",
    "command": str(ROOT / ".venv" / "Scripts" / "python.exe"),
    "args": ["scripts/mcp_demo_server.py"],
    "enabled": True,
    "description": "项目自带的最小 stdio MCP server，用于离线验证「客户端接入层」全链路"
                   "（不依赖 npx 联网拉包）。",
}


def req(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode())


def main() -> None:
    _, existing = req("GET", "/skills")
    names = {s["name"] for s in existing.get("skills", [])}
    print(f"现有技能 {len(names)} 个")
    for s in SKILLS:
        if s["name"] in names:
            print(f"  跳过（已存在）: {s['name']}")
            continue
        try:
            status, body = req("POST", "/skills", s)
            print(f"  创建 [{status}] {body.get('name')} id={body.get('id')}")
        except urllib.error.HTTPError as e:
            print(f"  创建失败 {s['name']}: {e.code} {e.read().decode()[:200]}")

    _, cur = req("GET", "/mcp/servers")
    have = {x["name"] for x in cur.get("servers", [])}
    if MCP_SERVER["name"] in have:
        print(f"  跳过（已存在）: {MCP_SERVER['name']}")
        return
    try:
        status, body = req("POST", "/mcp/servers", MCP_SERVER)
        print(f"  创建 MCP [{status}] id={body.get('id')}")
        sid = body.get("id")
        status, probe = req("POST", f"/mcp/servers/{sid}/test")
        print(f"  连接测试 [{status}] ok={probe.get('ok')} tools={probe.get('tool_count')} "
              f"latency={probe.get('latency_ms')}ms error={probe.get('error')}")
        for t in probe.get("tools", []):
            print(f"    - {t['name']}: {t['description']}")
    except urllib.error.HTTPError as e:
        print(f"  创建 MCP 失败: {e.code} {e.read().decode()[:300]}")


if __name__ == "__main__":
    main()
