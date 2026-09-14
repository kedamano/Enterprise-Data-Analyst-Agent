# 可观测性 · 告警规则（#3）

本文件定义基于 `/metrics` 端点（Prometheus 文本格式）的告警阈值。由外部
Prometheus + Alertmanager / Grafana 抓取并触发；服务本体只负责产出指标，不内置告警引擎
（与 docs/对标企业级Gap.md §六「无告警引擎」的缺口对应——此处补齐规则定义）。

> 端点：`GET /metrics`
> 指标口径见 `app/infrastructure/observability/metrics.py`。

---

## 指标清单

| 指标 | 类型 | 含义 |
|---|---|---|
| `http_requests_total` | counter | HTTP 请求总数 |
| `http_request_duration_seconds` | histogram | 单请求时延（P50/P95/P99 由 `_quantile` 系列导出） |
| `tool_calls_total` | counter | 工具调用总数 |
| `tool_call_duration_seconds` | histogram | 单次工具耗时 |
| `tool_calls_failed_total` | counter | 工具失败次数 |
| `llm_calls_total` | counter | LLM 调用次数 |
| `llm_prompt_tokens_total` / `llm_completion_tokens_total` | counter | token 消耗 |
| `llm_cost_usd_total` | counter | 估算成本（USD，需配置 `cost_*_per_mtok`） |
| `llm_fallbacks_total` | counter | **LLM 降级次数**（D44 新增：此前只有进程内 `fallback_events()`，多副本不聚合） |
| `trace_numeric_claims_total` / `trace_untraced_claims_total` / `trace_hallucination_ratio` | counter×2 / gauge | 数值溯源覆盖与疑似幻觉率（D41） |
| `event_http_401_total` / `event_http_429_total` / `event_http_503_total` | counter | 鉴权拒绝 / 限流 / 配置错误 |

---

## 告警规则（PromQL 草案）

```yaml
groups:
  - name: da-agent
    rules:
      # 1) P95 时延劣化：近 5 分钟 P95 > 8s
      - alert: HighRequestLatencyP95
        expr: histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 8
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "P95 时延超 8s（近 5m）"

      # 2) 工具失败率过高：近 5 分钟失败率 > 10%
      - alert: HighToolFailureRate
        expr: rate(tool_calls_failed_total[5m]) / rate(tool_calls_total[5m]) > 0.10
        for: 5m
        labels: { severity: critical }
        annotations:
          summary: "工具失败率 > 10%"

      # 3) 鉴权配置故障：出现 503（AUTH_ENABLED=true 但未配 key）
      - alert: AuthMisconfigured
        expr: increase(event_http_503_total[5m]) > 0
        for: 1m
        labels: { severity: critical }
        annotations:
          summary: "鉴权 503：生产开了鉴权却没配 AUTH_KEYS"

      # 4) 限流命中：429 突增（可能遭遇滥用或配额过小）
      - alert: RateLimitedSpikes
        expr: increase(event_http_429_total[5m]) > 20
        for: 2m
        labels: { severity: warning }
        annotations:
          summary: "限流 429 突增"

      # 5) LLM 成本失控：近 1h 成本速率 > 阈值（需配置 cost_*_per_mtok）
      - alert: LLMCostRunaway
        expr: rate(llm_cost_usd_total[1h]) > 5
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: "LLM 估算成本近 1h 速率 > $5/h"
```

---

## Dashboard 建议（Grafana）

- 面板 A：请求量 + P50/P95/P99 时延趋势。
- 面板 B：工具调用量 / 失败率（红绿色块）。
- 面板 C：LLM token 与成本（按 stage 维度若后续加 label）。
- 面板 D：401/429/503 事件计数（安全与容量告警前兆）。

---

## D41 · 疑似幻觉率告警（数值无源）

**指标**（`/metrics`，与 `app/eval` **同一口径** `sources.trace_coverage`）：

| 指标 | 类型 | 含义 |
|---|---|---|
| `trace_numeric_claims_total` | counter | 报告里的**数值结论**总数 |
| `trace_untraced_claims_total` | counter | 其中**拿不到对应 SQL 步骤**的（疑似编造） |
| `trace_hallucination_ratio` | gauge | 最近一次运行的 `untraced / numeric` |

