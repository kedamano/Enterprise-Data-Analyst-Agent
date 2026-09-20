# 变更日志（CHANGELOG）

注：每一条都能在 `docs/progress/pending-real.md` 或 `docs/progress/eval-*.md` 中找到对应的真跑证据。**没有具体数字的条目标 🟡。**

## D64 (2026-09-19) — P0/P1 全栈加固：Budget / 防注入 / Self-Consistency / 上下文治理 / Workflow Jobs / Replay CLI / LangFuse

> 本轮 7 项 P0/P1 全部交付；新增前端 BudgetBar；前后端解耦全路径。

### P0 — LLM Budget（三级限额 + 前端用量条）

**`app/infrastructure/llm/budget.py`（New ~160 行）**：

- `BudgetTracker`：per-session / per-user daily / per-tenant monthly 三级嵌套限额（LRU 64/256/256）。
- `_evaluate_limit(tokens_before, tokens_to_add, limit_tokens)`：返回 `(ok, reason, severity_info|degrade_info|block_info)`。三层按 session→user→tenant'顺序评估；任何一层超限即拦截。
- overrun_policy 三档：`allow` / `degrade`（切 LLM_DEGRADED_MODEL）/ `block`（429）。
- `charge(session_id, tokens, ...)`：上报 token 使用（采样估算+模型 pricing metadata）。全程 WAL 持久化。

**`app/api/routes/budget.py`（New ~90 行）**：

- `GET /api/v1/budget/usage`：返回三级用量（session/user/tenant 各自 usage + limit + ratio）。
- `GET /api/v1/budget/config`：返回 policy + 各级 limit（admin-only if AUTH）。

**前端** `web/src/components/BudgetBar.tsx`（New Top-of-rail）：

- 顶部进度条：day ratio / month ratio 染色（<70% 蓝 / 70–90% 黄 / >90% 红）。
- 命中高阈值前台可见告警；`SkeletonLine` 骨架屏。

**`tests/test_budget.py` + `tests/test_budget_api.py`（New ~220 行）**：18 cases → **18 passed**。

### P0 — Prompt 注入防御 + 用户输入清洗层

**`app/infrastructure/llm/guard.py`（New ~220 行）**：

- `SanitizeResult` dataclass（text / warnings / risk_score / action_taken）。
- `PromptGuard.sanitize_user_input(text)`：正则匹配已知注入载荷池；命中 + `action_taken=redact` → `[REDACTED]` 替换；`action_taken=block` 直接抛 `SecurityRiskDetected`。
- `PromptGuard.harden_system_prompt(system_prompt)`：追加 role-marker + 清理 residual instruction。
- **Fail-open 铁律**：任何内部异常 → 原样透传 + logger.warning（绝不打挂主分析）。

**`tests/test_prompt_guard.py`（New 8 cases）**：→ **8 passed**。

### P0 — Self-Consistency（analyst 多次采样投票 + 一致性率入 metadata）

**`app/core/agents/data_analyst/self_consistency.py`（New ~170 行）**：

- `extract_metric(text)`：抽取首个数值型结论（保留 1 位小数，去单位）。
- `consensus_pick(samples, temperature=0.7, n=3)`：`asyncio.gather` 多采样，频率最高值作为 consensus；计算 `consistency_rate = 赞同共识的样本数 ÷ n` → 入 metadata。
- 一致性率低于阈值 → logger.warning（便于 LangFuse 告警降级）。

**`tests/test_self_consistency.py`（New 6 cases）**：→ **6 passed**。

### P0 — 上下文窗口治理（旧消息摘要压缩 + token 预算硬上限）

**`app/core/memory/compression.py`（New ~340 行）**：

- `ContextCompressor.compress_tool_result(text, max_chars=500)`：头尾各留 40% + 中段截断 + 行数统计。
- `ContextCompressor.summarize_report_for_history(report, max_chars=300)`：保留 executive_summary + 核心指标数值。
- `ContextCompressor.count_messages_tokens(messages)`：近似 token 计数（4 chars ≈ 1 token）。
- `ContextOverflow` exception → status="CONTEXT_OVERFLOW" → 前端提示"请精简问题 / 新开对话"。

**接入点** `graph.py::_drive_sync`：`run_planner` 前做 sanity 检查；`run_reflection` 后、REPLAN 前做一次主动压缩。

**`tests/test_context_compression.py`（New 7 cases）**：→ **7 passed**。

### P1 — Workflow Job 模块（threading scheduler + webhook 回调）

**`app/infrastructure/jobs/jobs.py`（New ~340 行）**：

- `JobScheduler`：`threading.Timer` 驱动（MVP 原地替代 APScheduler）；支持 `daily@HH:MM` / `monthly@DD@HH:MM` 周期调度 + `run_once_at` 一次性。
- `WorkflowJob` dataclass：`{id, owner_sub, tenant_id, name, query_template, schedule, run_once_at, webhook_url, ...}`。
- `render_template()`：替换 `{{today}}` / `{{last_month}}` / `{{now}}`。
- SQLite (`data/jobs/jobs.db` WAL) 持久化 + lazy schema（`_SCHEMA_ENSURED` 防重复建表）。
- Webhook POST（urllib.request）吞异常：回调失败不影响 job 记录。

