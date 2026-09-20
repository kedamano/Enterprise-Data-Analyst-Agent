# 简历项目经历 — 企业数据分析智能体

> 可按需选 1 条「一句话项目描述」+ 3–4 条核心亮点投不同岗位。编号仅为引用标记，不要照抄序号。

---

## 项目名称

**Enterprise Data Analyst Agent**（企业数据分析智能体）

独立设计并实现的 AI 数据分析助手，把「自然语言 → 取数 → 分析 → 结论 → 交付」全链路工程化。

**技术栈：** Python · FastAPI · LangGraph · React 19 · TypeScript · Tailwind CSS v4 · SQLite/PostgreSQL · Redis · ARQ · Kubernetes · LangFuse

---

## 项目描述（可选一条，按岗位微调）

- **偏后端 / AI 工程：** 基于 LangGraph 构建 6 阶段 Agent 编排引擎，驱动 LLM 调用真实 SQL/Python 工具取数、分析、反思，产出带证据链的业务报告。
- **偏全栈：** 独立交付从 FastAPI 后端到 React 19 SPA 的全栈应用，集成 OAuth2/OIDC 认证、可观测、K8s 蓝绿部署与 CI/CD。
- **偏前端：** 主导前端工程化改造：引入 TanStack Query 接管服务端状态、cmdk 命令面板提升操作效率、tsc 零错误交付。

---

## 核心亮点（按能力维度分类，可挑 3–4 条组合）

### Agent 编排与工程化
- 设计并实现 6 阶段 Agent Pipeline（Clarify → Plan → Execute+Cache → Reflect → Replan → Finalize），每个阶段输出持久化 trace JSONL，支持 Debug Replay CLI（`scripts/replay.py`）100% 复现任意一次分析。
- 落地 Multi-Agent Supervisor：自动拆解复杂查询为并行子任务，失败子任务隔离不影响整体；搭配 Feedback Loop 收集用户评分，驱动 Replan 阶段迭代补数。
- 引入 Stage-Aware LLM Router：按 planner / analyst / reporter 各阶段成本/能力自动路由轻/重模型，实测推理成本显著下降（通过 LangFuse 归因）。

### 安全与治理（零信任设计）
- 自研 PromptGuard：正则匹配已知注入载荷池，支持 passthrough/redirect/block 三档动作；**fail-open 铁律**——任何内部异常原文透传 + 日志告警，绝不阻塞主分析链路。
- 构建 LLM Budget 三级限额（session → user daily → tenant monthly），超标时按策略降级（切便宜模型）或拦截（429），用量数据实时推送到前端用量条。
- 集成 MTLS + OIDC（Authorization Code Flow），自签证书脚本一键生成；OIDC 回调页通过 CustomEvent 写 token → sessionStorage → 主页面 state 完成无感登录。

### 可观测与 DevOps
- 本地 JSONL trace 与 LangFuse 云端**互为备份、互不依赖**：任一通道失败自动回落，Tracers.end() 末端回调保证双写一致性。
- 构建 K8s 蓝绿部署方案（17 个 YAML 清单）：Deployment HPA 弹性伸缩（2–6 副本）、initContainer 等依赖就绪、preStop 优雅下线、NetworkPolicy 默认拒绝 + 细粒度放行；配套 `blue-green-switch.sh` 一键切版本、失败自动 rollout undo。
- ARQ + Redis 升级定时调度：新增 DLQ 重试表（单 job 近 200 条执行轨迹自动截尾）、`next_fire_at` 预计算列支持 ARQ cron 30s tick 捞取待跑 job；未配 REDIS_URL 时自动回退到线程调度（fail-open）。

### 前端工程化
- 引入 TanStack Query 统一接管服务端请求（jobs / knowledge / analytics / budget），告别手动 loading/error state；封装 `useExportZIP` 等 7 个 hooks，告别重复代码。
- 新增 cmdk 命令面板（Ctrl/Cmd-K 唤醒）：整合对话/视图/Job/设置五大命令分组，支持模糊搜索 + 键盘导航；通过 `useOidcCallback` 监听 OIDC callback 事件完成登录态同步。
- 引入骨架屏原语（SkeletonLine / SkeletonCard / SkeletonTable / SkeletonList）统一加载态；tsc --noEmit 零错误交付。

### 测试与质量
- 累计 **1686+ pytest cases**，新增 60 个 D64 安全/治理用例（Budget 18 + PromptGuard 8 + Self-Consistency 6 + Context 7）+ 16 个 ARQ/OIDC/workflow/replay/langfuse 回归用例，全模块零新回归。
- RAG 基准（合成语料 offline）：Recall@1 37.5% · Recall@3 68.1% · Recall@5 75.0%（见 `benchmarks/RESULTS.md`）。

---

## 一句话总结（适合放简历顶部 Summary / 一句话项目描述）

> 独立设计并交付企业数据分析 Agent（FastAPI + LangGraph + React 19）：落地 6 阶段 Agent 编排、LLM Budget 三级限额、PromptGuard 注入防御、ARQ 重试 + DLQ、LangFuse 双写可观测、MTLS/OIDC、K8s 蓝绿部署，累计 1686+ 测试零回归。
