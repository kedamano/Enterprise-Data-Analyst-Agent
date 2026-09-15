# Enterprise Data Analyst Agent × 企业级 Agent 对标 Gap 清单

> **回写说明（2026-09-13）**：本清单最初写于项目早期，此后**从未回写**——
> 导致它与 `优化方案-基于Anthropic七模式.md`、`面试稿_STAR.md`、`部署上线.md`
> 在"并行执行/多模态/导出/脱敏/镜像体积"等六处**互相矛盾**，读者无法分辨哪条还成立。
>
> 本次按**代码实况**逐条复核（不采信任何文档的自我声明），给每行加 `状态` 与 `证据` 两列。
> 状态口径：**✅ 已消除**（该行差距整体不再成立）/ **⚠️ 部分**（仍有明确残留，见证据列）
> / **❌ 仍缺**（整体仍成立）。
>
> **复核结果（D57 重数，2026-09-15）：22 条能力差距中，✅ 6 条完全消除 · ⚠️ 13 条只剩部分残留 · ❌ 3 条仍成立。**
>
> 上版此行写的是 `✅ 3 / ⚠️ 13 / ❌ 6`——**与表里的实际标记对不上**：
> 表确实有 22 行（总数没写错），但后续几天**有 3 行升了级**（按总数差可推：3 行进了 ✅，
> 其中 2 行原先标 ❌、1 行原先标 ⚠️）时，**没有同步这行汇总**。
> 汇总行是这份文档的"记分板"，**它比表本身更容易被读**——记分板与表不符时，
> 读到的是**低估了已消除、高估了仍缺**。现在按逐类计数对齐（`✅ 6 / ⚠️ 12 / ❌ 4 = 22`）。
> **纪律：改任何一行的状态标记时，同一笔改掉这行汇总。**

> 定位判断（**仍成立**）：
> - 作为**面试作品 / 数据 Agent 原型**：优秀，工程护栏密度远超大多数候选人。
> - 作为**企业级生产 Agent**：仍**未达标**——缺的是数据治理深度、合规落地、多租户运维、
>   真数据真业务验证。缺的**不是**前端。

---

## 一、自主能力（Agent 本体）

| 现状（已实现） | 企业级要求 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|---|
| Executor 确定性参数合成 + 固定 7 工具模板（`nodes.build_executor_params`） | LLM **自由写 SQL/Python**、按需调用，沙箱执行 | 最大短板：只移植了沙箱，没移植自由写码 | **✅ 已消除** | E2 已落地自由写码：`freeform` 工具（模型给只读 SQL）+ `pycode.deliver_python_code`（模型写脚本 → AST 守卫 → 沙箱验证 → 失败结构化回注重试）。工具 **7 → 9**（+`freeform`/`image_analyze`）。**残留**：真实模型下写码**质量**未单独度量（见 §七） |
| 六节点串行流水线（`graph._drive_sync`） | 动态并行子任务、按需扩展 | 全串行、无 DAG / 并行调度 | **✅ 已消除** | `run_executor_all`（`nodes.py:857`）+ `parallel_executor_workers`（默认 4），`graph.py:161/320` 同步与流式都接入；实测多工具步骤并发峰值 ≥2 |
| JSON schema 阶段输出 | 原生 tool_calls + **并行工具调用** | 无 parallel tool calling；无 tool_choice 强制某阶段 | **❌ 仍缺** | 全项目无 `tool_choice` / 原生 `tools=[...]`；工具并行是**自研波次调度**，不是协议层能力 |
| 单 Agent 多角色阶段 | 需要时多 Agent / 子 Agent 委托 | 无（如"报告评审 Agent"做第二意见） | **❌ 仍缺** | 无子 Agent 委托机制 |

---