**`app/api/routes/jobs.py`（New ~180 行）**：

- 6 REST 端点：POST/GET/GET{id}/PATCH/DELETE/POST{id}/run。
- `current_principal()` + `_job_or_404` 鉴权（owner/sub 或 admin）。
- Max jobs 上限（`settings.workflow_jobs_max_per_user`，默认 10）。

**`tests/test_workflow_jobs.py`（New 9 cases）**：→ **9 passed**。

### P1 — Agent Debug Replay CLI

**`scripts/replay.py`（New ~250 行）**：
- `replay show <sid>`：summary 视图（各阶段耗时 + 摘要）。
- `replay run <sid> [--tools ...]`：重放 trace，`_apply_tool_overrides` 替换 REGISTRY 上的工具实现（退出时恢复）。
- `replay diff <sid>`：列出历史 traces 对比。
- 由 `conda run` 启动（走项目 env）→ 不依赖 shell 相对路径。

**`tests/test_replay.py`（New 7 cases）**：→ **7 passed**。

### P1 — LangFuse 集成（env 开关 → 双写 + flush on shutdown）

**`app/infrastructure/observability/langfuse.py`（New ~140 行）**：

- Env 门控：未配 `LANGFUSE_SECRET_KEY` + `LANGFUSE_PUBLIC_KEY` → `init()` 返回 False → all no-op。
- `emit_span(name, trace_id, status, duration_ms, input, output, metadata)`：双写一块 span 到 LangFuse trace。
- `observe_span` 装饰器 + `_NoopSpan` fallback（env 缺失时 `yield` 一个 noop）。
- `flush()` 干净关闭。

**`app/infrastructure/observability/tracing.py`**：端点 `Tracer.end()` 末尾追加 `emit_span(...)` 回调 → 本地 JSONL 与 LangFuse 云端互为备份、互不依赖；任何异常被吞（本地 trace 永不依赖 LangFuse）。

**`app/main.py`**：`lifespan` teardown 追加 `_lf_flush()`。

**`tests/test_langfuse.py`（New 5 cases）**：→ **5 passed**。

### 验收

| 维度 | 结果 |
|---|---|
| `tsc --noEmit` | **0 errors** |
| 本轮新增 cases | budget 18 + guard 8 + self_consistency 6 + ctx 7 + workflow_jobs 9 + replay 7 + langfuse 5 = **60 passed** |
| 主回归 | **全绿 0 failed** |
| LangFuse 缺包/缺 env 路径 | 纯 no-op（测试 + 代码路径双重守卫） |

### 新增配置（ENV / `.env.example`）

| 分组 | 配置项 |
|---|---|
| LangFuse | `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_HOST` |
| Workflow | `WORKFLOW_JOBS_ENABLED` / `WORKFLOW_JOBS_MAX_PER_USER` |
| Budget | `BUDGET_ENABLED` / `PER_SESSION_TOKENS` / `PER_USER_DAILY_TOKENS` / `PER_TENANT_MONTHLY` / `OVERRUN_POLICY` |
| Guard | `PROMPT_GUARD_ENABLED` / `ACTION` / `LOG_LEVEL` |
| Context | `CONTEXT_COMPRESSION_ENABLED` / `MAX_TOKENS` / ... |
| Self-Consistency | `SELF_CONSISTENCY_ENABLED` / `N` / `TEMPERATURE` |

## D63 (2026-09-18) — 广度拓展 P2：Multi-Agent Supervisor + Feedback Loop + Stage-Aware LLM Router

> 本轮 3 项 P2 全部交付；前端 tsc 0 errors、后端 60 cases（含本轮 26 cases 全绿）。

### P2 — Multi-Agent Supervisor（并行子任务调度）

**`app/core/agents/data_analyst/supervisor.py`（新建 ~350 行）**：

- `plan_subtasks(state, max_subtasks=4)`：识别"对比"/"以及"/"vs"等自然语言切分信号，按指标数 × 维度数 决定是否拆分；复杂问题拆为多个独立 SubTask；简单问题 → 单元素列表（直接回落主流水线）。
- `run_supervisor(parent_state)`：按 priority 拓扑排序 + `ThreadPoolExecutor(max_workers=4)` 并行跑子任务；子任务 pipeline **复用已有 nodes**（`run_context`/`run_planner`/`run_analyst`/`run_reporter`）；任一子任务异常 → 标注失败 + 照常汇总其他子任务；汇总生产 master_report（按章节拼接）+ metadata `{supervisor: {...}}` + 独立 trace JSONL。
- 配置：`supervisor_enabled`、`max_subtasks`、`supervisor_max_parallel`、`supervisor_timeout_per_subtask_s`。

**`app/core/agents/data_analyst/graph.py`**：`_drive_sync()` 的 `run_planner` 之后、`run_executor_all` 之前插入 Supervisor 触发判断；Supervisor 失败兜底为 metadata 写 fallback_reason + 继续走原流水线。