**零数值结论时不记**（`numeric_claims=0` → 不 inc）：没有数值就无所谓"幻觉率"，
记 0 会造成"系统很干净"的错觉。这与 eval 侧 `hallucination_rate=None` 的表达一致。

**建议规则**：

```yaml
- alert: 疑似幻觉率偏高
  expr: trace_hallucination_ratio > 0.2
  for: 10m
  labels: {severity: page}
  annotations:
    summary: "近 10 分钟报告数值有 >20% 无法溯源到 SQL 步骤"
    runbook: "查 /api/v1/chat/analyze/trace/{session} 看 claims 明细；对照 analyst 输出"
```

> **为什么阈值给 0.2 而不是 0**：`sql_id` 由 analyst 后处理自动补全，偶发漏补不代表
> 结论编造（数字可能来自用户问题或知识库口径）。**持续 >20% 才值得人看**——
> 与项目其它门禁一致：宁可漏报，不要制造报警疲劳。
>
> **这条告警的价值**：它监控的是"**报告说了数字、但那数字没有出处**"——
> 正是 E1 溯源体系要防的核心风险，此前只在离线 eval 里算，线上是**盲区**。

---

## 备注

- 直方图仅保留最近 `2000` 个样本用于分位估算（内存有界）；长期趋势依赖 Prometheus TSDB。
- OTel 导出：`app/infrastructure/observability/metrics.export_otel()` 在 `opentelemetry` SDK
  可用时推送；缺失时为静默 no-op，不影响服务启动。
- 当前为单进程指标；多副本部署需配 `--web.enable-remote-write` 或 OTel collector 聚合。

---

## D44 · SLI / SLO 定义

> 此前只有零散指标与告警规则，**没有明确"什么算好"**。本节把 SLI（量什么）、
> SLO（目标多少）、采集口径（读哪个指标）写死，避免"看着还行"这种无法验收的判断。

| # | SLI | 口径（指标） | SLO（建议） | 不达标意味着 |
|---|---|---|---|---|
| 1 | **可用性** | `http_requests_total` 中非 5xx 占比 | ≥ 99%（月） | 服务本身有问题 |
| 2 | **请求延迟** | `http_request_duration_seconds` P95 | ≤ 5s（mock/缓存命中）；真模型另算 | 排队或上游慢 |
| 3 | **工具可靠性** | `1 - tool_calls_failed_total / tool_calls_total` | ≥ 95% | 取数链路不稳（SQL 错/权限/超时） |
| 4 | **降级率** | `llm_fallbacks_total / llm_calls_total` | ≤ 1% | **真实模型没在干活**（限流/余额/上游故障） |
| 5 | **数值可信度** | `trace_hallucination_ratio` | ≤ 0.05 | 报告数字无出处（见 D41 告警） |
| 6 | **会话成本** | `llm_prompt_tokens_total + llm_completion_tokens_total`（按测试窗口） | 单次 ≤ 200 万（= D43 熔断线） | 打滑或重规划过多 |

**三条口径纪律**（否则 SLI 会骗人）：

1. **降级样本必须剔除**：`llm_fallbacks_total > 0` 的窗口里，延迟/成本类 SLI **不可信**
   （回落到 Mock 当然快、当然便宜）——与 eval 的 `DEGRADED` 剔除同一条道理。
2. **零样本 ≠ 达标**：某窗口没有数值 claim 时，SLI #5 是**未定义**而不是 0
   （同 D41：把"没测到"报成"0 幻觉"是最典型的自欺）。
3. **单副本口径**：当前指标是**进程内**的。多副本部署需 OTel collector / Prometheus
   联邦聚合后再算 SLI，否则每个副本各报各的。

### 待补（如实记录）

- **日志采样**已可用（`LOG_SAMPLE_RATIO`，只采正常、失败必留），但**采样后的日志
  尚未接入任何聚合**（如按 status 计数入指标）——即采样会降低可观测性的**分辨率**，
  需要"日志 → 指标"的桥才完整。本日不做。
