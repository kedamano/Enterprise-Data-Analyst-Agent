// FeedbackWidget: star select, submit, success, error, cancel
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { FeedbackWidget } from "./FeedbackWidget";
import { submitFeedback, fetchFeedback } from "@/lib/api";

// mock api.ts
vi.mock("@/lib/api", () => ({
  submitFeedback: vi.fn(),
  fetchFeedback: vi.fn(),
}));

// 通过所有 <button> 中 textContent 包含关键字的那个
function findBtnByText(substr: string): HTMLButtonElement {
  const btns = Array.from(document.querySelectorAll("button"));
  const found = btns.find((b) => b.textContent?.includes(substr));
  if (!found) throw new Error(`找不到包含「${substr}」的 button`);
  return found as HTMLButtonElement;
}

describe("FeedbackWidget", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  afterEach(() => {
    cleanup();
  });

  it("渲染：显示评分按钮（👍 有帮助 / 👎 没帮助）+ 无评论框", () => {
    render(<FeedbackWidget sessionId="test-session-1" />);

    expect(findBtnByText("有帮助")).toBeInTheDocument();
    expect(findBtnByText("没帮助")).toBeInTheDocument();
    // 初始不显示评论框发送按钮
    const btns = Array.from(document.querySelectorAll("button"));
    expect(btns.some((b) => b.textContent?.includes("发送反馈"))).toBe(false);
  });

  it("选星：点击 '👍 有帮助' → 高亮 text-verified 并展开评论框", async () => {
    render(<FeedbackWidget sessionId="test-session-2" />);

    fireEvent.click(findBtnByText("有帮助"));

    await waitFor(() => {
      const btns = Array.from(document.querySelectorAll("button"));
      expect(btns.some((b) => b.textContent?.includes("发送反馈"))).toBe(true);
    });

    // 高亮态：对应按钮应有 text-verified
    expect(findBtnByText("有帮助").className).toContain("text-verified");
  });

  it("提交：点 '发送反馈' → 调 submitFeedback 一次；提交中按钮 disabled", async () => {
    // 让 submit 挂起以验证中间状态
    let resolveSubmit: () => void;
    vi.mocked(submitFeedback).mockImplementation(
      () =>
        new Promise<void>((r) => {
          resolveSubmit = r;
        }),
    );

    render(<FeedbackWidget sessionId="test-session-3" />);

    fireEvent.click(findBtnByText("有帮助"));
    await waitFor(() => {
      expect(findBtnByText("发送反馈")).toBeDefined();
    });

    // 点提交
    fireEvent.click(findBtnByText("发送反馈"));

    await waitFor(() => {
      expect(findBtnByText("发送反馈").disabled).toBe(true);
    });

    expect(submitFeedback).toHaveBeenCalledTimes(1);

    // 清理
    resolveSubmit!();
  });

  it("成功：返回 200 → 显示 '已感谢' 状态（消失发送按钮）", async () => {
    vi.mocked(submitFeedback).mockResolvedValue(undefined);

    render(<FeedbackWidget sessionId="test-session-4" />);

    fireEvent.click(findBtnByText("有帮助"));
    await waitFor(() => {
      expect(findBtnByText("发送反馈")).toBeDefined();
    });

    fireEvent.click(findBtnByText("发送反馈"));

    await waitFor(() => {
      // 提交成功后进入 "已感谢" 态
      const thankText = document.querySelector(".text-verified");
      expect(thankText?.textContent).toContain("已感谢");
      // 发送按钮消失
      expect(findBtnByText.bind(null, "发送反馈")).toThrow();
    });
  });

  it("失败：网络错 → 显示错误文案", async () => {
    vi.mocked(submitFeedback).mockRejectedValue(new Error("网络异常"));

    render(<FeedbackWidget sessionId="test-session-5" />);

    fireEvent.click(findBtnByText("有帮助"));
    await waitFor(() => {
      expect(findBtnByText("发送反馈")).toBeDefined();
    });

    const textarea = document.querySelector("textarea")!;
    fireEvent.change(textarea, { target: { value: "分析结果不太准" } });
    fireEvent.click(findBtnByText("发送反馈"));

    await waitFor(() => {
      const errEl = document.querySelector(".text-danger");
      expect(errEl?.textContent).toBe("网络异常");
    });
  });

  it("选 '👎 没帮助' → 评论框展开，高亮 text-danger", async () => {
    vi.mocked(submitFeedback).mockResolvedValue(undefined);

    render(<FeedbackWidget sessionId="test-session-6" />);

    fireEvent.click(findBtnByText("没帮助"));

    await waitFor(() => {
      expect(findBtnByText("发送反馈")).toBeDefined();
    });

    // 高亮态应有 .text-danger
    expect(findBtnByText("没帮助").className).toContain("text-danger");
  });
});