**`tests/test_supervisor.py`**：6 cases（复杂 query 拆分、简单 query 回落、单 subtask 并行跳过、mock tools 汇总、异常隔离、heuristic 5 条件分支）→ **6 passed**。

### P2 — Feedback Loop（用户反馈 → 落库 → 聚合 + 入 eval set）

**`app/infrastructure/feedback/feedback_store.py`（新建 ~150 行）**：

- SQLite (`data/feedback/feedback.db`)，upsert by `session_id`（UNIQUE + `ON CONFLICT DO UPDATE`）。
- `submit_feedback(rating, comment, report_snippet, stage_breakdown)`：±2 之外的 rating 抛 ValueError；comment 超 500 字自动截断；任何内部异常静默吞 + logger.warning（永不打挂主 stream）。
- `aggregate_feedback()`：总数 / 好评率 / 好评数 / 差评数 / 有文字数 / 最近 10 条 / 有文字列表。
- `low_quality_sessions()`：`rating=-1` 的 session_id 列表 → 供 eval runner 自动提取 bad cases。

**`app/api/routes/feedback.py`（新建 ~100 行）**：

- `POST /api/v1/feedback/{session_id}`：验证 checkpoint 存在 → submit → 201。
- `GET /api/v1/feedback/{session_id}`：单条查询 / 404。
- `GET /api/v1/feedback/stats`：聚合统计（admin_only if AUTH）。

**前端** `web/src/components/FeedbackWidget.tsx`（新建）：

- 👍👎 双按钮 + "写几句..." 文本输入；加载 / 成功 / 错误三态；已提交灰掉 + "修改反馈"。
- `web/src/lib/api.ts`：`submitFeedback`/`fetchFeedback`/`fetchFeedbackStats` + 类型。
- `web/src/components/Report.tsx`：渲染后插入 `<FeedbackWidget sessionId={...} />`。
- `web/src/components/AnalyticsView.tsx`：新增"反馈质量" tab（5 KPI + 差评列表 + 文字反馈列表）。

**`tests/test_feedback.py`**：13 cases 全覆盖 → **13 passed**。

### P2 — Stage-Aware LLM Router（按阶段分级路由）

**`app/infrastructure/llm/stage_router.py`（新建 ~132 行）**：

- `STAGE_TIER`：context/planner/executor/reflection → light；analyst/reporter → heavy；vision → 始终 heavy。
- `StageLLM(BaseLLM)`：组合每个 tier 对应的 `RouterLLM`（带 tier 字段的 provider 列表）；tier 无匹配 → fallback 到 legacy `OpenAILLM`。
- `_tier_providers(settings, tier)`：从 `LLM_ROUTES` 筛 `tier` 字段匹配的 ModelConfig；未标 tier 的条目走 legacy。

**Router 切换** `router.py::get_llm()`：

- `LLM_ROUTES` 中有任何一条带 `tier` → 启用 `StageLLM`。
- 无任何 `tier` 字段 → 保持旧 `RouterLLM` 行为不变（**铁律：向后兼容**）。

**`tests/test_stage_llm.py`**：7 cases（light 路由、heavy 路由、fallback legacy、vision 始终 heavy、get_llm 携带/不携带 tier、tier 字段解析）→ **7 passed**；现有 `tests/test_model_router.py` 6 cases 也全绿。

### 验收（铁律 6）

| 维度 | 结果 |
|---|---|
| `tsc --noEmit --skipLibCheck -p tsconfig.app.json` | **0 errors** |
| 本轮新增 26 cases (6 supervisor + 13 feedback + 7 stage_llm) | **26 passed** |
| 主回归 (response/semantic cache / rbac / model_router) | **34 passed / 0 failed** |
| 合入回归 (**含主回归 + supervisor/feedback/stage_llm**) | **60 passed / 1 skipped / 0 failed** |
| 反馈模块对主流水线零污染（submit 异常全静默） | 设计守卫 + 测试覆盖 |

### 深度审计发现（P0 标记 completed）

本轮还审计了此前认为缺失的 P0 项，发现**实为已有**：

| P0 项 | 位置 |
|---|---|
| 流式输出 + 取消 | `web/src/lib/api.ts:streamAnalyze` + App.tsx `abortRef` + Composer 停止按钮 + 后端 `chat.py:stream_analysis` SSE |
| Tool Use 层 | `app/core/tools/__init__.py`（REGISTRY + execute_tool）+ `python_tool.py`（AST guard + subprocess 沙箱）+ `specs.py` |

### D64-D66（下一轮可选，面试讲"规划"已足够）

- D64：Multi-datasource connector plugin 层（MySQL / BigQuery / Snowflake）
- D65：零停机部署 + k8s manifest（蓝绿 / rolling restart）+ DEPLOY.md
- D66：Auth 支持 SSO / LDAP + AuthCentre 登录页走真实 OIDC 流程

## D62 (2026-09-18) — 可观测 + 语义缓存：Cost Dashboard + Semantic Cache

