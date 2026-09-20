# 用法更新速查（D64 增订）

本文件补充 `/docs/部署上线.md` 未尽的 D64 新增能力。**主力使用方式不变**：`POST /api/v1/chat/analyze` + SSE stream 依然原样可用；D64 只是在"护栏 / 后台任务 / 可观测"这几层加厚。

## 0. 新增 HTTP 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | `/api/v1/budget/usage` | 三级用量（session / user / tenant 各自 usage + limit + ratio） |
| GET  | `/api/v1/budget/config` | 返回 policy + 各级 limit（admin-only if AUTH） |
| POST | `/api/v1/jobs` | 创建 workflow job |
| GET  | `/api/v1/jobs` | 列出当前用户 jobs |
| GET  | `/api/v1/jobs/{id}` | 单个 job 详情 |
| PATCH| `/api/v1/jobs/{id}` | 更新 enabled/query/schedule |
| DELETE | `/api/v1/jobs/{id}` | 删除 |
| POST | `/api/v1/jobs/{id}/run` | 手动触发一次 run now |

不存在的路由（或未启用）返回 **422 / 404**；**未认证**返回 401。

---

## 1. Workflow Job 创建示例

```bash
# 1. 日报：每天早上 08:00 跑 + 推送 webhook
curl -X POST http://host/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "daily-sales",
    "query_template": "生成 {{today}} 销售日报",
    "schedule": "daily@08:00",
    "webhook_url": "https://hooks.example.com/daily"
  }'

# 2. 月末一次性：次月 1 号 09:00 跑，然后自动失效
curl -X POST http://api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "month-close",
    "query_template": "生成 {{last_month}} 月度经营回顾",
    "schedule": "monthly@01@09:00"
  }'

# 3. 立即跑一次
curl -X POST http://api/v1/jobs/$JOB_ID/run \
  -H "Authorization: Bearer $TOKEN"
```

模板占位符：`{{today}}`（ISO 日期 YYYY-MM-DD）/ `{{last_month}}`（YYYY-MM）/ `{{now}}`（ISO 时间戳）。

### 1.1 单 tenant 约束

- 单用户最大 `WORKFLOW_JOBS_MAX_PER_USER`（默认 10，见 `.env.example`）。
- job 归属 owner_sub；非 owner 非 admin ⇒ 404（不存在感）。
- queue 走进程内 `threading.Timer`（MVP）；生产升级到 APScheduler/ARQ 后两 HTTP 接口不变。

---

## 2. LLM Budget

### 2.1 请求级评估顺序

`charge(session_id, tokens)` 按 session → user-daily → tenant-monthly 顺序评估，命中任一上限即按 `BUDGET_OVERRUN_POLICY` 处置：

| policy | 表现 |
|---|---|
| `allow`（默认） | 超限仅记录 metadata，**不阻断** |
| `degrade` | 切到 `BUDGET_DEGRADED_MODEL`（便宜模型），客户端拿 `budget_degraded=true` |
| `block` | SSE 终止；同步请求拿 **429 Too Many Requests** |

### 2.2 查看用量

```bash
curl http://host/api/v1/budget/usage \
  -H "Authorization: Bearer $TOKEN"
```

```
{
  "session": {"used_tokens": 12340, "limit_tokens": 50000, "ratio": 0.247},
  "user_daily": {"used_tokens": 89000, "limit_tokens": 200000, "ratio": 0.445},
  "tenant_monthly": {"used_tokens": 1200000, "limit_tokens": 10000000, "ratio": 0.12}
}
```

前端 `BudgetBar` 顶部进度条用同样数据（day/month ratio 染色提示）。

---

## 3. Prompt 注入防御

默认开启（`PROMPT_GUARD_ENABLED=true`），无需配置。

| 动作 | 行为 |
|---|---|
| `passthrough`（默认观察） | 触发样 injection payloads 被日志记录，不删除 |
| `redact` | 命中 → `[REDACTED]` 替换 + `warnings[]` 写入 metadata（推荐生产） |
| `block` | 高置信度阻断（≥0.85 risk_score）→ 400 |

**Fail-open 铁律**：Guard 内部任何异常（regex 膨胀、unicode 编解码错等）一律 → **原文透传 + logger.warning**，永不打挂主分析。