## 二、数据 / 领域层

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| 样例 SQLite + 预置表；schema_search 只读元数据 | 无真实企业库 / 多方言真实场景；无数据血缘；无列级权限 / 脱敏分级；无查询成本 / 行数配额按租户 | **⚠️ 部分** | 多方言 ✅（三方言 bench，`bench_scale.py --backend sqlite\|postgres\|mysql`）；真实库 ✅ 部分（**MySQL 8.0.26 live 13 passed**、**PG live 4 passed**）；数据血缘 ✅（E1 `sources.py`：数值 claim → `sql_id` → 可点开的 SQL 步骤，覆盖率实测 1.0）；列级权限 ✅（AUTH/01 `allowed_tables`/`denied_columns`/`row_filters`）；脱敏分级 ✅（E4/02 `sample`/`strict`/`none`，默认开）；租户配额 ✅（`quota_per_min`）。**残留**：**未接真实业务库**（只有合成 1M 行库） |
| 知识 = 文档 chunk；PDF 仅 pypdf 抽文本 | 无表格 / 版面理解、无 OCR、无父子 chunk、无索引版本 / 双写一致性、无坏 chunk 回流 | **⚠️ 部分** | **表格 ✅**（D57 `etl/chunker.py` `chunk_structured`：HTML `<table>` / Markdown `|..|` / CSV 三类表识别，按行组 chunk + 表头 + 前文前缀注入；残缺表（无体 / 单列 / 分隔线断裂）静默降级整块 chunk 不丢数据；无外部依赖）。**索引版本 ✅**（D57 `KnowledgeStore.rebuild_source`：content-hash 幂等，重传同文 → `added: 0`；版本号按 `(source, tenant, kb_id)` 三元组自增；旧版标 `deprecated=1`，检索自动剔除；`cleanup_old_versions(keep=2)` 物理删老版）。**坏 chunk 回流 ✅**（D57 `_classify_chunk` 在嵌入前运行：`empty`(<10字) / `noise`(>80% 标点) / `ok` / `embed_failed`(嵌入不可用)；`status` + `status_reason` 入 DB；`chunk_diagnostics()` 返回 `{total, ok, empty, noise, embed_failed, deprecated}`；`embed_failed` 的 chunk BM25 仍可召回——结构仍保底）。**嵌入失败自愈 + 面板 ✅**（D58 `E8/02`：状态机 `embed_failed ──(retry 成功)──→ ok` / `embed_failed ──(fail>max)──→ abandoned`（新增终态，**自动从检索结果中排除**）；候选查询 `failed_count <= max_retries` 给 count=max 最后一次重试机会；`retry_embed / abandon_expired / list_embed_failed` 三个方法加 Milvus stub 兜底；调度器 `embed_scheduler.py`（daemon 线程，立即首跑 + interval 循环；`interval_s<=0` 禁用；每 tick 发 Prometheus 指标）；chunk 质量运维 GET `/{kb_id}/diagnostics` 扩展返回 `{abandoned, embed_failed_aging: {1h,1d,7d,older}}`——aging 直方图让运维能看到"24h/7d/older" 三层堆积；管理 POST `/admin/embed-failed/retry|abandon|list`）。**TTL 默认 30 天**自动清掉不再重试的老 chunk，解决 `embed_failed` 无限堆积问题；**config** 四字段 `embed_retry_max=3 / embed_retry_batch=100 / embed_retry_interval_s=3600 / embed_failed_ttl_s=2592000`。**残留**：版面理解、OCR、**父子 chunk**（D57 SDD §3 边界明确「不做」）；索引一致性、向量与源文档权限对齐仍缺（见下文 §四 Milvus 行）；root-cause 诊断（OOM / 模型缺失 / 超时就同一命运）。**嵌入向量版本迁移 ✅（D59）**：schema 加 `embed_model_version TEXT` + 二级索引 `chunks_emv(embed_model_version, kb_id)`（共 **15 列**）；`add()` 在 INSERT 时用 `_resolve_emv()` 打当前版本戳；`search()` WHERE 自动拼 `embed_model_version IS NULL OR = ?`（兼容老数据），`include_stale_versions=True` 跳过过滤；`set_emv_override(v)` 运行时改进程内存态、**重启恢复 settings 默认**（有意设计：rotate 是运维决策，不该跨重启自动生效）；`reembed_chunk(id)` 单条升级+成功后写新版本；`reembed_batch(kb_id, target, limit)` 批量迁移，**跳过 `status != 'ok'` 的行**（embed_failed 的 chunk 保持旧版本）；`version_stats(kb_id?)` 返回 `{by_version: [{version, count}], stale}` 分布——方便 rotate 前后做 re-embed 成本估算；`retry_embed` 成功后写 `embed_model_version = _resolve_emv()`，让修复后的 chunk 自动对齐当前版本；`/api/v1/knowledge-bases/admin/embed-version|rotate|chunks/{id}/re-embed|embed-version/migrate` 四个管理端点；conftest `_reset_state` 每测试 `set_emv_override(None)` 防跨测试残留。**边界**：Milvus 只给 stub（三方法抛 `NotImplementedError`，等 SDK 接入时补全）；rotate 之后**不自动**触发 re-embed——管理员必须主动调 `migrate`（防止一次误操作把全库向量都重算）；不同版本的向量空间仍用同一内积打分（没有版本间距离归一化，留给后续多跳/EN 链路时考虑）。**D59 SDD**：`docs/specs/E8/03-embed-versioning.md` **D57 SDD**：`docs/specs/E8/01-knowledge-depth.md`；**改动**：`etl/pipeline.py` `ingest_text` 改用 `store.rebuild_source` → `chunk_structured`（原 `chunk_text` 保留为纯文本兜底）；`core/tools/knowledge_tool.py` 懒迁移加 `version/status/status_reason/deprecated/content_hash/kb_id`；D59 增 `embed_model_version` + 二级索引
| 中文、通用术语 | 无行业词表、无 query 改写 | **⚠️ 部分** | 业务语义层已做（SEMANTIC/01：维表枚举 + 命名约定键推断 → 注入 context/planner/analyst）。**残留**：行业词表、query 改写仍未做 |