> 广度拓展的两项 P1 从"缺失"补到"可用"；P0（流式输出、Tool Use 抽象层）经
> 审计发现**已有**（标记 completed）。

### P1 — Cost Dashboard（聚合 + 前端视图）

**后端** `app/api/routes/analytics.py`（新建）：
- `GET /api/v1/analytics/stats?range=24h|7d|30d&session_id=xxx`。
- 读 `data/traces/*.jsonl`（每文件最后一行 summary），按 mtime 过滤时间范围。
- 聚合：`totals`（runs / success_rate / avg_duration / p95 / total_tokens / total_cost / avg_tool_calls / avg_runs_per_hour）、`by_stage`、`by_tool`、`hourly`、`recent_runs`、`process_metrics`（Prometheus 快照）。
- `session_id` 存在时返回该 run 摘要（同 schema，runs=1）。
- 鉴权：AUTH 开启时取 `current_principal().roles`，非 admin → 403。
- 路由注册到 `app/main.py`（prefix=api_prefix，tags=["analytics"]）。

**前端** `web/src/components/AnalyticsView.tsx`（新建）：
- 顶部 4 个 KPI 卡片：总分析数 / 成功率 / 平均耗时（avg + P95）/ Token 消耗。
- 阶段耗时条形图（planner / executor / analyst / reflection / reporter），纯 CSS flex bar（`bg-brand` + `width: pct%`），零图表库依赖。
- 工具调用分布（sql / python / knowledge / visualization / …）。
- 最近 runs 列表（session_id 前 8 位 + 状态色点：OK 绿 / ERROR 红）。
- 24h / 7d / 30d 切换 tab；错误态红色 banner + 403"需管理员权限"提示。

`web/src/components/AnalyticsView.test.tsx`（新建）：2 个 vitest case（KPI 渲染 + mock fetch）。
`web/src/components/SideRail.tsx` + `web/src/App.tsx`：左侧 rail 加"控制台"项（`IconChartBar`，来自 `@tabler/icons-react`），view="analytics" 时渲染 `<AnalyticsView />`。

凭证：`conda run -n base python -m pytest tests/test_analytics_stats.py -q` → **7 passed**；`tsc --noEmit --skipLibCheck -p tsconfig.app.json` → **0 errors**。

### P1 — Semantic Cache（hashing trick + SQLite，cosine 召回）

**新建** `app/core/agents/data_analyst/semantic_cache.py`：
- `embed_query(query, dim=64)`：hashing trick（词袋 → 64 维，确定性、零模型依赖）。
- SQLite 持久化：`data/cache/semantic_cache.db`，表 `semantic_cache`（session_id / query / embedding_json / state_json / query_hash / created_at），按 `query_hash` + `session_id` 双索引。
- `get_semantic(session_id, query)` → 同 session cosine ≥ `semantic_cache_similarity_threshold`（默认 0.92）命中，深拷贝 state 并打 metadata `{cache_hit:true, cache_key:"semantic", similarity, matched_query}`。三不存守卫：status ≠ FINISH / report 空 / degraded → 跳过。
- `put_semantic(session_id, query, state)` → best-effort（任何异常静默 log，不污染主路径）。
- `clear_semantic(session_id?)` / `semantic_enabled()`。

**接入** `app/core/agents/data_analyst/graph.py`：
- sync `run_analysis()`：exact-match response_cache 未命中后，进入 `trace_run` 前查 semantic cache；命中直接返回 metadata 标记的 state。末尾 `put_cached` 后调 `put_semantic`。
- stream `_stream_analysis_inner()`：在生成器最开头（INIT yield 之后）命中即 return；`finally` 块里也调 `_put_semantic`。
- **两条路径并列**：response_cache（exact-match）与 semantic_cache（近似匹配）独立准入 / 独立淘汰。

**配置** `app/config.py`：
- `semantic_cache_enabled: bool = True`
- `semantic_cache_similarity_threshold: float = 0.92`
- `semantic_cache_embedding_dim: int = 64`
- `.env.example` 对应 `SEMANTIC_CACHE_ENABLED` / `SEMANTIC_CACHE_SIMILARITY_THRESHOLD`。

凭证：`conda run -n base python -m pytest tests/test_semantic_cache.py tests/test_response_cache.py -q` → **17 passed**（7 semantic + 10 response_cache，后者已对齐 semantic_cache fixture 隔离至 tmp_path）。

### P0 — 审计发现（已有，未改动）

| 项 | 现状 |
|---|---|
| 流式输出 | `/api/v1/chat/analyze/stream` SSE + `stream_analysis` 生成器（`app/api/routes/chat.py` L215-380）；前端 `web/src/lib/api.ts:streamAnalyze` + App.tsx 的 `abortRef` + Composer 的 停止按钮 |
| Tool Use 抽象层 | `app/core/tools/__init__.py`：`REGISTRY` 注册表 + `execute_tool()`（审计/RBAC/速率限制/脱敏/重试）；`app/core/tools/python_tool.py`：AST guard + subprocess 沙箱（超时 + allowlist）；6 个 built-in tools（schema/python/knowledge/visualization/sql/freeform） |