---

## 4. Self-Consistency（analyst 多次采样）

**默认开**（`SELF_CONSISTENCY_ENABLED=true`），analyst 子阶段多采 `SELF_CONSISTENCY_N=3` 次（temperature=0.7），频率最高的结论作主输出，一致性率入 `metadata.self_consistency_rate`。

一致性率低于 0.5 → logger.warning（可在 LangFuse 配告警）。

关闭：`.env` 设 `SELF_CONSISTENCY_ENABLED=false` → 退回单次调用。

---

## 5. 上下文压缩

`CONTEXT_COMPRESSION_ENABLED=true`（默认开）。

| 阈值 | 行为 |
|---|---|
| 总 tokens < `MAX_TOKENS × COMPRESS_THRESHOLD`（默认 60k × 0.8 = 48k） | 不压缩 |
| 超过 | tool_result 截断至 `CONTEXT_COMPRESS_TOOL_RESULT_CHARS`（默认 500 chars，头尾各留 40% + 行数统计）；老 report 摘要至 `CONTEXT_COMPRESS_REPORT_CHARS`（300 chars） |
| 压缩后仍 > `CONTEXT_HARD_MAX_TOKENS`（80k） | 抛 `ContextOverflow` → SSE status=`CONTEXT_OVERFLOW` → 前端弹窗「请精简问题或新开对话」 |

关闭：`CONTEXT_COMPRESSION_ENABLED=false`（退回旧 tokens 无限累积，便于评测排障）。

---

## 6. Replay CLI（本地排障）

```bash
# 跑项目 env 内的 CLI（不要裸 python；conda/venv 隔离）
conda run -n base python scripts/replay.py show <session_id>
conda run -n base python scripts/replay.py diff <session_id>...
conda run -n base python scripts/replay.py run <session_id> --tools sql_query=mock,python_analysis=mock
```

`run` 行为：加载 trace JSONL → `_apply_tool_overrides` 把 `sql_query/python_analysis/schema_search/knowledge_search` 替换为内存 mock → 运行全流水线 → **退出时恢复 REGISTRY 原值**（即使抛异常）。挂载的 mock 实现位于脚本内 `MOCK_TOOLS` dict；现场调优把真 SQL 结果拷进去即可 100% 复现 analyst/reflection 行为。

---

## 7. LangFuse 远程可观测（opt-in）

**缺 env = 完全不调用**；本地 trace JSONL 不受影响（纯本地模式）。

配对步骤：

```bash
# .env
LANGFUSE_SECRET_KEY=sk-xxxx
LANGFUSE_PUBLIC_KEY=pk-xxxx
LANGFUSE_HOST=https://cloud.langfuse.com   # 自托管则改
```

配对后自动双写：`app/infrastructure/observability/tracing.py::Tracer.end()` 末端回调 → 一个本地 JSONL span + 同 span 推 LangFuse trace。**任一通道失败不影响另一通道**；LangFuse 包本身也是 `try/ ImportError → _NoopSpan` fallback。

关：`.env` 留空 / 不填 → 初始化返回 False → 全部 no-op。

---

## 8. 配置速查

| 分组 | 关键 env | 默认 |
|---|---|---|
| LangFuse | `LANGFUSE_SECRET_KEY` / `PUBLIC_KEY` | 空（关） |
| Workflow | `WORKFLOW_JOBS_ENABLED` / `MAX_PER_USER` | true / 10 |
| Budget | `BUDGET_ENABLED` / `PER_SESSION` / `PER_USER_DAILY` / `PER_TENANT_MONTHLY` / `OVERRUN_POLICY` | true / 50k / 200k / 10M / allow |
| Guard | `PROMPT_GUARD_ENABLED` / `ACTION` / `LOG_LEVEL` | true / redact / WARNING |
| Context | `CONTEXT_COMPRESSION_ENABLED` / `MAX_TOKENS` / `THRESHOLD` / `HARD_MAX` | true / 60k / 0.8 / 80k |
| Self-Consistency | `SELF_CONSISTENCY_ENABLED` / `N` / `TEMPERATURE` | true / 3 / 0.7 |

详见仓库根 `.env.example`（每行带推荐值 + 注释）。
