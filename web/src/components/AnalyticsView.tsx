import { useEffect, useState } from "react";
import {
  BarChart3,
  CheckCircle2,
  Clock,
  Loader2,
  MessageSquare,
  ThumbsDown,
  ThumbsUp,
  TrendingUp,
  Zap,
  XCircle,
} from "@/components/icons";
import {
  fetchAnalyticsStats,
  fetchFeedbackStats,
  type AnalyticsStats,
  type FeedbackStats,
} from "@/lib/api";
import { SkeletonCard } from "@/components/ui/skeleton";

type TimeRange = "24h" | "7d" | "30d";
type TabKey = "overview" | "feedback";

const TAB_LABELS: Record<TabKey, string> = {
  overview: "运行统计",
  feedback: "反馈质量",
};

const RANGE_LABELS: Record<TimeRange, string> = {
  "24h": "24 小时",
  "7d": "7 天",
  "30d": "30 天",
};

const STAGE_LABELS: Record<string, string> = {
  planner: "意图 & 规划",
  executor: "执行取数",
  analyst: "证据分析",
  reflection: "质检反思",
  reporter: "生成报告",
};

/** KPI 统计卡片 */
function KpiCard({
  label,
  value,
  sub,
  accent = "brand",
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: "brand" | "emerald" | "amber" | "rose";
}) {
  const ring: Record<string, string> = {
    brand: "text-brand",
    emerald: "text-verified",
    amber: "text-attention",
    rose: "text-danger",
  };
  return (
    <div className="rounded-panel border border-rule bg-white p-4 shadow-sm">
      <div className="text-micro font-medium text-ink-3">{label}</div>
      <div className={`mt-1 text-title font-semibold ${ring[accent]}`}>
        {value}
      </div>
      {sub ? <div className="mt-0.5 text-micro text-ink-3">{sub}</div> : null}
    </div>
  );
}