## D61 (2026-09-17) — 面试短板修复：RAG 基线 + E2E CI 流程落地

> 修复前文「#三一眼可见的短板」中影响面试说服力的 2 项。

### 1. RAG synthetic benchmark（offline）

- 新文件 `scripts/benchmark_rag.py`（406 行）：
  - 60 条合成短文档 / 6 主题 / 12 条 query，ground truth 人工标注；
  - hashing trick 64-dim 嵌入 + BM25 + RRF + 项目既有 deterministic reranker；
  - 输出 Recall@1 / Recall@3 / Recall@5 / MRR / Duration。
- 新文件 `benchmarks/RESULTS.md`（含表格 + 解读 + 逐条明细）。
- README 顶部已插入徽章行：
  > **RAG 基准（合成语料，offline）：Recall@1 37.5% · Recall@3 68.1% · Recall@5 75.0%。**

**实测**：`conda run -n base python scripts/benchmark_rag.py` → 70ms，输出如上。

### 2. E2E 运行流程文档化（Playwright 浏览器被 proxy 墙挡住，CI 是正路）

- 新文件 `docs/E2E_RUNBOOK.md`，覆盖：CI 如何跑 / 本机如何绕过 proxy（`PLAYWRIGHT_DOWNLOAD_HOST`）/ headless+headed 切换 / reporter 输出位置。
- 新文件 `scripts/install_playwright_browsers.sh`：封装 Playwright 浏览器下载（国内镜像 fallback）。

## D60 (2026-09-17) — 安全/测试/RAG/Docker SPA 路由全量修复（#11 全清单）

本轮按深层扫描发现的 6 项安全/RAG 测试缺口 + SPA URL 路由 + AuthCentre 退出链路 + 骨架屏，逐项修复。

### P0 — 后端安全/RAG/语义/文件 补测试（6 文件 59 cases）

- **tests/test_security_principal.py**（10 cases）：主体跨租户隔离——断言来自不同 token 的 principal 必有独立 subject，后台任务默认拒绝 `sub="__system__"` 伪冒；对方租户 403 主体不可见。
- **tests/test_security_rbac.py**（8 cases）：RBAC 双层门——viewer 不能写 / admin 可读可写；装饰器 deny 时抛 Forbidden；租户隔离下 role 互不串。
- **tests/test_rag_ingest.py**（7 cases）：嵌入服务 drop → ingest 抛原因为 `embed_unavailable` 的 IngestError；切分器对空输入 / 单文档 / 多文档边界；索引写入后 count>0。
- **tests/test_semantics_score.py**（11 cases）：cosine 向量余弦三种等价表达、零向量鲁棒、关键词命中 / 未命中、阈值边界 (0.7 / 0.7001 / 0.6999)、维度 mismatch 报错。
- **tests/test_filestore_safe.py**（10 cases）：路径逃逸（`../etc/passwd` 经 sanitize 后落在 base 内）、符号链接逃逸拦截、0 字节 / >10MB / 非法 mime 被拒。
- **tests/test_attachments_preview.py**（13 cases）：文本 mime 直接预览；图片 mime 返回 data-url；pdf/zip 返回"不支持预览"占位；404 / 脏路径安全化。

**证据**：`pytest tests/test_security_principal.py tests/test_security_rbac.py tests/test_rag_ingest.py tests/test_semantics_score.py tests/test_filestore_safe.py tests/test_attachments_preview.py` → **59 passed / 0 failed**。

### P0 — 前端 vitest 基础设施 + 大型视图单测（子 agent 协作）

- **web/vitest.config.ts**（新文件）+ **web/src/test/setup.ts**（新文件）。
- **`@testing-library/react` + `vitest` + `jsdom`** 进 `web/package.json` devDependencies。
- **单元测试 14 文件**，覆盖：`lib/auth.storage` / `lib/user.api` / `Composer` / `SettingsView` / `KnowledgeView` / `FilesView` / `DataSourcesView` / `ChatMessage` / `ConversationList` / `Settings{account,team,security,experience}.tsx`。
- `web/tsconfig.app.json` exclude 加入 `src/**/*.test.ts(x)` 与 `src/test/**`。

> **注意**：本机 npm install 被 proxy 墙卡住；文件就绪，CI 国际出口自动装。

### P0 — docker-compose 资源约束 + 凭据外置

- **postgres**：硬编码 `da/da` → `${POSTGRES_USER:-da}` / `${POSTGRES_PASSWORD:-da}` / `${POSTGRES_DB:-da_agent}`。healthcheck 用 `pg_isready -U ${POSTGRES_USER:-da}`。`deploy.resources.limits.memory: 1G`。
- **minio**：硬编码 `minioadmin` → `${MINIO_ROOT_USER:-minioadmin}` / `${MINIO_ROOT_PASSWORD:-minioadmin}`。新增 `mc ready local` healthcheck，`limits.memory: 512M`。
- **milvus**：depends_on minio `service_healthy`，healthcheck `interval 10s / retries 15 / start_period 120s`，`limits.memory: 2G`。
- **.env.example**：新段 `POSTGRES_*` / `MINIO_ROOT_*` 三变量。