---

## 三、记忆 / 上下文工程

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| 短期滚动摘要 + 教训回流 + checkpoint | 无**跨副本一致性**；无 TTL / 遗忘策略；无结构化长期槽位 | **⚠️ 部分** | Redis **已真跑**（`test_redis_live.py` 2 passed，此前恒 skip；端到端证实会话 hash 落 Redis）；TTL ✅ 已补（`SHORT_TERM_TTL_S` 默认 24h + 滑动续期，此前键**永不过期**）。**残留**：跨副本一致性（设计仍单副本）、结构化偏好槽位 |
| token 预算辅助（`memory/budget.py`） | **prompt 组装未真正按预算强制执行**；无**单会话 token 熔断** | **✅ 已消除** | **预算强制 ✅**（D42 + D43，本条两个子问题都已解决）：① `prompts/budget.py` **序列化前**按优先级压缩（行数据减半 → 丢最旧工具结果 → 只留 context/plan），说明写进 prompt 的 `<context_budget>` 块；② `infrastructure/llm/token_budget.py` **按 run 的 token 熔断**——超预算即**拒绝下一次调用**（检查在调用前、且在重试内部），不可重试 + 留痕。**边界**：熔断按 run（一次 Agent 执行）而非跨多轮会话（透传 session_id 要动全部调用点，未做） |

---

