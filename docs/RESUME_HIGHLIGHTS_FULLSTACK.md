# 项目亮点 · 偏全栈版

> 如果你投递的是「全栈工程师 / 应用开发」，下面这版直接能贴简历。4 条核心 + 2 条补充，每条 = 前后端做了什么 + 结果。

---

## 核心亮点（4 条，建议全放）

1. **独立交付从 FastAPI 后端到 React 19 SPA 的全栈应用，落地 6 阶段 Agent 编排引擎**
   后端基于 LangGraph 驱动 LLM 调用 SQL/Python 工具取数、分析、反思；前端用 TypeScript + Tailwind v4 渲染对话流 / 报告 / 计划时间轴 / 数据源管理等 8 个视图。每个分析阶段落盘结构化 trace JSONL，配合自研 Replay CLI 100% 复现任一次分析现场。

2. **主导认证与治理全栈改造：MTLS + OIDC + PromptGuard + LLM Budget**
   ‑ 后端：自签证书（trustme）+ uvicorn HTTPS 入口 + OIDC Authorization Code Flow 换 token / JWKS 验签 + fail-open PromptGuard（正则匹配注入载荷池，异常原文透传永不挂主链路）；
   ‑ 前端：AuthCentre 增加「使用 OIDC 登录」按钮，回调页 CustomEvent → sessionStorage → 主页面 state 完成无感登录；BudgetBar 实时显示 session/user/tenant 三级用量进度条；
   ‑ 治理：LLM Budget 三级限额超标自动切便宜模型（degrade）或 429 拦截（block）。

3. **主导 K8s 蓝绿交付 + 异步任务持久化**
   ‑ 17 个清单覆盖 Deployment（HPA 2–6 副本）/ Ingress（Let's Encrypt + 限流）/ NetworkPolicy（默认拒绝 + 细粒度放行）/ ServiceMonitor；配套 `kind-bootstrap.sh` 一键起本地集群、`blue-green-switch.sh` 一键切版本失败自动 rollout undo；
   ‑ 升级调度：ARQ + Redis 替换线程调度，新增 DLQ 重试表 + `next_fire_at` 预计算列；未配 Redis 时自动回退线程调度（fail-open），任务持久化不丢。

4. **前端工程化升级：状态管理 + 命令面板 + 可信交付**
   ‑ 引入 TanStack Query 统一接管 jobs / knowledge / analytics / budget 服务端状态，封装 7 个 hooks 告别手动 loading/error 模板代码；
   ‑ 新增 cmdk 命令面板（Ctrl-K 唤醒），整合对话 / 视图 / Job / 设置 5 大类操作，支持模糊搜索 + 键盘导航；
   ‑ 引入骨架屏原语体系（SkeletonLine/Card/Table/List）统一加载态；tsc --noEmit 零错误上线。

---

## 补充亮点（选 1–2 条，按 JD 微调）

- **多模态交互**：Multi-Agent Supervisor 把复杂查询自动拆解为并行子任务，单个子任务失败隔离不影响整体；Feedback Loop 收集用户评分，自动驱动 Replan 阶段迭代补数。
- **可观测双写**：本地 JSONL trace 与 LangFuse 云端 trace 互不依赖、互为备份，任一通道失败自动回落；Tracers.end() 末端回调保证双写一致性。
- **质量底线**：累计 1686+ pytest cases，新增 60 例安全/治理用例 + 16 例 ARQ/OIDC/workflow/replay/langfuse 回归用例，全模块零新回归。

---

## 面试展开模板

| 亮点 | 一句话 | 技术细节 | 踩坑 / trade-off |
|---|---|---|---|
| 全栈 Agent | 独立交付 FastAPI + React 全栈，6 阶段 Pipeline 驱动 LLM 取数分析 | LangGraph node = 1 阶段；state = pydantic；trace = JSONL append-only | 早期 replay 改 REGISTRY 忘复原，下个 test 受影响 → 改 try/finally |
| 认证治理 | MTLS + OIDC + PromptGuard + Budget 全栈 | AuthCode Flow + CustomEvent 登录态同步；Guard fail-open 铁律；Budget 三级 → degrade/block | Guard 过于激进会误伤专业术语 → 用 regex 白名单兜底 |
| K8s 蓝绿 | 17 清单 + 3 脚本完成蓝绿 + 自动回滚 | HPA 2-6、initContainer 等依赖、preStop 优雅下线、NetworkPolicy 默认拒绝 | kind 网络偶发 DNS 失败 → 改为 `kind wait` + 重试 |
| 前端工程化 | TanStack Query + cmdk 统一状态与操作入口 | 7 个 hooks 复盖全部 GET/POST；cmdk 键盘导航 | 早期 Query 缓存过早失效 → 调 staleTime 5s |
