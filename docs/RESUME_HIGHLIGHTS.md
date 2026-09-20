# 项目亮点（精炼版）

> 每条 = 动作 + 做了什么 + 可量化结果。面试被追问时，括号里的关键词就是展开点。

---

## 🥇 优先展示（技术深度 + 业务价值兼备）

1. **设计并落地 6 阶段 Agent 编排引擎**（Clarify→Plan→Execute+Cache→Reflect→Replan→Finalize），每阶段落盘结构化 trace JSONL，配合自研 Replay CLI 可 100% 复现任一次分析现场，排障从"凭感觉"变成"按图索骥"。

2. **构建 LLM Budget 三级限额治理体系**（session → user daily → tenant monthly），超标时按策略自动切到便宜模型（degrade）或 429 拦截（block），用量数据实时推送到前端进度条；配合 PromptGuard 注入防御层（正则 + fail-open 铁律），安全事件零误杀主链路。

3. **主导 K8s 蓝绿交付**：独立完成 17 个清单 + 3 个配套脚本（kind 一键集群 / blue-green 切版本 / pg 定时备份），HPA 2–6 副本弹性伸缩、preStop 优雅下线、NetworkPolicy 默认拒绝；上线即可回滚（`rollout undo`）。

4. **前端工程化升级**：引入 TanStack Query 统一接管服务端状态，封装 7 个 hooks 复盖 jobs / knowledge / analytics / budget；新增 cmdk 命令面板（Ctrl-K）整合 5 大类操作；tsc 零错误交付。

---

## 🥈 补充亮点（按岗位方向选用）

### 偏后端 / AI 工程
- **Multi-Agent Supervisor**：把复杂查询自动拆解为并行子任务，单个子任务失败隔离不影响整体；搭配 Feedback Loop 自动驱动 Replan。
- **ARQ + Redis 升级调度**：新增 DLQ 重试表 + `next_fire_at` 预计算列，未配 Redis 时自动回退线程调度（fail-open），任务持久化不丢。
- **LangFuse 双写可观测**：本地 JSONL 与云端 trace 互为备份、互不依赖；Tracers.end() 末端回调保证双写一致性。

### 偏全栈 / 安全
- **MTLS + OIDC 全栈联调**：自签证书脚本一键生成；Authorization Code Flow 完成接入，OIDC 回调页通过 CustomEvent + sessionStorage 无感同步登录态。
- **Stage-Aware LLM Router**：按 planner / analyst / reporter 阶段自动路由轻/重模型，推理成本下降通过 LangFuse 归因。

### 偏前端
- **骨架屏原语体系**（SkeletonLine/Card/Table/List）统一加载态；预算条 / 命令面板 / OIDC 回调监听 3 个新组件 tsc 零错误上线。

### 测试
- **1686+ pytest cases**，新增 Budget / Guard / Self-Consistency / Context 60 例 + ARQ/OIDC/workflow/replay/langfuse 16 例，全模块零新回归。
- RAG 离线基准：Recall@1 37.5% · Recall@3 68.1% · Recall@5 75.0%。

---

## 面试展开口诀

每条亮点准备 **三个层次的回答**：

| 层次 | 范文（对应亮点 1） |
|---|---|
| **一句话** | "我把 Agent 工作流拆成 6 个阶段、每阶段落盘，再用 Replay CLI 随时复现。" |
| **技术细节** | 每阶段是一个 LangGraph node，state 是 pydantic model，trace 是 append-only JSONL；replay 时 monkeypatch REGISTRY 上的 tool 实现。 |
| **踩过的坑** | 早期 replay 改 REGISTRY 没清干净，下个 test 受到影响——后来改成 try/finally 包一层，异常也恢复。 |

被问"为什么这么设计"时，带一句 **trade-off**："threading scheduler 零依赖适合 MVP，ARQ 选装是为了不强迫部署方装 Redis；fail-open 是为了局部失败不拖垮整个服务。"