## 四、RAG / 检索

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| BM25(+CJK 二元组) + 向量 RRF + 短语亲和重排 | 无 query 改写、多跳 / 子问题拆分、检索-生成 **fail 兜底**；无检索质量在线分维 | **⚠️ 部分** | 检索质量分维 ✅（`eval/rag_eval.py`：hit@k / recall@k / MRR / faithfulness 下界，实测 1.0 / 0.747，且能分辨含伪造数字的样本）。**低置信 fail 兜底 ✅（D53）**：`rag/confidence.py` 按首条短语亲和分级，低置信**清空 chunks**（结构性保证，非 prompt 纪律）+ `rag_low_confidence_total`；阈值 0.15 由黄金集两侧标定（6 正例 0.188~0.500 全 high / 负例 0.111）、在 `RERANK_ENABLED` 开/关下判级一致。**嵌入失败自愈 ✅（D58）**：`abandoned` 终态自动从召回池移除（`status IN ('ok','embed_failed')`），不污染检索；`embed_failed` 仍走 BM25 兜底。**残留**：query 改写、多跳拆分、字面重合同的语义误判（"华东区域的年会在哪里办"仍判 high） |
| rerank 确定性实现，cross-encoder 为钩子 | cross-encoder 未真正上线；需离线预缓存模型 | **⚠️ 部分** | 短语亲和确定性重排已上线且 `rerank_enabled` 默认 **True**。**残留**：cross-encoder 仍是可选钩子（本机加载脆弱） |
| Milvus 可选后端 | 无索引一致性、embedding 版本迁移、向量与源文档权限对齐 | **⚠️ 部分** | Milvus **服务端**已真跑（`test_milvus_live.py` 5 passed / 1 skipped，此前只跑过 Lite）；租户隔离已修（Milvus schema 增 `tenant` 字段 + filter）。**嵌入版本迁移 ✅（D59）**：KnowledgeStore 侧完整实现 `embed_model_version` 写入 + `search()` 版本隔离 + `reembed_chunk/reembed_batch/version_stats/get_current_embed_version` 四个方法，Milvus stub 抛 `NotImplementedError`（SDK 等接入时补全）。**残留**：索引一致性、**Milvus 真后端时 embedding 版本迁移**（当前仅 SQLite 后端闭环）、权限对齐 |

---

## 五、安全 / 合规 / 治理（企业级红线区）

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| SQL 只读 + 白名单 + 沙箱 + 审计 JSONL + Prompt 注入分层 | 缺 **RBAC / 细粒度权限**；缺 **两步授权 / 高危确认 HITL**；审计为文件非不可篡改库；无间接注入对抗集；**无输出 PII 脱敏** | **⚠️ 部分** | RBAC ✅（AUTH/01 用户级鉴权 + 角色→权限门禁，`test_rbac.py` 11 + `test_auth_permissions.py` 18）；细粒度权限 ✅（表/列/行三级）；**输出 PII 脱敏 ✅**（E4/02 三级 + 值形态兜底 + **失败即关闭**，并写审计）；SQL 只读守卫已加固（`test_sql_readonly_guard.py` 33 项）；**两步授权 HITL ✅**（D45：**默认拒绝 + 显式白名单** + fail-closed + 凭证不可推导；`export_raw`/`deliver_python` 需 `POST /analyze/confirm`；允许与拒绝**都审计**；默认关=零影响）；**审计落库 ✅**（D46：四条流统一经 `security/audit_store.py`，`jsonl`/`sqlite`/`postgres` 三选一；`import_jsonl()` **内容指纹保证幂等**；`GET /debug/audit` SQL 查询）；**DLP 细粒度脱敏 ✅**（D49：角色×字段级别解析，`Principal.denied_columns` **权限高于策略**；HMAC-SHA256 **可验证**水印；导出审批流——脱敏版安全默认、原始版走 HITL；默认零影响）。**残留**：间接注入对抗测试集；水印真实接收方校验链路未验 |
| 多租户列（懒迁移，空=全局） | 默认单租户、浅实现；缺租户配额 / 隔离的接口强制与测试矩阵 | **⚠️ 部分** | 租户配额 ✅（`quota_per_min` → 429）；Milvus 租户隔离 ✅；**隔离测试矩阵 ✅**（D44 `test_tenant_matrix.py`：4 个数据面 × 2 条腿——跨租户不可见 **+ 同租户可见**（后者防「功能全坏也算隔离」的假绿）；另有"空租户=全局"兼容性用例，文件头附覆盖面指路表）。**残留**：默认仍单租户（`default_tenant=""`），这是**刻意的向后兼容**而非缺陷。**D55 补一处矩阵之外的洞**：矩阵测的是**跨租户**，而"**两个都不带 `session_id` 的调用方共用同一个桶**"根本不在矩阵的维度里——上传接口默认值**就是** `"default"`（`Form(default="default")`），`chat.py` 用 `req.session_id or ""`，经 `sidecar_path` 归一后**还是同一个桶**（实测 `attached_tables(None)` 与 `("")` 返回同一批）。已由 `AUTH/02` 的 `resolve_session_id`（在 API 边界**生成**而非拒绝）修掉，`default` 兜底保留但不再可达。**教训：要能被质疑的是矩阵的"维度"本身**——只测"有身份的两个主体彼此不可见"，就看不见"**没有身份**的主体之间共享" |

