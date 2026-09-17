"""D50：MCP 接入层 —— 把**只读**工具按 MCP 协议暴露（可发现 / 可授权 / 可观测）。

Spec: docs/specs/MCP/01-adapter.md

缺口（`docs/对标企业级Gap.md` 八）：工具全是进程内函数，外部 Agent
（Claude Desktop / Cursor / 任意 MCP client）够不着。

三支柱
------
1. **可发现** —— `list_tools()` 返回 name/description/input_schema。
   注意：**函数签名就是 schema 声明**——MCP 2.x 从函数签名推导 input_schema，
   无法从外部注入。所以下方 `_mcp_*` 包装函数的签名**必须与 `TOOL_SPECS` 的
   `properties` 一致**（含 `required`＝无默认值），`tests/test_mcp_server.py`
   的 `test_mcp_schema_pinned_to_tool_specs` 钉住两者，防止签名漂移。
   描述与 `read_only_hint` 注解**取自** `TOOL_SPECS`，不另写。
2. **可授权** —— 每个调用都走 `execute_tool()`，那里已经有完整安全链：
   **RBAC 门禁（AUTH/01 §6）→ 速率限制 → 只读守卫 → 执行 → 输出管控 → 审计**。
   接入层不复制其中任何一环；权限不足 → `status=FAILED` → MCP `isError`。
3. **可观测** —— `execute_tool` 已写 `data/audit/tool_audit.jsonl`
   （成功失败皆记）。权限被拒的调用**同样落审计**（否则"谁在探边界"无从复盘，
   同 AUTH/01 / D45 的取舍）。

暴露边界（**宁可少暴露，不可多暴露**）
--------------------------------------
暴露判定：`permission ∈ {READ_METADATA, READ_KNOWLEDGE, READ_DATA}`，从 `TOOL_SPECS`
**程序化推导**（新增只读工具自动进入，接入层不必改）。
`python_analysis`（任意代码执行面）/`visualization`/`generate_report` **绝不暴露**；
`image_analyze` 权限词表上是 READ_DATA 但耦合附件挂载 + 视觉模型成本，
暴露只会制造一个永远失败的端点 → 显式排除并留档。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ...config import get_settings
from ..tools import execute_tool
from ..tools.specs import TOOL_SPECS, ToolPermission

_READ_ONLY = {
    ToolPermission.READ_METADATA,
    ToolPermission.READ_KNOWLEDGE,
    ToolPermission.READ_DATA,
}
# 权限词表上是 READ_DATA，但实现耦合附件挂载（`_session_id` → 会话工作目录）
# 且调用视觉模型（有成本）。MCP client 传文件名在无附件会话里必然落空。
_EXCLUDED = frozenset({"image_analyze"})
_READ_ONLY_HINT = ToolAnnotations(read_only_hint=True)
_INSTRUCTIONS = (
    "企业数据分析 Agent 的**只读**工具集：schema 检索、知识库检索、数据画像、"
    "只读 SQL、自由写码 SQL。所有查询经只读守卫（禁 DML/DDL），"
    "并按调用方角色做 RBAC 门禁与审计。不含代码执行与产物生成工具。"
)


def exposed_tool_names() -> frozenset[str]:
    """暴露给 MCP 的工具名（只读权限，排除耦合附件的 image_analyze）。"""
    return frozenset(
        name for name, spec in TOOL_SPECS.items()
        if spec.permission in _READ_ONLY and name not in _EXCLUDED
    )


def exposed_tools() -> list[dict]:
    """发现面：直接从 `TOOL_SPECS` 取（权威，无推导）。"""
    out: list[dict] = []
    for name in sorted(exposed_tool_names()):
        spec = TOOL_SPECS[name]
        out.append({
            "name": name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "permission": spec.permission.value,
            "data_scope": spec.data_scope,
            "timeout_s": spec.timeout_s,
            "rate_limit_per_min": spec.rate_limit_per_min,
            "read_only_hint": True,
        })
    return out


def _session_id() -> str:
    """归集用 session_id（供限流/审计）。**不伪造真实会话**。"""
    return str(getattr(get_settings(), "mcp_session_id", "") or "mcp")


def invoke_tool(tool: str, arguments: Optional[dict] = None) -> dict:
    """核心调用：走 `execute_tool`（RBAC / 限流 / 守卫 / 审计全在里面）。

    接入层不做任何安全判定，只把 `ToolResult` 摊平成可序列化的 dict。
    """
    result = execute_tool(f"mcp-{tool}-{uuid.uuid4().hex[:8]}", tool,
                          dict(arguments or {}), _session_id())
    return {
        "step_id": result.step_id,
        "tool": result.tool,
        "status": result.status,
        "error": result.error,
        "error_class": result.error_class,
        "attempts": result.attempts,
        "execution_time_ms": result.execution_time_ms,
        "output": result.output,
        "artifacts": list(result.artifacts or []),
    }


def result_to_mcp(invoke_out: dict) -> CallToolResult:
    """MCP 里工具失败是**结果**（`is_error`）而非**异常**。"""
    ok = invoke_out.get("status") == "SUCCESS"
    text = json.dumps(invoke_out, ensure_ascii=False, default=str)
    return CallToolResult(content=[TextContent(type="text", text=text)],
                          is_error=not ok)


def _run(tool: str, **params: Any) -> CallToolResult:
    return result_to_mcp(invoke_tool(tool, params))


# --------------------------------------------------------------------------- #
# 显式签名（**必须与 TOOL_SPECS 的 properties / required 一致**）
#
# MCP 2.x 从函数签名推导 input_schema 并据此校验入参，无法从外部注入 schema。
# 所以签名与 TOOL_SPECS 必须保持一致——`test_mcp_schema_pinned_to_tool_specs`
# 会比对两者的 properties 与 required，签名漂移即测试失败。
# --------------------------------------------------------------------------- #
def mcp_schema_search(query: str = "", keyword: str = "",
                      entities: Optional[list[str]] = None) -> CallToolResult:
    return _run("schema_search", query=query, keyword=keyword, entities=entities)


def mcp_knowledge_search(query: str, top_k: int = 5) -> CallToolResult:
    return _run("knowledge_search", query=query, top_k=top_k)


def mcp_dataset_profile(dataset: str, table: str = "",
                        columns: Optional[list[str]] = None,
                        sample_rows: int = 1000) -> CallToolResult:
    return _run("dataset_profile", dataset=dataset, table=table,
                columns=columns, sample_rows=sample_rows)


def mcp_sql_query(sql: str, database: str, timeout_seconds: int = 30,
                  max_rows: int = 10000) -> CallToolResult:
    return _run("sql_query", sql=sql, database=database,
                timeout_seconds=timeout_seconds, max_rows=max_rows)


def mcp_freeform(sql: str) -> CallToolResult:
    return _run("freeform", sql=sql)


# 顺序不影响；与 exposed_tool_names() 的交集即最终暴露集
_WRAPPERS: dict[str, Any] = {
    "schema_search": mcp_schema_search,
    "knowledge_search": mcp_knowledge_search,
    "dataset_profile": mcp_dataset_profile,
    "sql_query": mcp_sql_query,
    "freeform": mcp_freeform,
}


def build_mcp_server() -> MCPServer:
    """构建 MCP server：只注册**暴露集**里的只读工具。"""
    server = MCPServer("enterprise-data-analyst", instructions=_INSTRUCTIONS)
    exposed = exposed_tool_names()
    for name, fn in _WRAPPERS.items():
        if name not in exposed:
            continue
        server.add_tool(fn, name=name, description=TOOL_SPECS[name].description,
                        annotations=_READ_ONLY_HINT)
    return server


# 传输层（stdio / SSE / streamable HTTP）由部署方按需要挂载：
#   build_mcp_server().run_stdio_async()            # stdio（本地 client）
#   build_mcp_server().streamable_http_app(...)     # HTTP（远端 client）
# 本卡交付 server 对象 + REST 发现/调用面；与真实 MCP client 的端到端握手
# 标 `[待真实验证]`（见规格 §4）。
