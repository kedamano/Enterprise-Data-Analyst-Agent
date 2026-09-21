// ChatMessage: user/assistant roles, event timeline + plan rendering, export link & ShareBar gating
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { ChatMessage } from "./ChatMessage";
import type { Message } from "@/lib/types";

// framer-motion 在 jsdom 内会异步调度动画，导致断言时机不准。
// 模拟所有组件变体为纯 div / Fragment passthrough——测试只关心渲染结果。
vi.mock("motion/react", async (importOriginal) => {
  const actual: any = await importOriginal();
  const passthrough = ({ children, ..._ }: any) =>
    require("react").createElement("div", null, children);
  const justChildren = ({ children, ..._ }: any) => children;
  return {
    ...actual,
    motion: new Proxy(actual?.motion ?? {}, {
      get: (_t, prop) => {
        if (prop === "div") return passthrough;
        return (props: any) => require("react").createElement("div", null, props.children);
      },
    }),
    AnimatePresence: justChildren,
    useScroll: () => ({ scrollYProgress: { get: () => 0, on: () => {} } }),
    useTransform: (_: any, __: any, out: any) => ({ get: () => out?.[0] ?? 0, on: () => {} }),
  };
});

const baseMsg: Message = {
  id: "m1",
  role: "user",
  text: "Q",
  events: [],
  status: "INIT",
  objective: null,
  done: false,
  error: null,
  clarification: null,
  answered: false,
};

function partial(over: Partial<Message>): Message {
  return { ...baseMsg, ...over };
}

describe("ChatMessage", () => {
  beforeEach(() => cleanup());

  it("用户消息：直接渲染 text，无 SkillPlan / Workflow 屑", () => {
    render(<ChatMessage message={partial({ role: "user", text: "分析本季度营收" })} />);
    expect(screen.getByText("分析本季度营收")).toBeInTheDocument();
    // 无 0/0 之类步骤屑
    expect(screen.queryByText(/未匹配到工作流/)).toBeNull();
  });

  it("助手消息 done + text：渲染分析报告 + 导出链接 + ShareBar", () => {
    render(
      <ChatMessage
        message={partial({
          role: "assistant",
          text: "# 营收分析\n总营收 100。",
          done: true,
        })}
        sessionId="abc"
      />,
    );
    // Export link present
    const link = screen.getByLabelText("导出交付包");
    expect(link).toBeInTheDocument();
    expect((link as HTMLAnchorElement).href).toContain("/api/v1/chat/analyze/export/abc?format=zip");
    // 分析报告标题
    expect(screen.getByText("分析报告")).toBeInTheDocument();
  });

  it("助手消息失败：直接渲染 error 文本", () => {
    render(
      <ChatMessage
        message={partial({ role: "assistant", done: false, error: "上游服务异常" })}
      />,
    );
    expect(screen.getByText("上游服务异常")).toBeInTheDocument();
  });

  it("助手事件列表：渲染 StageTimeline（done=false 默认展开，渲染'收起'按钮）", () => {
    render(
      <ChatMessage
        message={partial({
          role: "assistant",
          done: false,
          events: [
            { type: "CONNECT", status: "SUCCESS", step: null, parallel: null, data: null },
            { type: "STEP",  status: "EXECUTE", step: { id: "n2", tool: "清洗" }, parallel: null, data: null },
          ],
        })}
      />,
    );
    // ChatMessage 传 defaultExpanded={!isDone}=true → StageTimeline 展开态 → "收起" 按钮
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
    // 展开态 Timeline 的 StepCard 含 step.tool
    expect(screen.getByText("清洗")).toBeInTheDocument();
  });

  it("未 done 且无 sessionId：不渲染导出链接", () => {
    render(
      <ChatMessage
        message={partial({ role: "assistant", done: false })}
      />,
    );
    expect(screen.queryByLabelText("导出交付包")).toBeNull();
  });
});
