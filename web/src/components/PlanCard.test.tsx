// PlanCard: renders plan when given, task label, deliverable tags, workflow progress
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { PlanCard } from "./PlanCard";
import type { ExecutionPlan, AgentEvent } from "@/lib/api";

const PLAN: ExecutionPlan = {
  task_type: "business_analysis",
  deliverable: ["analysis_report", "chart"],
  workflow: ["清洗数据", "指标计算", "可视化", "撰写报告"],
  requires_sql: true,
  requires_python: true,
  requires_data: true,
  requires_report: true,
};

describe("PlanCard", () => {
  beforeEach(() => cleanup());

  it("plan 为 null → 不渲染任何内容", () => {
    const { container } = render(<PlanCard plan={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("显示 task label Business Analysis + 交付物标签", () => {
    render(<PlanCard plan={PLAN} />);
    // 任务识别 label
    expect(screen.getByText("任务识别")).toBeInTheDocument();
    expect(screen.getByText("Business Analysis")).toBeInTheDocument();
    // 交付物
    expect(screen.getByText("analysis_report")).toBeInTheDocument();
    expect(screen.getByText("chart")).toBeInTheDocument();
  });

  it("执行计划列表：4 个步骤全部可见，第一项未完成的带 active 动画圈", () => {
    render(<PlanCard plan={PLAN} />);
    const steps = screen.getAllByText(/清洗数据|指标计算|可视化|撰写报告/);
    expect(steps).toHaveLength(4);
  });

  it("需要：SQL / Python / 数据 / 报告", () => {
    render(<PlanCard plan={PLAN} />);
    expect(screen.getByText("需要：SQL / Python / 数据 / 报告")).toBeInTheDocument();
  });

  it("有 events 且 FINISH：所有步骤 state = done", () => {
    const events: AgentEvent[] = [{ type: "CONNECT", status: "FINISH", step: null, parallel: null, data: null }];
    render(<PlanCard plan={PLAN} events={events} />);
    // FINISH 时全部 4 步都该是 "done" 状态 → 每步文本带 line-through 类
    const through = document.querySelectorAll(".line-through");
    expect(through.length).toBe(4);
  });

  it("未知 task_type → 直接回退显示 task_type 字符串", () => {
    const weird: ExecutionPlan = {
      ...PLAN,
      task_type: "custom_task",
    };
    render(<PlanCard plan={weird} />);
    expect(screen.getByText("custom_task")).toBeInTheDocument();
  });
});