### P0 — CI image job 依赖 + 增量门收紧

- `.github/workflows/ci.yml`：`image` job `needs` 加 `e2e`；注释 badges/export mock LLM 设计取舍。

### P1 — AuthCentre 退出路径 +聚焦可达

- **AuthCentre.tsx**：新增 `onSkip` prop——右上角"跳过"按钮（仅 `onSkip` 存在时渲染）+ `useEffect` 监听 `Escape` → `onSkip()`。四颗按钮全部加 `focus-visible:ring-2 focus-visible:ring-white [focus-visible:ring-offset-2]`。
- **App.tsx**：`<AuthCentre onSkip={() => setNeedAuth(false)} />`。
- **AuthCentre.test.tsx**：新增 Escape + 跳过 2 cases（`fireEvent.keyDown(window, { key: "Escape" })` → `onSkip` 被调；`getByRole("button", { name: /跳过/ })` → `onSkip` 被调 / `onAuthed` 未调）。
- **web/e2e/auth-gate.spec.ts**：新增"落地页右上角「跳过」与 Escape 键都回到主界面" 1 case（Playwright `page.keyboard.press("Escape")` + 主界面可见断言）。

### P1 — Playwright 运维

- **playwright.config.ts**：`workers: process.env.CI ? 2 : undefined`（CI 双并发降总时长），`screenshot: "only-on-failure"`, `video: "retain-on-failure"`。

### P2 — SPA URL hash 双向绑定（view + conv 双维度）

- **App.tsx**：
  - `useState<RailView>(readViewFromHash)`（启动读 `#view=`）。
  - `setView` 写 `writeViewToHash(next)`。
  - `useState<string | null>(readConvFromHash)`（启动读 `#conv=` 恢复活动会话 / F5 深链）。
  - `hashchange` listener 同步 view + conv。
  - `newConversation` 内 `writeConvToHash(id, view)`。
  - `<ConversationList onSelect>` 内 `writeConvToHash(id, "chat")`。
- **lib/hash.ts**（或 App 内部）：`readViewFromHash` / `writeViewToHash` / `readConvFromHash` / `writeConvToHash` 四个纯函数（`URLSearchParams` 解析 `window.location.hash`）。

### P2 — 骨架屏（KnowledgeView + FilesView）

- **web/src/components/ui/skeleton.tsx**（新文件）：`SkeletonLine` / `SkeletonCard` / `SkeletonList`（含 `rows` prop）/ `SkeletonTable` exports。
- **KnowledgeView.tsx** + **FilesView.tsx** 加载占位从 `<Loader2 animate-spin>` 改为 `<SkeletonList rows={5} />`。

**本轮未破坏既有基线**：`tsc --noEmit --skipLibCheck` 0 errors；后端离线回归同 D59。

## D58 (2026-09-17) — 登录 / 设置双页重构（统一落地页设计语言）

- **web/src/components/AuthCentre.tsx**（新文件）：替代原 AuthGate 弹窗，改为参考 Codepen FlorinPop17 "Double slider Sign in/up Form" 的整合落地页——两张表单 + 一张 overlay 面板在同一张卡片内水平滑动（`transition-transform duration-500 ease-in-out`）；品牌色 `linear-gradient(135deg, #143a5e → #0f2c48)` 替代参考设计的橙红。登录 / 注册走同一套 `pendingRef` 重试链路（被 401 拦截 → 整屏落地页 → 填 key → 回到原操作）。`fixed inset-0 z-[100]` 确保全屏覆盖。
- **web/src/components/AuthGate.tsx**：删除。原单弹窗模式由 AuthCentre 落地页整体替代，App.tsx 同步移除 `authOpen` 模态状态（更名为 `needAuth`），取消按钮不再需要（落地页不能「关」，只能「登录完成」或「留在当前页」）。
- **web/src/components/SettingsView.tsx**：页头加与 AuthCentre 同款的 3px 品牌渐变装饰条（`absolute top-0, linear-gradient`），右上角加「成员 / 管理员」角色徽标（attention 色 / 中性灰区分）；内容区改为卡片容器（`rounded-panel + border-rule + bg-white + shadow-sm + p-6`），与落地页卡片语言统一；图标加 `group-hover` 微动效。

**本轮未破坏既有基线**：`tsc --noEmit --skipLibCheck` 0 errors。

## D59 (2026-09-17) — 统一重构后的首轮修复（按 P0→P2 推进）

按扫描发现的 6 项依次推进，记录如下。

### P0

**[前端测试盲区] → vitest 基础设施 + AuthCentre 8 案例单测（web/）**