---

## 六、可靠性 / 可运维

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| 三态熔断 + 多模型路由 + 降级 + 重试；checkpoint 续跑 | 无 **k8s / 优雅伸缩 / 差异化探针**；无背压 / 排队；无**异步任务 + 回调** | **❌ 仍缺** | 无变化（长分析仍只有同步 / SSE） |
| 结构化 span JSONL + `/debug/traces` | 无 **OTel / 指标 / 告警 / SLI-SLO**；日志无采样；无分布式 trace；**无 P95 监控** | **⚠️ 部分** | **OTel 钩子 ✅**（`metrics.export_otel()`，`opentelemetry` 缺失则返回 False 不影响主流程）；**Prometheus `/metrics` ✅**；**P50/P95/P99 ✅**；告警规则 ✅（`docs/observability-alerts.md`）；降级可见化 ✅（`degraded`/`llm_fallbacks`/`/health/llm`）；**SLI/SLO ✅**（D44：6 条 SLI + 口径纪律；顺带补 `llm_fallbacks_total`——降级率此前**算不出来**）；**日志采样 ✅**（D44：只采正常、失败永不采样）。**残留**：多服务分布式 trace；采样后的日志未接入聚合 |
| Dockerfile + `run-container.sh` | 镜像 11.3GB 未瘦身；无 CI/CD / 镜像仓库 / schema 迁移 | **⚠️ 部分** | `Dockerfile.prod` ✅（多阶段 / 非 root / HEALTHCHECK / **默认不装 torch**）；**CI ✅**（`.github/workflows/ci.yml` 四 job：离线回归 → Playwright E2E → live（MySQL 容器 + Milvus Lite）→ 镜像构建 + **`<2GB` 硬断言**）。**残留**：**镜像体积 `<2GB` 仍未实测**（本机镜像源全不可达，非"无 Docker"）；镜像仓库、schema 迁移 |

---

## 七、评测 / 质量 / 成本治理

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| eval 骨架（`app/eval`）；防假绿（conftest） | golden 少、纯自建；无业务标注集 / 人工评审 / badcase 回流；无 retrieval-vs-忠实度分维；无离线-在线一致性；无幻觉率监控 | **⚠️ 部分** | golden **5 → 15+ 条**（含 7 条 `requires_real` 门控）；**真实基线已产出并逐版收敛**（D37→D38：工具成功率 0.36→**1.0**、findings 全 0→**非 0**、断言 0.429→**0.143**——最后一次"变低"是**去掉了两类假绿**）；防假绿 ✅ 已从测试侧走到**运行态**（`DEGRADED` 剔除 + `scored_cases`/`degraded_excluded` + ⚠️ 告警块；LLM-judge）；**badcase 回流 ✅**（D39：落盘/去重/复跑/草稿 + `--badcases`）；**幻觉率监控 ✅ + 离线-在线一致性 ✅**（D41：唯一口径 `sources.trace_coverage()`，离线 eval / `/trace` API / SSE / `/metrics` 全走它，并有测试钉死"同 state 同值"；零 claim → `None` 不报 0）。**残留**：业务标注集 / 人工评审 |
| 成本实算需配单价 | 无**按租户 / 场景预算封顶与告警**；无语义缓存 | **⚠️ 部分** | 成本单价 ✅（`cost_*_per_mtok`，并已区分"未知"与"已知免费"）；结果缓存 ✅（`response_cache.py`，会话内隔离、只缓存可信 FINISH、`force_full_rerun` 绕过）。**残留**：按租户 / 场景预算**封顶与告警**、语义缓存 |

