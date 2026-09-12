# DEGRADE/01 降级可见化 — 规格 v1.0

> 来源：D18 演示 dry-run 实测暴露的产品缺陷。
> 现象：真实模式下每个 LLM 调用被 OpenRouter 402 拒绝 → 路由器**静默降级为 Mock**，
> 而对外一切"正常"：`/health` 报配置值 `mock_llm:false`、请求返回 `status:FINISH`，
> 只有报告正文里露出「Mock分析师未做深度统计推断」。
>
> 违反铁律 3「任何静默降级判失败」——该铁律目前只在**测试侧**由 conftest 兜住，**运行态是空的**。

## 1. 目标
让"这次分析究竟是真模型还是兜底模板"成为一个**机器可读、可告警、不可抵赖**的事实。

## 2. 归因：降级事件必须挂在具体某次运行上
- `record_fallback(stage, error)` 增记 `run_id`（取 `tracing.current_run_id()`，无则 `None`）。
- 新增 `tracing.current_run_id()`：返回当前 trace 上下文（`trace_run(run_id=…)`）的 run_id。
- `fallback_events(run_id=None)`：`run_id=None` 返回全部（**保持向后兼容**，现有测试与 `fallback_occurred()` 不变）。
- `error` 字段截断至 **300 字**（402 的 body 很长，不能整段回给前端）。

## 3. 契约

### 3.1 `POST /chat/analyze`（`AnalyzeResponse`）
| 字段 | 类型 | 语义 |
|---|---|---|
| `degraded` | bool | **本轮**（同 run_id）发生 ≥1 次降级 |
| `llm_fallbacks` | `[{stage, error}]` | 本轮降级明细（error 截断） |

- `MOCK_LLM=true`（配置就是 mock）→ `degraded=false`：**主动选择 mock 不是降级**，这两件事必须可区分。
- 降级不影响 `status`（报告仍产出），但调用方/UI 必须能据此打横幅。

### 3.2 SSE `/chat/analyze/stream`
- 每个事件带 `degraded: bool`（当前累计），让 UI 能在降级发生的那一刻就提示，而不是等到最后。
- `FINISH` 事件带完整 `llm_fallbacks` 明细。

### 3.3 `GET /health`
在保留 `status/mock_llm/data_source`（兼容）基础上新增：

| 字段 | 语义 |
|---|---|
| `llm_mode` | `"mock"` / `"real"` —— **配置**意图 |
| `llm_degraded` | **观测**事实：进程内最近一次真实调用是否降级 |
| `llm_fallbacks_total` | 进程内降级累计次数 |
| `llm_last_error` | 最近一次降级原因（截断） |

> 关键：`mock_llm` 是配置值，`llm_degraded` 是**观测值**。D18 的坑正是"只看配置值"。

### 3.4 `GET /health/llm`（主动探测）
- 用当前配置做一次 **1 token** 的真实调用，返回
  `{reachable: bool, model, latency_ms, error?}`。
- **探测自身绝不降级**（否则探测被 mock 骗过就毫无意义）：直接调用、异常即 `reachable=false`，
  且**不写入**降级事件、不影响熔断器状态。
- 有成本（每次 1 token），故独立端点、不并入 `/health` 自动轮询。
- **语义边界（实测补充）**：探测回答的是"端点可达 / 鉴权有效 / 模型存在"，**不是**"当前配置跑得动"。
  D18 实测即为反例：`/health/llm` 返回 `reachable: true`（1 token 在额度内），
  而流水线因 `LLM_MAX_TOKENS=2048` 超出可用额度全部 402。
  因此**"可达"与"真能干活"必须分清**：前者看探测，后者看 `llm_degraded` / `llm_fallbacks_total`。

### 3.5 运行态守卫（把铁律 3 从测试推到运行时）
- 复用既有 `llm_no_fallback`（默认 false）：为 true 时降级直接抛错，**不产出 mock 报告**。
- 部署建议写进 `docs/部署上线.md`：生产 `LLM_NO_FALLBACK=true`，宁可失败也不给假报告。
- 降级日志级别 `warning` → **`error`**（可被告警规则捕获）；熔断打开单独一条 `error`。

## 4. 边界
- 无 trace 上下文时降级（如单元测试直接调 `OpenAILLM.complete`）→ `run_id=None`，仍记入全量事件。
- 同一进程并发多个请求：归因按 run_id 过滤，互不串台。
- `degraded` 为 `true` 时**不改写报告正文**（避免污染导出物）；由 UI/调用方渲染横幅。
  报告内嵌横幅列为后续可选增强。

## 5. TDD
| 用例 | 断言 |
|---|---|
| 响应可见 | 真实模式 + 不可达端点 → `state.metadata["degraded"] is True`、`llm_fallbacks` 含 stage |
| mock 不算降级 | `MOCK_LLM=true` → `degraded is False`（即使没有真实 key） |
| 归因隔离 | 运行 A 降级、运行 B 正常 → B 的 `llm_fallbacks == []` |
| health 观测 | 降级发生后 `/health`：`llm_mode=="real"`、`llm_degraded is True`、`llm_fallbacks_total >= 1` |
| 探测不降级 | `/health/llm` 对不可达端点 → `reachable is False` 且**不新增**降级事件 |
| 探测成功路径 | monkeypatch 客户端成功 → `reachable is True`、`latency_ms >= 0` |
| 严格模式 | `LLM_NO_FALLBACK=true` + 失败 → 抛错（不返回 mock 内容） |
| 截断 | 超长 error → 事件里 `len(error) <= 300` |
| API 契约 | `AnalyzeRequest`/`AnalyzeResponse`/`HealthResponse` 字段可解析 |
