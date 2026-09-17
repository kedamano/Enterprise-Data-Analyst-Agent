# MCP 接入层 — 规格 v1.0（D50 定稿）

> 缺口（`docs/对标企业级Gap.md` 八）
> > | 工具为进程内函数 | 无 **MCP** | **❌ 仍缺** | 无变化 |
>
> 本项目所有工具都是**进程内函数**（`REGISTRY` dict），外部 Agent（Claude Desktop、
> Cursor、任何 MCP client）**够不着**。D50 把**只读**工具按 MCP 协议暴露出去。

---

## 1. 暴露什么：**只读**，且只读是硬边界

暴露判定：`ToolPermission ∈ {READ_METADATA, READ_KNOWLEDGE, READ_DATA}`，
即从 `TOOL_SPECS` **程序化推导**（新增只读工具自动进入，不必改接入层）。

| 工具 | 权限 | 暴露 |
|---|---|---|
| `schema_search` | READ_METADATA | ✅ |
| `knowledge_search` | READ_KNOWLEDGE | ✅ |
| `dataset_profile` | READ_DATA | ✅ |
| `sql_query` | READ_DATA | ✅ |
| `freeform` | READ_DATA | ✅ |
| `python_analysis` | COMPUTE | ❌ **任意代码执行面** |
| `visualization` | COMPUTE | ❌ 产物生成 |
| `generate_report` | GENERATE_ARTIFACT | ❌ 产物生成 |
| `image_analyze` | READ_DATA | ❌ **显式排除**（见下） |

**`image_analyze` 例外**：权限词表上是 READ_DATA，但实现耦合了附件挂载
（`_session_id` → 会话工作目录）且调用视觉模型（有成本）。MCP client 传文件名
在无附件会话里必然落空 → 暴露它会制造一个**永远失败的端点**。故显式排除并留档。

> 设计取向：**宁可少暴露，不可多暴露**。MCP client 拿到的是一套只读数据面，
> 永远不是代码执行面——这与规格 §22「词表无 WRITE 权限」同一条纪律。

## 2. 三支柱

### 2.1 可发现（`tools/list`）
`list_tools()` 返回每个暴露工具的 `name / description / inputSchema / annotations`。
**schema 直接来自 `TOOL_SPECS.input_schema`**——已经在 §22 声明式登记过，
接入层不另写一份（避免两处漂移，与 E4/02 模式表统一入口同范式）。
全部工具标 `read_only_hint=True`（MCP 原生注解，让 client 自行做确认提示）。

### 2.2 可授权（复用 AUTH/01，**不重写鉴权**）
每个 `call_tool` 都走 `execute_tool()`——那里已经有序列完整的安全链：
**RBAC 门禁（AUTH/01 §6）→ 速率限制 → 只读守卫 → 执行 → 输出管控 → 审计**。
接入层不复制其中任何一环。权限不足 → `execute_tool` 返回
`status=FAILED` / `error_class=NON_RETRYABLE`，接入层转成 MCP `isError` 结果
（**绝不执行**）。

### 2.3 可观测
`execute_tool` 已写 `data/audit/tool_audit.jsonl`（成功失败皆记）。
接入层额外保证：**权限被拒的调用同样落审计**（否则"谁在探边界"无从复盘，
同 AUTH/01 / D45 的取舍）。

## 3. 传输与 REST 面

- **MCP 官方协议**：用 `mcp` 包的 `MCPServer`（v2.x；v1 的 `FastMCP` 已改名）。
  支持 stdio / SSE / streamable HTTP（由包提供），本卡交付**核心 server 对象**
  + REST 发现/调用面，实际挂载由部署方按传输方式接入。
- **REST 面**（便于无 MCP client 时直接验证）：
  | 方法 | 路径 | 行为 |
  |---|---|---|
  | `GET` | `/api/v1/mcp/tools` | 列出暴露工具（含 schema + 权限 + data_scope） |
  | `POST` | `/api/v1/mcp/call` | `{tool, arguments}` → 走 `execute_tool`，返回结构化结果 |

## 4. 边界与诚实纪律

- **`mcp_enabled` 默认 `false`** → REST 面返回 **503**（配置未启用要吵，不静默空跑），
  且**不影响既有 950+ 用例**（与 D45「默认关」同范式）。
- **接入层只做编排，不做安全**：鉴权/限流/守卫/审计全部委托 `execute_tool`。
  接入层自己出错 → 转 MCP `isError`，**绝不裸抛**打挂 server。
- **`isError` 语义**：工具执行失败或权限被拒 → `is_error=True` + 文本说明，
  不抛异常（MCP 协议里工具失败是**结果**而非**异常**）。
- **零副作用**：接入层不写任何新状态；session_id 用 `mcp_session_id` 配置
  （默认 `"mcp"`）供限流/审计归集，不伪造真实会话。
- `[待真实验证]`：与**真实 MCP client**（Claude Desktop / Cursor）的端到端握手
  与工具调用未验——本卡验证的是 server 对象与协议契约（`list_tools` / `call_tool`
  形状），transport 层由部署方接入。

---

## 5. TDD（先红后绿）

`tests/test_mcp_server.py`：
- **可发现**：`list_tools()` 恰好 5 个（schema_search/knowledge_search/dataset_profile/
  sql_query/freeform）；**不含** `python_analysis`/`visualization`/`generate_report`/
  `image_analyze`；每个含非空 `inputSchema`；`read_only_hint=True`。
- **可授权**：AUTH 开 + `viewer` 角色调 `sql_query` → `isError`、错误含权限不足、
  **且未被执行**（用 spy 断言 `REGISTRY["sql_query"]` 未被调用）；`analyst` 角色 → 正常返回。
- **可观测**：成功调用与权限拒绝**都**写 tool 审计记录。
- **健壮性**：`call_tool` 传未暴露工具（如 `python_analysis`）→ `isError`，不崩；
  传不存在工具 → `isError`；工具内部抛异常 → 转 `isError` 不裸抛。
- **REST**：`GET /mcp/tools` 返回列表；`POST /mcp/call` 返回结构化结果；
  `mcp_enabled=false`（默认）→ 两端点 **503**。
