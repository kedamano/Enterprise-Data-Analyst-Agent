// AnalyticsView: mock fetch, render, assert KPI cards show correct numbers
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { AnalyticsView } from "./AnalyticsView";

const mockStats = {
  range: "24h",
  totals: {
    runs: 42,
    success: 38,
    failed: 4,
    success_rate: 0.905,
    avg_duration_ms: 12500,
    p95_duration_ms: 32000,
    total_prompt_tokens: 152000,
    total_completion_tokens: 48000,
    total_cost_usd: 0.0234,
    avg_tool_calls_per_run: 3.2,
    avg_runs_per_hour: 1.75,
  },
  by_stage: [
    { stage: "planner", avg_ms: 2300, count: 42 },
    { stage: "executor", avg_ms: 8000, count: 40 },
    { stage: "analyst", avg_ms: 3200, count: 38 },
  ],
  by_tool: [
    { tool: "sql_query", count: 80, avg_ms: 1200, success_rate: 0.95 },
    { tool: "python_analysis", count: 30, avg_ms: 3500, success_rate: 0.9 },
  ],
  daily: [
    { hour: "2026-09-18T10", runs: 3, tokens: 5000 },
    { hour: "2026-09-18T11", runs: 5, tokens: 8000 },
  ],
  recent_runs: [
    { session_id: "abc12345xyz", ts: 1716000000, duration_ms: 12000, status: "OK", tokens: 800 },
    { session_id: "def67890uvw", ts: 1716001000, duration_ms: 5000, status: "ERROR", tokens: 300 },
  ],
  process_metrics: {
    counters: { http_requests_total: 100 },
    gauges: {},
    histograms: {},
  },
};

describe("AnalyticsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  afterEach(() => {
    cleanup();
  });

  it("renders KPI cards with correct numbers", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(mockStats), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    render(<AnalyticsView />);

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByText("42")).toBeInTheDocument();
    });

    // Total runs
    expect(screen.getByText("42")).toBeInTheDocument();
    // Success rate
    expect(screen.getByText("90.5%")).toBeInTheDocument();
    // The loading skeletons should be gone
    expect(screen.queryByLabelText("使用文档")).toBeNull();

    fetchSpy.mockRestore();
  });

  it("renders 0 runs when empty", async () => {
    const emptyStats = { ...mockStats, totals: { ...mockStats.totals, runs: 0, success: 0, failed: 0 } };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(emptyStats), { status: 200, headers: { "Content-Type": "application/json" } }),
    );

    render(<AnalyticsView />);

    await waitFor(() => {
      expect(screen.getByText("0")).toBeInTheDocument();
    });

    fetchSpy.mockRestore();
  });
});
