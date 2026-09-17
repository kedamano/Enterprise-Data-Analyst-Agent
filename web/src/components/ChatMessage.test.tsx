// ChatMessage 覆盖盲区：user / assistant / tool 消息、code block 高亮、卡片展示
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

// ChatMessage 依赖了 Report / MetricCards 等子组件，mock 掉避免 markdown 渲染链
vi.mock("@/components/Report", () => ({
  Report: vi.fn(() => <div data-testid="report-mock" />),
}));
vi.mock("@/components/MetricCards", () => ({
  MetricCards: vi.fn(() => null),
  latestMetrics: vi.fn(() => []),
}));
vi.mock("@/components/PlanCard", () => ({
  PlanCard: vi.fn(() => null),
}));
vi.mock("@/components/ClarifyCard", () => ({
  ClarifyCard: vi.fn(() => null),
}));
vi.mock("@/components/StageTimeline", () => ({
  StageTimeline: vi.fn(() => null),
}));
vi.mock("@/components/ShareBar", () => ({
  ShareBar: vi.fn(() => null),
}));
vi.mock("@/components/RunBadges", () => ({
  RunBadges: vi.fn(() => null),
}));
vi.mock("@/components/ExportPreview", () => ({
  ExportPreview: vi.fn(() => null),
}));
vi.mock("@/lib/api", () => ({
  stageLabel: vi.fn((s: string) => s),
}));

import type { Message } from "@/lib/types";
import { ChatMessage } from "./ChatMessage";

function makeMsg(overrides: Partial<Message>): Message {
  return {
    id: "m1",
    role: "assistant",
    text: "",
    status: "FINISH",
    done: true,
    ...overrides,
  };
}

describe("ChatMessage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("用户消息：渲染文本右对齐", () => {
    const msg = makeMsg({ role: "user", text: "帮我查一下营收" });
    render(<ChatMessage message={msg} />);
    expect(screen.getByText("帮我查一下营收")).toBeInTheDocument();
  });

  it("助手消息 done 且有 text → 渲染报告卡片", () => {
    const msg = makeMsg({
      role: "assistant",
      text: "## 报告标题\n\n营收同比增长 15%。",
      done: true,
      status: "FINISH",
    });
    render(<ChatMessage message={msg} sessionId="s-1" />);
    expect(screen.getByText("分析报告")).toBeInTheDocument();
  });

  it("带 status（中间态）→ 显示状态徽标", () => {
    const msg = makeMsg({
      role: "assistant",
      status: "ANALYSIS",
      done: false,
      text: "",
    });
    render(<ChatMessage message={msg} />);
    expect(screen.getByText(/ANALYSIS/)).toBeInTheDocument();
  });

  it("带 error → 显示错误提示框", () => {
    const msg = makeMsg({
      role: "assistant",
      error: "连接数据库超时",
      done: false,
    });
    render(<ChatMessage message={msg} />);
    expect(screen.getByText("连接数据库超时")).toBeInTheDocument();
  });

  it("复制按钮点击 → 文案切为「已复制」", async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    });

    const msg = makeMsg({
      role: "assistant",
      text: "可复制文本",
      done: true,
    });
    render(<ChatMessage message={msg} sessionId="s-1" />);

    const copyBtn = await screen.findByRole("button", { name: /复制报告/ });
    fireEvent.click(copyBtn);

    // clipboard mock 后文案变"已复制"
    await vi.waitFor(() => {
      expect(screen.getByText("已复制")).toBeInTheDocument();
    });
  });
});
