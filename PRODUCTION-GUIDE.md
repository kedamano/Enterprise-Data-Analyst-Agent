# 生产部署速查（PRODUCTION-GUIDE）

> 配套文档：`docs/progress/pending-real.md`（评测进度 + 阻塞清单）、`app/config.py`（Settings 字段完整列表）、`Dockerfile.prod`（生产镜像、CI 已 assert < 2GB）。

---

## 1. 前置条件

### 环境变量（.env）

变量读取逻辑：pydantic-settings 2.x 优先从 **进程环境变量** 取值，`.env` 作为 fallback（`python-dotenv` 加载）。

**必填**（任一缺失都会启动失败或功能退化）：

| 变量 | 含义 | 示例 |
|---|---|---|
| `LLM_API_KEY` | OpenAI-compatible 网关 key | sk-... |
| `LLM_BASE_URL` | 端点 URL（**不要**带尾 `/v1` 之后的路径） | `https://matrix.mzsjai.com/v1` |
| `LLM_MODEL` | 默认模型 id | `qwen/qwen3.8-27b` |
| `LLM_MAX_TOKENS` | 单轮上限（太高会被 402 按量拒） | `16384` |

**可选**（有安全默认值）：

| 变量 | 默认 | 何时改 |
|---|---|---|
| `LLM_TEMPERATURE` | `0.2` | 分析任务 |
| `LLM_NO_FALLBACK` | 未设 = 允许 mock fallback | **CI / baseline 跑 mock 时设为 `false`**；**生产必须不设** |
| `MOCK_LLM` | `false` | 本地 dry-run 可设 `true` |
| `AUTH_ENABLED` | `true` | 关掉鉴权 |
| `REDIS_URL` | 未设 = 不可用降级 | 短期记忆开启 |
| `POSTGRES_DSN` | 未设 = 不可用降级 | 长期记忆 / audit 开启 |
| `MILVUS_HOST` + `MILVUS_PORT` | 未设 = 内存兜底 | 向量召回开启 |
| `MILVUS_LITE_PATH` | 未设 | MilvusLite 单文件路径——**必须纯 ASCII 路径**（中文路径会让 faiss fopen 失败） |
| `caliber_llm_enabled` | `false` | 打开语义判读（gated，mock-determined） |

⚠️ **铁律 6**：无真实 key / 真实业务数据前，禁止把 `--strict` eval baseline 报"通过"。`docs/progress/pending-real.md` 表格里 5 项仍在等真数据。

### 依赖

- Docker 24+ / Compose v2
- Python 3.11+（开发用）
- 磁盘：容器镜像 < 2GB（CI 已 assert）；Milvus Lite 单文件 ~ 100MB起步；审计 JSONL 按会话数线性增长。

---

## 2. 启动步骤

```bash
# 1. 装包（一次性）
cd web && npm ci && npm run build && cd ..

# 2. 起依赖中间件（Redis / Postgres / etcd / minio / Milvus）
docker compose up -d

# 3. 等各服务健康（/health 返回 200 即可）
for i in $(seq 1 60); do
  curl -sf http://localhost:8000/api/v1/health || { sleep 2; continue; }
  break
done
curl -sf http://localhost:8000/api/v1/health

# 4. 打生产镜像
docker build -f Dockerfile.prod -t da-agent:prod .

# 5. 起 app（单独容器，接 compose backend 网络）
docker run -d --name da-agent \
  --network enterprise-data-analyst-agent_backend \
  -p 8000:8000 \
  -e LLM_API_KEY=... -e LLM_BASE_URL=... -e LLM_MODEL=... \
  da-agent:prod
```

> 前缘镜像已包含 Step 2 编译好的 `web/dist`；可直接在 Nginx 反代 `api_prefix=/api/v1` 接收（见 `app/config.py:59`）。

---

## 3. 关键健康检查

| 端点 | 期望 | 含义 |
|---|---|---|
| `GET /api/v1/health` | `200 {"status": "ok"}` | 应用整体健康 |
| `GET /metrics` | 输出含 `http_requests_total` | 可观测性指标 |
| `GET /health`（根） | `200` | 应用存活探活 |

容器冒烟脚本（`docker run` 后 10 次重试 ~20s）：
```bash
for i in $(seq 1 10); do
  curl -sf http://localhost:8000/api/v1/health >/dev/null && break
  sleep 2
done
curl -sf http://localhost:8000/api/v1/health
curl -sf http://localhost:8000/metrics | grep -q http_requests_total
```

---

## 4. FAQs（排查指引）

**Q: 响应没加 degraded / mock fallback 静默了吗？**
A: D52 后`/api/v1/health` 开始报 mock 路径的降级事件；见 `app/infrastructure/llm/router.py` 中 `fallback_events()`；前端 chat 气泡显式标注"mock 模式"。

**Q: Redis 不可用会怎样？**
A: 会话短期记忆写不进——用户重启浏览器 / 切换设备后恢复不到上轮上下文。长期报告仍在（文件落盘）。`/health` 仍 `200`, 不阻塞调用方。

**Q: `caliber_llm_enabled=true` 什么时候开？**
A: 默认 `false`。打开后 caliber 阶段调 LLM 给报告里的指标口径/限定词做语义复核（gated，mock-determined）。生产开之前先在单会话 dry-run 跑一遍。

**Q: pg / mysql 真库测试怎么触发？**
A: 环境变量里有 `POSTGRES_DSN` + `MYSQL_DSN` 时 `tests/test_pg_live.py` / `tests/test_mysql_live.py` 自动从 skip 转绿。CI live job 已挂 4 套容器（MySQL 8.0 / postgres:16-alpine / redis:7-alpine / Milvus Lite）。

---

## 5. 发布 checklist（生产前）

- [ ] `docker build -f Dockerfile.prod -t da-agent:prod .` 镜像 size < 2GB
- [ ] 容器 smoke：`/health` 200 + `/metrics` 含 `http_requests_total`
- [ ] `AUTH_ENABLED=true`、`LOG_LEVEL=WARNING` + `MOCK_LLM=false`
- [ ] 凭据不进镜像（.env 在容器单独挂载）
- [ ] 首启的管理员创建后立即关闭 open registration（`app/core/security/users.py` 的 first-admin 逻辑）
- [ ] MilvusLite 数据目录挂持久卷（重启不丢向量）
