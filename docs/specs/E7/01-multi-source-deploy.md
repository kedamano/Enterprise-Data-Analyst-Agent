# E7/01 多数据源 + 部署硬化 — 规格 v1.0

> 动因（计划 D31–D34）：分析师的数据**不止一个库**——业务库、CRM、财务各一处。
> 现状只有一个 `data_db_url`，想查第二个库就得改配置重启。
> 另：生产镜像没有瘦身版（计划目标 <2GB），容器冒烟没有脚本化。

## 1. 命名数据源

### 1.1 配置
```bash
# 主源（保持既有语义：不填 `source` 时就用它）
DATA_DB_URL=sqlite:///./data/sample_enterprise.db
# 命名源（JSON 数组或逗号分隔的 name=url 列表）
DATA_SOURCES=[{"name":"crm","url":"sqlite:///./data/crm.db","dialect":"sqlite"},
              {"name":"finance","url":"postgresql://user:pw@host:5432/fin"}]
```
- 主源在可用源列表里显示为 `default`；命名源按 `name` 寻址。
- 解析失败（JSON 坏、缺 name/url）→ **不阻塞启动**，记 warning 并忽略该条（配置错误不该让服务起不来）。

### 1.2 寻址
- 工具新增可选参数 `source`：`sql_query` / `freeform` / `dataset_profile` / `schema_search` 均支持。
- **不填 = 主源**（既有行为完全不变，向后兼容是硬要求）。
- 未知源名 → **可读错误**，并列出可用源名（分析师要能自己纠正）。
- 计划步骤可用 `input.source` 指定（与 `build_executor_params` 既有模式一致）。

### 1.3 明确不做
- **不做跨源 JOIN / 联邦查询**：那需要联邦引擎与下推优化，属于另一个量级。
  需要跨源就在各自源上取数后用 `python_analysis` 合并（沙箱已支持单 CSV 之外的手写合并逻辑）。
  规格显式写明，避免把它当"已支持多源分析"。

## 2. 部署硬化

### 2.1 `Dockerfile.prod`
- 多阶段构建：builder 装依赖 → runtime 只带 venv + 源码；不含 `.venv`、`tests/`、`data/`、`web/node_modules`、`.git`。
- 非 root 运行；`PYTHONUNBUFFERED=1`；健康检查打 `/api/v1/health`。
- 目标 **<2GB**（记录实测体积到 `docs/progress/metrics.md`）。

### 2.2 容器冒烟脚本 `scripts/smoke_container.sh`
- `build → run → 等健康 → /health 里 `status=ok` 且 `llm_mode` 可读 → 一次 analyze（mock）→ 退出码反映成败`。
- CI 可复用（无需 docker 时跳过并**显式说明跳过**，不静默通过）。

### 2.3 安全说明（写进 `docs/部署上线.md`）
- **鉴权**：当前无内建鉴权 → 必须置于反向代理/网关之后（给出 nginx 片段与最小配置）。
- **多租户**：`DEFAULT_TENANT` 语义（记忆与知识按租户隔离）已在配置层支持；数据源层面的隔离由 DSN 决定。
- **HTTPS**：终止在网关；应用只监听 127.0.0.1。

## 3. TDD
| 用例 | 断言 |
|---|---|
| 多源参数化 | 两个 sqlite 源各有同名表但数据不同 → 按 `source` 取到各自数据 |
| 默认源不变 | 不传 `source` → 命中主源（既有用例全绿即证） |
| 未知源 | 可读错误 + 列出可用源名 |
| 配置容错 | 坏 JSON / 缺字段 → 忽略该条并 warning，服务仍可用 |
| `source` 进计划 | 计划步骤 `input.source` 能传到工具参数 |
| health | 列出可用源名（不含 DSN 密码） |
| prod 镜像 | `Dockerfile.prod` 存在且为多阶段；冒烟脚本可执行 |
| 冒烟脚本 | 无 docker 时**显式跳过**并返回可识别状态（不静默通过） |