---

## 八、产品 / 交互 / 生态

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| `/ui` 对话控制台 | 非产品级：**无引用溯源 UI、无图内嵌、无导出** | **✅ 已消除** | **引用溯源 ✅**（`GET /analyze/trace/{session}` + 报告 `[src:step_id]`）；**导出 ✅**（`GET /analyze/export` zip/sql/report/csv + UI 导出按钮）；协作骨架 ✅（分享/评论/权限）；**D51 补齐图内嵌**（`charts.py` 采集 + 报告 `## 图表` + `GET /analyze/chart/{sid}/{name}`，白名单/目录双层防护，不含 svg）/ **指标卡 ✅**（FINISH 帧下发 `metrics`，归一在后端）+ **导出预览 ✅**（manifest 与 zip 共用 `_build_items`，逐项相等；脱敏版不含位图）。**残留**：报告图引用是绝对 URL，贴到外部平台会裂 |
| 工具为进程内函数 | 无 **MCP** | **✅ 已消除** | D50 `integrations/mcp_server.py`：只读工具（READ_METADATA/READ_KNOWLEDGE/READ_DATA）按 MCP 协议暴露——**可发现**（`list_tools` 返回 schema 取自 `TOOL_SPECS`）/ **可授权**（每个 `call_tool` 走 `execute_tool`，RBAC/限流/守卫/审计全委托）/ **可观测**（权限被拒也落审计）。`python_analysis`/`visualization`/`generate_report` **绝不暴露**（代码执行面）；`image_analyze` 显式排除（耦合附件）。`mcp_enabled` 默认关 → 503。**残留**：与真实 MCP client 端到端握手未验 |
| 多模态 | 不支持（输入纯文本；模型不读图） | **✅ 已消除** | `image_analyze` 工具 + vision 网关分支（`llm_vision_model`）+ Planner 感知；原图落盘。**残留**：报告内嵌图与图表自检（vision 回读） |

---

## 优先级（回写后的**仍成立**排序）

> 原 P0-1（给 `python_analysis` 真写码路径）**已完成**，故整条移出。
> 下列为复核后仍成立的项。

**P0 · 决定它能不能进生产**
1. **真实业务库**接一次并跑真实业务问句（MySQL/PG live 已通过，缺的是**真数据**）。
2. ~~**安全合规最硬两件**：两步授权 HITL、审计落库（非 JSONL）~~ —— **已于 2026-09-14 完成**（D45 HITL + D46 审计落库 + D49 DLP 细粒度脱敏）。
3. **镜像瘦身实测 + 镜像仓库 + schema 版本迁移**（CI 与 `<2GB` 断言已就位，卡在镜像源）。

