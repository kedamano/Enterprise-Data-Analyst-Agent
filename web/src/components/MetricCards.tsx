import { Gauge } from "lucide-react";
import type { AgentEvent, MetricCard } from "@/lib/api";

/**
 * D51：指标卡 —— 把后端**早已算出的**核心指标摊开来给分析师看。
 *
 * 断在哪：`AnalysisResult.metrics` 一直是报告正文里的一张表，用户要读完才看得到关键数字；
 * 而且 SSE 的 FINISH 帧此前**根本不下发 metrics**（前端无从消费）。
 *
 * 归一化在**后端**（`metric_cards.normalize_metrics`）：这里拿到的一定是
 * `{name, value, comparison}`，前端不猜——猜错的那一半永远没人测到。
 */

/** FINISH 帧才带 metrics；取**最后一个非空**者（与 latestIteration 同范式）。 */
export function latestMetrics(events?: AgentEvent[] | null): MetricCard[] | null {
  if (!events) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const m = events[i].metrics;
    if (m && m.length > 0) return m;
  }
  return null;
}

export function MetricCards({ metrics }: { metrics?: MetricCard[] | null }) {
  // 无指标 → **整块不渲染**（渲染一个空壳卡片只会让人以为"指标丢了"）
  if (!metrics || metrics.length === 0) return null;

  return (
    <div className="mb-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3" data-testid="metric-cards">
      {metrics.map((m, i) => (
        <div
          key={`${m.name}-${i}`}
          className="rounded-panel border border-rule bg-white px-3 py-2.5 shadow-sm "
        >
          <div className="flex items-center gap-1 text-micro font-medium text-ink-3">
            <Gauge className="h-4 w-4 shrink-0" />
            <span className="truncate" title={m.name}>
              {m.name}
            </span>
          </div>
          {m.value ? (
            <div className="mt-1 break-words text-heading font-semibold text-ink">{m.value}</div>
          ) : null}
          {m.comparison ? (
            <div className="mt-0.5 text-small text-ink-3">{m.comparison}</div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