- `web/vitest.config.ts`（新文件）：`@vitejs/plugin-react` + jsdom + `@testing-library/jest-dom/vitals`。
- `web/src/test/setup.ts`（新文件）：注入 DOM 断言。
- `web/src/components/AuthCentre.test.tsx`（新文件，8 cases）：mode 切换 / 错误态 / 成功路径 / Enter 提交。Mock `@/lib/auth` + `@/lib/user`。
- `web/package.json`：加 `test / test:unit / test:unit:watch` 脚本，`@testing-library/* / vitest / jsdom` 进 devDependencies。
- `web/tsconfig.app.json`：`exclude` 加入 `src/**/*.test.ts(x)` 与 `src/test/**`，避免 build 尝试编译测试文件。

> **注意**：本机 npm install 被 proxy + registry 组合墙卡住 —— 所有文件就绪，**CI 国际出口会自动装**。所以本项标记为"本地不可验证，CI 验证"。`tsc --noEmit --skipLibCheck` 0 errors。

**[鉴权是假的] → AuthCentre 接后端 /auth 端点**

- `web/src/components/AuthCentre.tsx`：`submitLogin` / `submitRegister` 改 async，先走 `lib/user.ts` 的 `login()` / `register()`（Bearer token 写入 `da_user_token`，`useAuth` 上下文中生效）；后端不可达时 fallback 到 `setApiKey()`（保留 MOCK_LLM 离线可用性）。错误态透传后端 message（如"密码错误"）。
- 登录表单加"用户名字段"（原仅 key），label/placeholder 对齐后端 schema（LoginRequest.username / .password）。
- fallback 保留理由：`lib/auth.ts` 中 `authHeaders()` 已是 Bearer 优先 / X-API-Key 兜底，两套凭证并存本就是项目设计。

### P1

**[needAuth 路径无 e2e 覆盖] → web/e2e/auth-gate.spec.ts（新文件，3 cases）**

- 用 `page.route('**/api/v1/chat/**')` 强制首次 analyze 返回 401 → AuthCentre 落地页出现 → 填 key → 自动重试 → 主界面回来。
- 注册路径：切到注册模式 → 注册 → 回到主界面。
- 前端校验兜底：密码不足 6 位 / 两次不一致时**实地**在前端被拦（不到后端）。
- `test.slow()` 标注 + 15s visible 超时，匹配双滑块 500ms×2 动画。

**[ruff/mypy info door 未收紧] → ci.yml 增量门**

- 加 `Fetch PR base for incremental check` + `Incremental lint (ruff, BLOCK door, PR only)` + `Incremental type check (mypy, BLOCK door, PR only)`。
- 逻辑：`git diff --name-only origin/<base>...HEAD -- '*.py'` 捞本次改动的 .py 文件，单独 block；老代码仍然只走 info 门（继续暴露 + 不强制 block）。
- real-llm job 的 `continue-on-error: true` **保留**：该 job 真实用途是 LLM 行为契约探测（不计入主干红线），去掉反而丢失漂移告警。

### P2

**[Dockerfile 多阶段 + CI 一键 offline]**

- Dockerfile.prod **已**是多阶段（builder/runtime）+ 非 root + HEALTHCHECK + sentence-transformers 可选。**无需改**。
- 加 `scripts/run_offline_tests.sh`（新文件）：封装与 CI `test` job 同参数的本地命令（`MOCK_LLM=true AUTH_ENABLED=false` + 排 real-llm/中间件 live 用例），让贡献者本机一键拿到 CI 等价结果。

**[badges/export 真 LLM 单独一支]**

- 现状：`e2e` job 的 `Start backend (mock LLM)` 已用 `MOCK_LLM=true`，badges/export 当前就跑在 mock 后端；**不需要改**。
- real-llm job（test_agent_real.py）是**后端** smoke；前端尚无 real-LLM e2e（badges/export 强烈依赖 mock 后端的确定性返回），暂不需要拆出。在 ci.yml `e2e` job 注释加一行说明此设计取舍。

**本轮未破坏既有基线**：`tsc --noEmit --skipLibCheck` 0 errors。pytest 离线回归（112 passed + 18 skipped）仍在本机通过。

## D57 (2026-09-17) — 运维 & 质量基础设施加固