/** 纯 CSS 横向 bar 图 */
function BarChart({
  data,
  valueKey,
  labelKey,
  max,
  color = "bg-brand",
}: {
  data: Record<string, unknown>[];
  valueKey: string;
  labelKey: string;
  max: number;
  color?: string;
}) {
  if (!data || data.length === 0) {
    return <div className="py-4 text-center text-micro text-ink-3">暂无数据</div>;
  }
  return (
    <div className="space-y-2">
      {data.map((d, i) => {
        const val = Number(d[valueKey] || 0);
        const label = String(d[labelKey] ?? "");
        const pct = max > 0 ? Math.min(100, (val / max) * 100) : 0;
        return (
          <div key={`${label}-${i}`} className="flex items-center gap-3">
            <span className="w-28 shrink-0 truncate text-small text-ink-2" title={label}>
              {label}
            </span>
            <div className="relative h-5 flex-1 overflow-hidden rounded-control bg-canvas">
              <div
                className={`h-full rounded-control ${color}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="w-16 shrink-0 text-right text-micro font-medium text-ink">
              {val.toLocaleString()}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function AnalyticsView() {
  const [range, setRange] = useState<TimeRange>("24h");
  const [tab, setTab] = useState<TabKey>("overview");
  const [data, setData] = useState<AnalyticsStats | null>(null);
  const [fbStats, setFbStats] = useState<FeedbackStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const tasks: Promise<unknown>[] = [
      fetchAnalyticsStats(range, null, ctrl.signal)
        .then((d) => setData(d))
        .catch((e: unknown) => {
          if ((e as DOMException)?.name === "AbortError") return;
          const status = (e as { status?: number })?.status;
          if (status === 403) {
            setError("需要管理员权限");
          } else {
            setError(e instanceof Error ? e.message : "加载失败");
          }
          setData(null);
        }),
    ];
    if (tab === "feedback") {
      tasks.push(
        fetchFeedbackStats(ctrl.signal)
          .then((d) => setFbStats(d))
          .catch(() => setFbStats(null)),
      );
    }
    Promise.all(tasks).finally(() => setLoading(false));
    return () => ctrl.abort();
  }, [range, tab]);

  if (loading && !data) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mb-6 flex items-center gap-3">
          <BarChart3 className="h-6 w-6 text-brand" />
          <h1 className="text-title font-semibold text-ink">分析控制台</h1>
        </div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <SkeletonCard key={i} />
          ))}
        </div>
        <div className="mt-6 space-y-4">
          <SkeletonCard lines={3} />
          <SkeletonCard lines={3} />
        </div>
      </div>
    );
  }

  if (error === "需要管理员权限") {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mb-6 flex items-center gap-3">
          <BarChart3 className="h-6 w-6 text-brand" />
          <h1 className="text-title font-semibold text-ink">分析控制台</h1>
        </div>
        <div className="rounded-panel border border-attention bg-attention-soft p-4 text-small text-attention">
          需要管理员权限：当前账号没有 ``admin`` 角色，无法访问分析统计数据。请联系管理员授权。
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mb-6 flex items-center gap-3">
          <BarChart3 className="h-6 w-6 text-brand" />
          <h1 className="text-title font-semibold text-ink">分析控制台</h1>
        </div>
        <div className="rounded-panel border border-danger bg-danger-soft p-4 text-small text-danger">
          加载失败：{error}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const t = data.totals;
  const fmtRate = (r: number) => `${(r * 100).toFixed(1)}%`;
  const stageMax = data.by_stage.reduce((m, s) => Math.max(m, s.avg_ms), 0);
  const toolMax = data.by_tool.reduce((m, s) => Math.max(m, s.count), 0);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-6">
      {/* Header + tabs + range switcher */}
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <BarChart3 className="h-6 w-6 text-brand" />
          <h1 className="text-title font-semibold text-ink">分析控制台</h1>
          {/* Tab bar */}
          <div className="ml-4 flex items-center gap-1 rounded-control border border-rule bg-canvas p-0.5">
            {(Object.keys(TAB_LABELS) as TabKey[]).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setTab(k)}
                className={`rounded-control px-3 py-1 text-small font-medium transition ${
                  k === tab
                    ? "bg-white text-brand shadow-sm"
                    : "text-ink-3 hover:text-ink"
                }`}
              >
                {TAB_LABELS[k]}
              </button>
            ))}
          </div>
        </div>
        {/* Range switcher */}
        <div className="flex items-center gap-1 rounded-control border border-rule bg-white p-0.5">
          {(["24h", "7d", "30d"] as TimeRange[]).map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              className={`rounded-control px-3 py-1 text-small font-medium transition ${
                r === range
                  ? "bg-brand text-white"
                  : "text-ink-2 hover:text-ink"
              }`}
            >
              {RANGE_LABELS[r]}
            </button>
          ))}
        </div>
      </div>

      {/* ---------------- Feedback quality tab ---------------- */}
      {tab === "feedback" ? (
        <FeedbackStatsPanel stats={fbStats} />
      ) : (
        <>
          {/* KPI row */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <KpiCard label="总分析数" value={t.runs.toLocaleString()} accent="brand" />
            <KpiCard label="成功率" value={fmtRate(t.success_rate)} accent="emerald" />
            <KpiCard
              label="平均耗时"
              value={t.avg_duration_ms > 1000 ? `${(t.avg_duration_ms / 1000).toFixed(1)}s` : `${t.avg_duration_ms}ms`}
              sub={`P95: ${t.p95_duration_ms.toLocaleString()}ms`}
              accent="amber"
            />
            <KpiCard
              label="Token 总消耗"
              value={(t.total_prompt_tokens + t.total_completion_tokens).toLocaleString()}
              sub={`Prompt ${t.total_prompt_tokens.toLocaleString()} / Completion ${t.total_completion_tokens.toLocaleString()}`}
              accent="brand"
            />
          </div>

          {/* Charts row */}
          <div className="mt-6 grid gap-4 lg:grid-cols-2">
            <div className="rounded-panel border border-rule bg-white p-4 shadow-sm">
              <div className="mb-3 flex items-center gap-2">
                <Zap className="h-4 w-4 text-brand" />
                <h2 className="text-body font-semibold text-ink">阶段耗时排行</h2>
              </div>
              <BarChart
                data={data.by_stage.map((s) => ({ ...s, stage_label: STAGE_LABELS[s.stage] || s.stage }))}
                valueKey="avg_ms"
                labelKey="stage_label"
                max={stageMax}
                color="bg-brand"
              />
            </div>

            <div className="rounded-panel border border-rule bg-white p-4 shadow-sm">
              <div className="mb-3 flex items-center gap-2">
                <BarChart3 className="h-4 w-4 text-verified" />
                <h2 className="text-body font-semibold text-ink">工具调用分布</h2>
              </div>
              <BarChart
                data={data.by_tool}
                valueKey="count"
                labelKey="tool"
                max={toolMax}
                color="bg-verified"
              />
            </div>
          </div>

          {/* Recent runs */}
          <div className="mt-6 rounded-panel border border-rule bg-white shadow-sm">
            <div className="flex items-center gap-2 border-b border-rule px-4 py-3">
              <Clock className="h-4 w-4 text-ink-3" />
              <h2 className="text-body font-semibold text-ink">最近运行</h2>
            </div>
            {data.recent_runs.length === 0 ? (
              <div className="px-4 py-8 text-center text-micro text-ink-3">
                该时间段内没有运行记录
              </div>
            ) : (
              <div className="divide-y divide-rule">
                {data.recent_runs.map((r, i) => (
                  <div key={`${r.session_id}-${i}`} className="flex items-center gap-3 px-4 py-2.5">
                    {r.status === "OK" ? (
                      <CheckCircle2 className="h-4 w-4 shrink-0 text-verified" />
                    ) : (
                      <XCircle className="h-4 w-4 shrink-0 text-danger" />
                    )}
                    <span className="w-20 shrink-0 font-mono text-micro text-ink">
                      {r.session_id.slice(0, 8)}
                    </span>
                    <span className="text-small text-ink-2">
                      {r.duration_ms > 1000
                        ? `${(r.duration_ms / 1000).toFixed(1)}s`
                        : `${r.duration_ms}ms`}
                    </span>
                    <span className="text-micro text-ink-3">
                      {r.tokens.toLocaleString()} tokens
                    </span>
                    <span
                      className={`ml-auto rounded-full px-2 py-0.5 text-micro font-medium ${
                        r.status === "OK"
                          ? "bg-verified-soft text-verified"
                          : "bg-danger-soft text-danger"
                      }`}
                    >
                      {r.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/** Feedback quality sub-panel with KPIs and low-quality comments list */
function FeedbackStatsPanel({ stats }: { stats: FeedbackStats | null }) {
  if (!stats) {
    return (
      <div className="rounded-panel border border-rule bg-white p-8 text-center text-micro text-ink-3">
        暂无反馈数据
      </div>
    );
  }

  const s = stats;
  const fmtPct = (r: number) => `${(r * 100).toFixed(1)}%`;
  const hasComments = s.comments_with_text.length > 0;
  const hasLow = s.recent_10.filter((r) => r.rating === -1).length > 0;

  return (
    <>
      {/* 5 KPI cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <KpiCard label="总反馈数" value={s.total} accent="brand" />
        <KpiCard label="好评率" value={fmtPct(s.satisfaction_rate)} accent="emerald" />
        <KpiCard
          label="👍 好评"
          value={s.thumbs_up.toLocaleString()}
          accent="emerald"
        />
        <KpiCard
          label="👎 差评"
          value={s.thumbs_down.toLocaleString()}
          accent="rose"
        />
        <KpiCard
          label="有文字反馈"
          value={s.comments_with_text.length}
          accent="amber"
        />
      </div>

      {/* Low-quality sessions list */}
      {hasLow ? (
        <div className="mt-6 rounded-panel border border-rule bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b border-rule px-4 py-3">
            <ThumbsDown className="h-4 w-4 text-danger" />
            <h2 className="text-body font-semibold text-ink">差评反馈 (recent)</h2>
          </div>
          <div className="divide-y divide-rule">
            {s.recent_10
              .filter((r) => r.rating === -1)
              .map((r) => (
                <div key={`${r.session_id}-${r.created_at}`} className="px-4 py-2.5">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-micro text-ink">
                      {r.session_id.slice(0, 8)}
                    </span>
                    <span className="text-micro text-ink-3">{r.created_at}</span>
                  </div>
                  {r.comment ? (
                    <p className="mt-1 text-small text-ink-2 leading-relaxed">
                      {r.comment.slice(0, 100)}
                      {r.comment.length > 100 ? "…" : ""}
                    </p>
                  ) : null}
                </div>
              ))}
          </div>
        </div>
      ) : null}

      {/* Comments with text */}
      {hasComments ? (
        <div className="mt-6 rounded-panel border border-rule bg-white shadow-sm">
          <div className="flex items-center gap-2 border-b border-rule px-4 py-3">
            <MessageSquare className="h-4 w-4 text-brand" />
            <h2 className="text-body font-semibold text-ink">文字反馈</h2>
          </div>
          <div className="divide-y divide-rule">
            {s.comments_with_text.slice(0, 15).map((r) => (
              <div key={`${r.session_id}-${r.created_at}-cmt`} className="px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-micro text-ink">
                    {r.session_id.slice(0, 8)}
                  </span>
                  {r.rating === 1 ? (
                    <ThumbsUp className="h-3 w-3 text-verified" />
                  ) : (
                    <ThumbsDown className="h-3 w-3 text-danger" />
                  )}
                  <span className="text-micro text-ink-3">{r.created_at}</span>
                </div>
                {r.comment ? (
                  <p className="mt-1 text-small text-ink-2 leading-relaxed">
                    {r.comment.slice(0, 100)}
                    {r.comment.length > 100 ? "…" : ""}
                  </p>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </>
  );
}
