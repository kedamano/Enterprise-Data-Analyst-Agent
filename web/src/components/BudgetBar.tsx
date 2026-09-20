import { useEffect, useState } from "react";
import { SkeletonLine } from "@/components/ui/skeleton";
import { fetchBudgetConfig, fetchBudgetUsage } from "@/lib/api";
import type { BudgetConfig, BudgetUsage } from "@/lib/api";

/** 0..1 转颜色类（<70% 蓝 / 70-90% 黄 / >90% 红） */
function ratioColor(ratio: number): string {
  if (ratio >= 0.9) return "bg-danger";
  if (ratio >= 0.7) return "bg-attention";
  return "bg-brand";
}

function ratioTextColor(ratio: number): string {
  if (ratio >= 0.9) return "text-danger";
  if (ratio >= 0.7) return "text-attention";
  return "text-brand";
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

export function BudgetBar() {
  const [usage, setUsage] = useState<BudgetUsage | null>(null);
  const [config, setConfig] = useState<BudgetConfig | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ac = new AbortController();
    Promise.all([
      fetchBudgetUsage(ac.signal).catch(() => null),
      fetchBudgetConfig(ac.signal).catch(() => null),
    ])
      .then(([u, c]) => {
        if (u) setUsage(u);
        if (c) setConfig(c);
      })
      .finally(() => setLoading(false));
    return () => ac.abort();
  }, []);

  // mock 模式（config.enabled=false 或 usage 有特定标记）或 loading 分支
  if (loading) {
    return (
      <div className="space-y-2 border-b border-rule bg-white px-4 py-2.5">
        <SkeletonLine className="h-3 w-3/5" />
        <SkeletonLine className="h-3 w-2/5" />
      </div>
    );
  }

  // 预算未启用 → 不渲染进度条
  if (!config?.enabled) {
    return null;
  }

  const dailyRatio = usage?.user_daily_ratio ?? 0;
  const monthlyRatio = usage?.tenant_monthly_ratio ?? 0;
  const dailyExceeded = dailyRatio >= 1.0;
  const monthlyExceeded = monthlyRatio >= 1.0;

  return (
    <div className="border-b border-rule bg-white px-4 py-2">
      <div className="flex items-center justify-between">
        <span className="text-micro font-medium text-ink-2">用量</span>
        {(dailyExceeded || monthlyExceeded) && (
          <span className="rounded-full border border-danger bg-danger-soft px-1.5 py-0.5 text-[10px] font-semibold text-danger">
            已超限
          </span>
        )}
      </div>
      {/* 日用量条 */}
      <div className="mt-1.5">
        <div className="mb-0.5 flex items-center justify-between text-[11px] text-ink-3">
          <span>日</span>
          <span className={ratioTextColor(dailyRatio)}>
            {formatTokens(usage?.user_daily_used ?? 0)} / {formatTokens(config.per_user_daily_tokens)}
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-canvas">
          <div
            className={`h-full rounded-full transition-all ${ratioColor(dailyRatio)}`}
            style={{ width: `${Math.min(100, dailyRatio * 100)}%` }}
          />
        </div>
      </div>
      {/* 月用量条 */}
      <div className="mt-1.5">
        <div className="mb-0.5 flex items-center justify-between text-[11px] text-ink-3">
          <span>月</span>
          <span className={ratioTextColor(monthlyRatio)}>
            {formatTokens(usage?.tenant_monthly_used ?? 0)} / {formatTokens(config.per_tenant_monthly_tokens)}
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-canvas">
          <div
            className={`h-full rounded-full transition-all ${ratioColor(monthlyRatio)}`}
            style={{ width: `${Math.min(100, monthlyRatio * 100)}%` }}
          />
        </div>
      </div>
    </div>
  );
}