**P1 · 撑故事 / 加分**
4. ~~**prompt 预算强制 + 单会话 token 熔断**~~ —— **已于 2026-09-14 完成**（D42 `prompts/budget.py` 序列化前按优先级压缩 + D43 按 run 的 token 熔断，超预算即**拒绝下一次调用**）。
5. ~~日志采样 / SLI-SLO；多租户隔离**测试矩阵**~~ —— **已于 2026-09-14 完成**（D44）。**残留**：长任务异步化（价值取决于是否真上生产，见 §9.2 阻塞表）。
6. 检索侧：~~低置信 fail 兜底~~ ✅ **D53 已做**（`RAG/01`）；**残留** query 改写、多跳拆分。
7. ~~badcase 回流；幻觉率监控~~ —— **已于 2026-09-14 完成**（D39 落盘/去重/复跑 + D41 唯一口径 `sources.trace_coverage()`）。
   **幻觉率已于 2026-09-15（D54）从"指标"升为"可否决的门禁"**：此前 `疑似幻觉率 0.667` 对 ✅/❌
   **毫无影响**——`q_region_top` 只跑了一条 3 行维表查询却**凭空造出一整张区域营收表**仍判 ✅。
   根因是溯源只查 `findings[].evidence[].value`，**报告正文的数值从不检查** →
   `ungrounded_numbers`（正文大额数值必须找得到出处）+ 5 个基础 `q_*` 补 `min_findings`
   + `--strict` 退出码 2。**回放真实产物时又挖出一层**：第一版把 `evidence[].value`
   **无条件**当出处，而那次编造的表**同时**写在 evidence 里（`sql_id` 指向一条 3 行维表查询）
   ——等于**让模型的自我声明给自己作证**，E1 溯源反成洗白通道；改为
   「evidence 的值必须在**它声称的那条 SQL 输出里真能找到**才算出处」（修前抓到 8 条 → 修后 12 条）。
   **残留**：业务标注集 / 人工评审。
8. ~~MCP 接入层~~ —— **已于 2026-09-14 完成**（D50 只读工具按 MCP 协议暴露：可发现 / 可授权 / 可观测）。**残留**：报告图内嵌 + 图表自检；与真实 MCP client 的端到端握手未验。
9. ~~`dataset_profile` 规模瓶颈~~ —— **已于 2026-09-13 修复**（`profile_column_batch` 合并聚合 +
   `profile_max_columns` 常数上界；121 列 1M 行 15.36s → **5.63s**，且成本与表宽无关）。
   见 `docs/specs/E4/01-profile-quality.md` §7 与 `metrics.md`。

## 一句话结论（回写版）

> **2026-09-15（D54）追加**：真实质量基线那一步走完了，但**第一个结论是"这个基线测的东西不对"**——
> `工具成功率 0.986` 掩盖着"planner 的计划没有 SQL、执行器把每一步都填成同一条
> `SELECT * FROM dim_channel LIMIT 100`、报告据此编出一整张表还判 ✅"。
> D54 把这条链修掉（计划-SQL 契约 + 按内容选表 + 门禁有否决权），
> **"可评测"这一格的含金量因此不同了**：在此之前，评测本身会把"编造"判成通过。

本项目把「**安全执行、可观测、可评测、工程护栏**」做得很像企业级，且这一轮又补齐了
**自由写码、并行执行、真实库 / 真实向量库 live、浏览器 E2E、首个有效真实基线**——
至 D59 复核：E8 知识库深度三连（D57 表格感知+版本+坏 chunk、D58 嵌入失败自愈、D59 嵌入版本迁移）全部闭环。**22 条中 6 条完全消除、13 条只剩残项、3 条仍成立**（§一
「tool_choice」「子Agent」、§六「异步任务」）。**真正还差的三块半**是：**真实业务数据验证
（真业务库 + 真业务问句）、可运维部署体系（镜像体格 + 真实业务数据）、检索深度与数据治理
（跨副本一致性、Milvus 真后端 embedding 版本迁移、向量与源文档权限对齐、版面理解 / OCR / 父子 chunk），
以及多租户**（跨副本一致性、结构化偏好槽位）。

> **其中"合规落地"与"预算治理"两块已于 2026-09-14 完成**（D45 HITL + D46 审计落库
> + D49 DLP 细粒度脱敏；D42 prompt 预算 + D43 单会话 token 熔断）——
> 上面这句是**更早的回写**，此处保留原话以便对照，**不要按它排期**。
> 按 §优先级 的复核结果，**P0 仍成立的是第 1 项（真实业务数据）与第 3 项（部署体系）**。

> 关联：`docs/审计报告.md`（Round 1–9）、`docs/progress/`（DailyLog / metrics / pending-real / live-validation）、
> `docs/开发计划_企业化.md`（§8.3 未完成、§8.4 C 里程碑）。
