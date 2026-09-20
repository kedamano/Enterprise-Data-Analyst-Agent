// BudgetBar: normal render, high usage, loading, ratio 0
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { BudgetBar } from "./BudgetBar";
import { fetchBudgetUsage, fetchBudgetConfig } from "@/lib/api";

// mock api.ts 模块：所有网络走这里
vi.mock("@/lib/api", () => ({
  fetchBudgetUsage: vi.fn(),
  fetchBudgetConfig: vi.fn(),
}));

function mockApi(usage: Record<string, unknown>, config: Record<string, unknown>) {
  vi.mocked(fetchBudgetUsage).mockResolvedValue(usage as never);
  vi.mocked(fetchBudgetConfig).mockResolvedValue(config as never);
}

describe("BudgetBar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  afterEach(() => {
    cleanup();
  });

  it("正常渲染：day ratio 0.45 / month 0.23 → 显示百分比文本、两条进度条", async () => {
    mockApi(
      {
        user_daily_ratio: 0.45,
        user_daily_used: 45_000,
        tenant_monthly_ratio: 0.23,
        tenant_monthly_used: 230_000,
      },
      {
        enabled: true,
        per_user_daily_tokens: 100_000,
        per_tenant_monthly_tokens: 1_000_000,
      },
    );

    render(<BudgetBar />);

    await waitFor(() => {
      // "45k / 100k" 日用量文本
      expect(screen.getByText("45k / 100k")).toBeInTheDocument();
      // "230k / 1M" 月用量文本
      expect(screen.getByText("230k / 1.0M")).toBeInTheDocument();
    });

    // 两条进度条都能找到（包含 ratioColor 类）
    const bars = document.querySelectorAll(".h-full.rounded-full.transition-all");
    expect(bars.length).toBe(2);
    // day bar 宽度 45%
    expect(bars[0]).toHaveStyle({ width: "45%" });
    // month bar 宽度 23%
    expect(bars[1]).toHaveStyle({ width: "23%" });
  });

  it("高用量：day ratio 0.95 → 存在 bg-danger 类（含'警告'语义）", async () => {
    mockApi(
      {
        user_daily_ratio: 0.95,
        user_daily_used: 95_000,
        tenant_monthly_ratio: 0.10,
        tenant_monthly_used: 100_000,
      },
      {
        enabled: true,
        per_user_daily_tokens: 100_000,
        per_tenant_monthly_tokens: 1_000_000,
      },
    );

    render(<BudgetBar />);

    await waitFor(() => {
      expect(screen.getByText("95k / 100k")).toBeInTheDocument();
    });

    // 日用量进度条使用 bg-danger（高用量警告）
    const bars = document.querySelectorAll(".h-full.rounded-full.transition-all");
    expect(bars[0].className).toContain("bg-danger");
    // 月用量 0.10 → bg-brand
    expect(bars[1].className).toContain("bg-brand");
  });

  it("加载中：loading=true 时显示 SkeletonLine 占位 (aria-hidden)", async () => {
    // 承诺不决议，保持 loading=true
    vi.mocked(fetchBudgetUsage).mockReturnValue(new Promise(() => {}));
    vi.mocked(fetchBudgetConfig).mockReturnValue(new Promise(() => {}));

    render(<BudgetBar />);

    // 两个 SkeletonLine 都带 aria-hidden
    const skeletons = document.querySelectorAll('[aria-hidden="true"]');
    expect(skeletons.length).toBe(2);
  });

  it("ratio 为 0 时：显示 0 / <limit> 且进度条宽度 0%", async () => {
    mockApi(
      {
        user_daily_ratio: 0,
        user_daily_used: 0,
        tenant_monthly_ratio: 0,
        tenant_monthly_used: 0,
      },
      {
        enabled: true,
        per_user_daily_tokens: 100_000,
        per_tenant_monthly_tokens: 1_000_000,
      },
    );

    render(<BudgetBar />);

    await waitFor(() => {
      expect(screen.getByText("0 / 100k")).toBeInTheDocument();
      expect(screen.getByText("0 / 1.0M")).toBeInTheDocument();
    });

    // 两条进度条宽度均为 0%
    const bars = document.querySelectorAll(".h-full.rounded-full.transition-all");
    expect(bars[0]).toHaveStyle({ width: "0%" });
    expect(bars[1]).toHaveStyle({ width: "0%" });
  });
});
