// ClarifyCard: renders questions, textarea + send button gating, answered vs not answered
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { ClarifyCard } from "./ClarifyCard";
import type { Clarification } from "@/lib/api";

const CLARIFY: Clarification = {
  objective: "统计口径需要确认",
  questions: ["是否包含退款？", "对比哪个时间段？", "按什么维度拆分？"],
  assumptions: ["按自然月"],
};

describe("ClarifyCard", () => {
  beforeEach(() => cleanup());

  it("渲染：标题为'需要你确认'，列出 3 个问题，含 objective 与 assumptions", () => {
    render(<ClarifyCard clarification={CLARIFY} />);
    expect(screen.getByText("需要你确认")).toBeInTheDocument();
    expect(screen.getByText("是否包含退款？")).toBeInTheDocument();
    expect(screen.getByText("对比哪个时间段？")).toBeInTheDocument();
    expect(screen.getByText("按什么维度拆分？")).toBeInTheDocument();
    expect(screen.getByText("目标：统计口径需要确认")).toBeInTheDocument();
    expect(screen.getByText("已假设：按自然月")).toBeInTheDocument();
  });

  it("未回答 + 有 onAnswer：显示 textarea 与发送按钮，空内容时按钮 disabled", () => {
    const onAnswer = vi.fn();
    render(<ClarifyCard clarification={CLARIFY} onAnswer={onAnswer} />);
    const ta = screen.getByLabelText("澄清回答");
    expect(ta).toBeInTheDocument();
    const sendBtn = screen.getByLabelText("提交澄清回答");
    expect(sendBtn).toBeDisabled();

    // 输入
    fireEvent.change(ta, { target: { value: "   " } });
    expect(sendBtn).toBeDisabled(); // 纯空格仍 disabled
    fireEvent.change(ta, { target: { value: "不含退款，同比去年" } });
    expect(sendBtn).not.toBeDisabled();
  });

  it("发送：点击发送按钮 → 调 onAnswer(text) 并清空 textarea", () => {
    const onAnswer = vi.fn();
    render(<ClarifyCard clarification={CLARIFY} onAnswer={onAnswer} />);
    const ta = screen.getByLabelText("澄清回答") as HTMLTextAreaElement;
    fireEvent.change(ta, { target: { value: "不含退款，同比去年" } });
    fireEvent.click(screen.getByLabelText("提交澄清回答"));
    expect(onAnswer).toHaveBeenCalledWith("不含退款，同比去年");
    expect(ta.value).toBe("");
  });

  it("Enter 触发提交（Shift+Enter 不触发）", () => {
    const onAnswer = vi.fn();
    render(<ClarifyCard clarification={CLARIFY} onAnswer={onAnswer} />);
    const ta = screen.getByLabelText("澄清回答");
    fireEvent.change(ta, { target: { value: "答" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  it("Shift+Enter 不触发提交", () => {
    const onAnswer = vi.fn();
    render(<ClarifyCard clarification={CLARIFY} onAnswer={onAnswer} />);
    const ta = screen.getByLabelText("澄清回答");
    fireEvent.change(ta, { target: { value: "答" } });
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onAnswer).not.toHaveBeenCalled();
  });

  it("answered=true：不渲染 textarea/按钮，标题变为'已澄清'", () => {
    render(<ClarifyCard clarification={CLARIFY} answered />);
    expect(screen.queryByLabelText("澄清回答")).toBeNull();
    expect(screen.queryByLabelText("提交澄清回答")).toBeNull();
    expect(screen.getByText("已澄清")).toBeInTheDocument();
  });

  it("无 onAnswer：不显示输入区与按钮", () => {
    render(<ClarifyCard clarification={CLARIFY} />);
    expect(screen.queryByLabelText("澄清回答")).toBeNull();
    expect(screen.queryByLabelText("提交澄清回答")).toBeNull();
  });
});
