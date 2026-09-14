# Enterprise Data Analyst Agent × 企业级 Agent 对标 Gap 清单

> **回写说明（2026-09-13）**：本清单最初写于项目早期，此后**从未回写**——
> 导致它与 `优化方案-基于Anthropic七模式.md`、`面试稿_STAR.md`、`部署上线.md`
> 在"并行执行/多模态/导出/脱敏/镜像体积"等六处**互相矛盾**，读者无法分辨哪条还成立。
>
> 本次按**代码实况**逐条复核（不采信任何文档的自我声明），给每行加 `状态` 与 `证据` 两列。
> 状态口径：**✅ 已消除**（该行差距整体不再成立）/ **⚠️ 部分**（仍有明确残留，见证据列）
> / **❌ 仍缺**（整体仍成立）。
>
> **复核结果：22 条能力差距中，✅ 3 条完全消除 · ⚠️ 13 条只剩部分残留 · ❌ 6 条仍成立。**

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
| 知识 = 文档 chunk；PDF 仅 pypdf 抽文本 | 无表格 / 版面理解、无 OCR、无父子 chunk、无索引版本 / 双写一致性、无坏 chunk 回流 | **❌ 仍缺** | 无变化 |
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
| BM25(+CJK 二元组) + 向量 RRF + 短语亲和重排 | 无 query 改写、多跳 / 子问题拆分、检索-生成 **fail 兜底**；无检索质量在线分维 | **⚠️ 部分** | 检索质量分维 ✅（`eval/rag_eval.py`：hit@k / recall@k / MRR / faithfulness 下界，实测 1.0 / 0.747，且能分辨含伪造数字的样本）。**残留**：query 改写、多跳拆分、低置信 fail 兜底 |
| rerank 确定性实现，cross-encoder 为钩子 | cross-encoder 未真正上线；需离线预缓存模型 | **⚠️ 部分** | 短语亲和确定性重排已上线且 `rerank_enabled` 默认 **True**。**残留**：cross-encoder 仍是可选钩子（本机加载脆弱） |
| Milvus 可选后端 | 无索引一致性、embedding 版本迁移、向量与源文档权限对齐 | **⚠️ 部分** | Milvus **服务端**已真跑（`test_milvus_live.py` 5 passed / 1 skipped，此前只跑过 Lite）；租户隔离已修（Milvus schema 增 `tenant` 字段 + filter）。**残留**：索引一致性、embedding 版本迁移、权限对齐 |

---

## 五、安全 / 合规 / 治理（企业级红线区）

| 现状 | 差距（原始判断） | 状态 | 复核证据 |
|---|---|---|---|
| SQL 只读 + 白名单 + 沙箱 + 审计 JSONL + Prompt 注入分层 | 缺 **RBAC / 细粒度权限**；缺 **两步授权 / 高危确认 HITL**；审计为文件非不可篡改库；无间接注入对抗集；**无输出 PII 脱敏** | **⚠️ 部分** | RBAC ✅（AUTH/01 用户级鉴权 + 角色→权限门禁，`test_rbac.py` 11 + `test_auth_permissions.py` 18）；细粒度权限 ✅（表/列/行三级）；**输出 PII 脱敏 ✅**（E4/02 三级 + 值形态兜底 + **失败即关闭**，并写审计）；SQL 只读守卫已加固（`test_sql_readonly_guard.py` 33 项）；**两步授权 HITL ✅**（D45：**默认拒绝 + 显式白名单** + fail-closed + 凭证不可推导；`export_raw`/`deliver_python` 需 `POST /analyze/confirm`；允许与拒绝**都审计**；默认关=零影响）；**审计落库 ✅**（D46：四条流统一经 `security/audit_store.py`，`jsonl`/`sqlite`/`postgres` 三选一；`import_jsonl()` **内容指纹保证幂等**；`GET /debug/audit` SQL 查询）；**DLP 细粒度脱敏 ✅**（D49：角色×字段级别解析，`Principal.denied_columns` **权限高于策略**；HMAC-SHA256 **可验证**水印；导出审批流——脱敏版安全默认、原始版走 HITL；默认零影响）。**残留**：间接注入对抗测试集；水印真实接收方校验链路未验 |
| 多租户列（懒迁移，空=全局） | 默认单租户、浅实现；缺租户配额 / 隔离的接口强制与测试矩阵 | **⚠️ 部分** | 租户配额 ✅（`quota_per_min` → 429）；Milvus 租户隔离 ✅；**隔离测试矩阵 ✅**（D44 `test_tenant_matrix.py`：4 个数据面 × 2 条腿——跨租户不可见 **+ 同租户可见**（后者防「功能全坏也算隔离」的假绿）；另有"空租户=全局"兼容性用例，文件头附覆盖面指路表）。**残留**：默认仍单租户（`default_tenant=""`），这是**刻意的向后兼容**而非缺陷 |

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
| `/ui` 对话控制台 | 非产品级：**无引用溯源 UI、无图内嵌、无导出** | **⚠️ 部分** | **引用溯源 ✅**（`GET /analyze/trace/{session}` + 报告 `[src:step_id]`）；**导出 ✅**（`GET /analyze/export` zip/sql/report/csv + UI 导出按钮）；协作骨架 ✅（分享/评论/权限）。**残留**：报告**图内嵌**仍未做；指标卡 / 导出预览 |
| 工具为进程内函数 | 无 **MCP** | **❌ 仍缺** | 无变化 |
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
6. 检索侧：query 改写、多跳拆分、低置信 fail 兜底。
7. ~~badcase 回流；幻觉率监控~~ —— **已于 2026-09-14 完成**（D39 落盘/去重/复跑 + D41 唯一口径 `sources.trace_coverage()`）。**残留**：业务标注集 / 人工评审。
8. MCP 接入层；报告图内嵌 + 图表自检。
9. ~~`dataset_profile` 规模瓶颈~~ —— **已于 2026-09-13 修复**（`profile_column_batch` 合并聚合 +
   `profile_max_columns` 常数上界；121 列 1M 行 15.36s → **5.63s**，且成本与表宽无关）。
   见 `docs/specs/E4/01-profile-quality.md` §7 与 `metrics.md`。

## 一句话结论（回写版）

本项目把「**安全执行、可观测、可评测、工程护栏**」做得很像企业级，且这一轮又补齐了
**自由写码、并行执行、真实库 / 真实向量库 live、浏览器 E2E、首个有效真实基线**——
原始清单里 3 条已完全消除、13 条只剩残项。**真正还差的四块**是：
**真实业务数据验证、合规落地（HITL/审计落库）、可运维部署体系、预算治理**。

> 关联：`docs/审计报告.md`（Round 1–9）、`docs/progress/`（DailyLog / metrics / pending-real / live-validation）、
> `docs/开发计划_企业化.md`（§8.3 未完成、§8.4 C 里程碑）。
