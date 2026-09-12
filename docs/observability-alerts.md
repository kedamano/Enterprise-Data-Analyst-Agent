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

## 备注

- 直方图仅保留最近 `2000` 个样本用于分位估算（内存有界）；长期趋势依赖 Prometheus TSDB。
- OTel 导出：`app/infrastructure/observability/metrics.export_otel()` 在 `opentelemetry` SDK
  可用时推送；缺失时为静默 no-op，不影响服务启动。
- 当前为单进程指标；多副本部署需配 `--web.enable-remote-write` 或 OTel collector 聚合。
