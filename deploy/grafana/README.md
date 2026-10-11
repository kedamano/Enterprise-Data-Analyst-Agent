# Grafana Dashboard

## 导入方式

1. **UI 导入**：Grafana → Create (+) → Import → 上传 `edaa-dashboard.json`
2. **ConfigMap 供给（GitOps 友好）**：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-edaa
  namespace: monitoring
  labels:
    grafana_dashboard: "1"  # grafana-sidecar 自动发现
data:
  edaa-dashboard.json: |
    <!-- 文件内容 -->
```

## Dashboard 结构

| 行 | 面板 | 内容 |
|---|---|---|
| SLO 概览 | 6 个 stat | 可用性 / P95 延迟 / 工具可靠性 / 降级率 / 幻觉率 / 活跃会话 |
| 面板 A | 请求速率 + 延迟分位 | 2xx/4xx/5xx + P50/P95/P99 |
| 面板 B | 工具调用与失败率 | 堆叠柱状 + 失败率趋势 |
| 面板 C | LLM Token 与成本 | prompt/completion tok/s + 估算 $/h + 降级/s |
| 面板 D | 安全 + 缓存 + DB | 401/429/503 + 缓存命中率 + 连接池 |
| 面板 E | 导出与溯源 | 导出耗时/吞吐 + 数值溯源覆盖 |