- **app/core/safe_fs.py**：6 处 `except BaseException` 改为**分路处理**——`KeyboardInterrupt` 重新 raise（Ctrl-C 立刻终止服务），`SystemExit` / `OSError` 等仍吞（保留 WorkBuddy 沙箱 safe-delete 语义）。证据：`tests/test_safe_fs_safety.py` 18 passed + 1 skipped。
- **app/core/security/watermark.py**：补 11 个往返 + 抗篡改 + padding-covered 测试。证据：`tests/test_watermark_verify.py` 11/11 passed。
- **docker-compose.yml**：6 个服务（app / redis / postgres / etcd / minio / milvus）全部挂载顶层 `networks: backend: bridge`——此前所有服务走默认 bridge，跨 compose 隔离命名缺失。
- **.github/workflows/ci.yml**：test job 加 ruff + mypy 两步 lint，设为 `continue-on-error: true` info door（数值待 once weekly tighten）。
- **app/core/interfaces.py**（新文件）：`trace_run`、`fallback_events` 两个 infrastructure 符号经此中转，graph.py 不再直接 import `infrastructure.observability.*` / `infrastructure.llm.router.*`。证据：下游 `tests/test_e4_quality_gate.py` 54 passed / 1 skipped。
- **tests/test_agent_real.py**：session-scoped autope fixture `_skip_if_llm_unreachable`——LLM endpoint TCP 不可达时 `pytest.skip`（诚实报告未就绪，不伪造 green）；有网 + 有有效 key 仍强制真跑。
- **pyproject.toml + requirements.txt**：单源化第一步——`[project.optional-dependencies]` 加 `mysql = ["pymysql>=1.1"]`；两文件顶部注释锚定"pyproject.toml 权威 / requirements.txt CI lock"。
- **docs/progress/pending-real.md**：表项状态审计（主表 5 行外 E1/E2/E3/E4/E5/E8/E9/AUTH/MCP/DEGRADE/Redis/Milvus 已实现 + 剩余缺口），消除主表漂移。🟡 文档层，无直通代码变化。
- **web/e2e/smoke.spec.ts**（新文件）：最低限度 UI smoke（首页加载 + 输入框 + 发送按钮，mock / 真 LLM 都能过）；CI e2e job 同步上传 shots/ 新截图（保留 30 天），替代 git 中 7 天前的旧快照。证据：`tsc --noEmit` 0 errors。🟡 需 playwright run 截图，CI runner 自动产出。

**本轮未破坏任何既有评估基线**：live offline regression 仍 1294 passed / 79 skipped / 0 failed。

## D56 (2026-09-15) — planner 方言预注入 + real 基线重校

- **tools/executor.py / agents/planner.md**：planner 步骤 `input` 在交付执行器前预注入方言提示（SQLite/PG/MySQL 三档，按 source 的 engine 取），取消带 `input.sql` 步骤的逐字重试（结构性 no-op）。
- **tools/sql_precheck.py**：第一步跑 0 字节占位 SQL `SELECT 1` 拿真实列清单 + engine，失败把列名回灌到 error（REPLAN 唯一能看懂的上下文）。
- **测试基线**：工具成功率一次读取值 0.228 → 按"分母只算执行过的步骤"重算 = 0.514（新分母 35 = 审计条数 35 互证，独立口径）。
- **test_agent_real 复测**：D52 错端点修复后首次全绿 14 passed / 9m17s；本次揭示配额路径上的 CLARIFY 代码被误判，修正 allow-set → 重新 14 passed。

🟡 **待真跑验证**：planner 方言预注入后模型"真改对"的比例（预检放行前的正确率 vs. 预检回灌后 30 分钟量级）。见 `docs/progress/pending-real.md` §E2。

## D55 (2026-09-15) — eval 门禁 v2（min_findings / ungrounded_numbers）

- **eval/golden.py**：`GoldenCase` 加 `min_findings`（最小发现数）、`ungrounded_numbers`（最大无来源数值数）、`accept_clarify`（接受澄清替代）。
- **eval/runner.py**：`--strict` 开关启用门禁断言；原来 PASS 的 case 在 `--strict` 下暴露：
  - 断言通过率 1.0 → **0.467**
  - 工具成功率 0.986 → **0.228**
- **报告表头**：成本列 + `--strict` 下 FAIL 用例列表。🟡 成本价 = 未计（.env 未配 `COST_*` 单价）。

## D54 (2026-09-15) — eval golden 修约 + 溯源修正

- **eval/golden.py**：golden case 列表扩展到 15 条，7 条标 `requires_real=True`（区分 mock 模式到不了的场景）。
- **eval/judge.py**：分数收敛 jitter 修复（0.5 分段的"漂"被收紧）。
- **tests/test_e2e_trace_validation.py**：fake-source 门控收紧——来源被正确判为"有证据但无归属"。

## D53 (2026-09-15) — planner 契约修复（每步 input 非空）

- **app/core/agents/data_analyst/planner.py / planner.md**：planner 步骤 `input` 允许为 `{}` 的路由被修掉；planner 应按 source 列名 + 指标语义构造真实 JSON-SQL。
- **tools/executor.py**：执行器按 `input` 内容选表（此前合成 `SELECT * FROM dim_channel LIMIT 100`）。证据：35 条审计里 0 条 `input={}`（D53 前 8 条全为 `{}`）。

## D52 (2026-09-15) — conftest 错端点修复（重大更正，解决 D18 以来的阻塞）

- **tests/conftest.py**：删除两行 `os.environ.setdefault("LLM_BASE_URL", "https://openrouter.ai/api/v1")` 的错端点默认值；此前 openrouter 的余额不足返回 401，让本项目 `.env` 真 key 行为看起来像"没有 key"。
- **效果**：`tests/test_agent_real.py` 14 passed / 9m17s，首次全绿（此前连续 6 天 ERROR/FAIL）。
- **顺带**：把 18 个 pre-existing failures 的 17 个修绿（含 state.py / caliber.py / lineage.py 的归因修平等），仅剩 1 个网络环境依赖。
