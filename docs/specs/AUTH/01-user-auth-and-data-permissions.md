# AUTH/01 用户级鉴权与数据权限 — 规格 v1.0

> 动因：现状是**零 API 鉴权**——任何能访问端口的人都能 `/chat/analyze` 查全部数据、
> `/export` 下载任意会话的 CSV、`/trace` 看任意人问过什么。
> 已有的权限是**工具级**（`ToolSpec.permission` + 高危工具 + 审计 + 限流），
> 缺的是**用户级**那一层：谁在调、他能看哪些表/列/行、能不能读别人的会话。
>
> 目标：在不破坏"默认本地可跑"的前提下，把这三问答清楚，且**每一步都可测、可审计**。

## 1. 身份（Principal）

```python
class Principal(BaseModel):
    user_id: str = "anonymous"
    tenant: str = ""
    roles: list[str] = []
    allowed_tables: list[str] = []      # 空 = 不限
    denied_columns: list[str] = []      # 列级黑名单（跨表按列名匹配）
    row_filters: dict[str, str] = {}    # 表 → 条件片段（如 {"fact_sales": "region_id IN (1,2)"}）
    quota_per_min: int = 0              # 0 = 不限
```

- 来源：`X-API-Key` 头 → 查 `AUTH_KEYS`（JSON 数组，见 §5）。
- **`AUTH_ENABLED=false`（默认）时是匿名 principal，权限全开** —— 保持既有的"本地起服务即可用"，
  且**既有 600+ 用例不受影响**。开了才强制 key。
- 未配置任何 key 但 `AUTH_ENABLED=true` → 所有业务端点 **503**（配置错了要吵，不能静默放开）。

## 2. 端点保护

| 端点 | 要求 |
|---|---|
| `/health`、`/health/llm`、`/ui`、`/docs` | **公开**（探活与文档不需要 key） |
| `/chat/analyze`、`/chat/analyze/stream` | 需 key |
| `/chat/analyze/export/{session}`、`/artifacts/{session}`、`/trace/{session}` | 需 key **且校验会话归属**（见 §4） |
| `/documents/ingest` | 需 key（写操作） |

- 无 key / key 无效 → **401**；有 key 但越权（表/列/会话）→ **403**；配额超限 → **429**。
- 失败响应不回显"哪个 key 存在"（防枚举）。

## 3. 数据权限（三层，全部确定性）

| 层 | 机制 | 违约时 |
|---|---|---|
| **表级** | `allowed_tables` 白名单；校验 SQL 里 `FROM/JOIN` 的表名 | 拒绝执行（403→ToolResult FAILED，`error_class=NON_RETRYABLE`） |
| **列级** | `denied_columns`：① SQL 引用禁列 → 拒绝；② 结果里含禁列 → **列不返回**（走 E4/02 同一收口点，复用 `mask_structured` 的列剔除路径） | SQL 引用 → 拒绝；结果 → 静默剔除并记审计 |
| **行级** | `row_filters[table]` → 对 SQL 里的该表**追加 `WHERE (filter)`**（已有 WHERE 则 `AND`） | 过滤由服务端强制，用户 SQL 无法绕过 |

- **行级实现要点**：只在 `FROM/JOIN <table>` 处包一层子查询会破坏别名语义，
  故采用**在 WHERE 追加谓词**：`SELECT ... FROM fact_sales WHERE x=1` → `... AND (region_id IN (1,2))`；
  无 WHERE 时加 `WHERE (…)`。过滤片段**由配置方提供**，仍过标识符/语法白名单（禁止 `;`、注释、子查询）。
- 三个层都不依赖 LLM：越权在**工具执行前**被确定性拦下。

## 4. 会话归属（防越权读别人的东西）

- 会话创建时记录 `session_id → tenant/user`（short_term，key `session_owner:<sid>`）。
- 访问 `export/artifacts/trace/{session}` 时校验归属：不匹配 → **403**；
  匿名模式（AUTH 关）不校验；同一 tenant 内**不**默认开放（最小权限）。
- 无归属记录的历史会话：AUTH 关时放行；AUTH 开时**拒绝并记审计**（宁可吵，不默认放开）。

## 5. 配置

```bash
AUTH_ENABLED=false                      # 默认关（本地开发友好）
AUTH_KEYS=[{"key":"k-acme-analyst","user_id":"alice","tenant":"acme",
            "allowed_tables":["fact_sales","dim_region"],
            "denied_columns":["customers"],
            "row_filters":{"fact_sales":"region_id IN (1,2)"},
            "quota_per_min":30}]
AUTH_ANONYMOUS_TENANT=                  # 匿名模式下的默认租户（空=全局）
```

## 6. 审计

`data/audit/auth.jsonl`：每条 `{ts, user_id, tenant, endpoint, decision(ALLOW|DENY|RATE_LIMIT),
reason, session_id, client_ip?}`。**拒绝与放行都记**（只记拒绝无法复盘）。

## 7. 边界（明确不做）
- 不做 OAuth/SSO/OIDC（企业接入时换 `Principal` 来源即可，接口不变）。
- 不做列级"部分掩码"(masking 已有，属 E4/02)、不做行级策略语言（RLS 引擎）。
- 不做跨租户共享/委托。
- **不改变**工具级权限与沙箱：AUTH 是**外层**，工具守卫是内层，两层都要过。

## 8. TDD
| 用例 | 断言 |
|---|---|
| 默认兼容 | `AUTH_ENABLED=false` → 无 key 也能 analyze（既有行为不变） |
| 401 | 开鉴权 + 无/错 key → 401，且不泄漏"key 是否存在" |
| 503 | 开鉴权但没配 key → 业务端点 503（配置错误要吵） |
| 403 表级 | 未授权表 → SQL 被拒（ToolResult FAILED + NON_RETRYABLE + 审计） |
| 403 列级 | SQL 引用禁列 → 拒绝；结果含禁列 → 列被剔除且审计 |
| 行级 | `row_filters` 生效：结果只含授权行；已有 WHERE 时正确 AND 拼接 |
| 行级不可绕过 | 过滤片段含 `;`/注释/子查询 → 配置校验拒绝（启动即报，不静默） |
| 会话归属 | 别人的 session → export/trace/artifacts 403 |
| 配额 | 超 `quota_per_min` → 429 |
| 审计 | ALLOW 与 DENY 都落 auth.jsonl，含 user/endpoint/reason |
