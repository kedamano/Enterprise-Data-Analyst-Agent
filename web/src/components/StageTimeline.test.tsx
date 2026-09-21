// StageTimeline: collapse/expand toggle, timeline content mapping, tone computation
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { StageTimeline } from "./StageTimeline";
import type { AgentEvent } from "@/lib/api";

// 已完成：含 FINISH → finished=true, running=false
const E_FINISH: AgentEvent[] = [
  { type: "CONNECT", status: "SUCCESS", step: null, parallel: null, data: null },
  { type: "UNDERSTAND", status: "SUCCESS", step: null, parallel: null, data: { objective: "营收分析" } },
  { type: "PLAN", status: "SUCCESS", step: null, parallel: null, data: null },
  { type: "STEP", status: "EXECUTE", step: { id: "n1", tool: "SQL查询" }, parallel: null, data: null },
  { type: "STEP", status: "SUCCESS", step: { id: "n1", tool: "SQL查询", status: "SUCCESS" }, parallel: null, data: null },
  { type: "REPORT", status: "SUCCESS", step: null, parallel: null, data: null },
  { type: "CONNECT", status: "FINISH", step: null, parallel: null, data: null },
];

// 进行中：无 FINISH/ERROR → running
const E_RUNNING: AgentEvent[] = [
  { type: "STEP", status: "EXECUTE", step: { id: "n1", tool: "SQL查询" }, parallel: null, data: null },
];

// 失败：有 ERROR，需要 status=EXECUTE+step 才能让 execTools.length=1
const E_FAIL: AgentEvent[] = [
  { type: "STEP", status: "EXECUTE", step: { id: "n2", tool: "Python", status: "FAILED" }, parallel: null, data: null },
];

describe("StageTimeline", () => {
  beforeEach(() => cleanup());

  it("折叠态（默认,FINISH）：button 可访问名含'执行完成' + 工具名", () => {
    render(<StageTimeline events={E_FINISH} />);
    const btn = screen.getByRole("button", { name: /执行完成/ });
    expect(btn).toBeInTheDocument();
    expect(btn.textContent).toContain("1 个工具");
    expect(btn.textContent).toContain("SQL查询");
  });

  it("点击折叠条 → 展开：显示'执行过程'标题 + 收起按钮；渲染 Timeline", () => {
    render(<StageTimeline events={E_FINISH} />);
    fireEvent.click(screen.getByRole("button", { name: /执行完成/ }));
    // 展开后标题
    expect(screen.getByText("执行过程")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("展开后点击'收起' → 回到折叠态", () => {
    render(<StageTimeline events={E_FINISH} />);
    fireEvent.click(screen.getByRole("button", { name: /执行完成/ }));
    expect(screen.getByText("执行过程")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.getByRole("button", { name: /执行完成/ })).toBeInTheDocument();
    expect(screen.queryByText("执行过程")).toBeNull();
  });

  it("失败事件：折叠态 button 含'执行异常 · 0/1 个工具成功'", () => {
    render(<StageTimeline events={E_FAIL} />);
    const btn = screen.getByRole("button", { name: /执行异常/ });
    expect(btn).toBeInTheDocument();
    expect(btn.textContent).toContain("0/1 个工具成功");
  });

  it("进行中（无 FINISH/ERROR）：折叠态 button 含'执行中'", () => {
    render(<StageTimeline events={E_RUNNING} />);
    const btn = screen.getByRole("button", { name: /执行中/ });
    expect(btn).toBeInTheDocument();
  });

  it("defaultExpanded=true：初始即为展开态", () => {
    render(<StageTimeline events={E_FINISH} defaultExpanded />);
    expect(screen.getByText("执行过程")).toBeInTheDocument();
  });
});
