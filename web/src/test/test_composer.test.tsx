// 本机 sandbox 受限，CI 可执行
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import type { Attachment } from "@/lib/types";
import { Composer } from "@/components/Composer";

// mock localStorage 默认行为
const mockAuthModule = vi.hoisted(() => ({
  getApiKey: vi.fn(() => ""),
  setApiKey: vi.fn(),
  authHeaders: vi.fn(() => ({})),
  AuthError: class extends Error {
    status: number;
    constructor(m: string, s: number) {
      super(m);
      this.name = "AuthError";
      this.status = s;
    }
  },
  maybeAuthError: vi.fn(),
}));

vi.mock("@/lib/auth", () => mockAuthModule);

// 提供可 mock 的 onSubmit / onStop
function renderComposer(streaming = false) {
  const onSubmit = vi.fn();
  const onStop = vi.fn();
  const utils = render(
    <Composer onSubmit={onSubmit} onStop={onStop} streaming={streaming} />,
  );
  return { onSubmit, onStop, ...utils };
}

describe("Composer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("输入文字 → 发送按钮由 disabled 变 enabled", () => {
    renderComposer();
    const sendBtn = screen.getByRole("button", { name: /发送/ });
    expect(sendBtn).toBeDisabled();
    const textarea = screen.getByRole("textbox", { name: /提问/ });
    fireEvent.change(textarea, { target: { value: "分析附件" } });
    expect(sendBtn).not.toBeDisabled();
  });

  it("Enter 键触发 submit（Ctrl/Meta + Enter）", async () => {
    const { onSubmit } = renderComposer();
    const textarea = screen.getByRole("textbox", { name: /提问/ });
    await fireEvent.change(textarea, { target: { value: "any" } });
    await fireEvent.keyDown(textarea, { key: "Enter", ctrlKey: true });
    // onSubmit 契约：(text, attachments, skillIds)。未勾选技能时第三参为空数组。
    expect(onSubmit).toHaveBeenCalledWith("any", [], []);
  });

  it("添加附件后 AttachmentList 显示（mock File）", () => {
    const { onSubmit } = renderComposer();
    const file = new File(["hello"], "sample.csv", { type: "text/csv" });
    const hiddenInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(hiddenInput, "files", { value: [file], configurable: true });
    fireEvent.change(hiddenInput);
    // 文件名会出现
    expect(screen.getByText("sample.csv")).toBeInTheDocument();
  });

  it("移除附件后列表消失", () => {
    const { onSubmit } = renderComposer();
    const file = new File(["x"], "a.csv", { type: "text/csv" });
    const hiddenInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(hiddenInput, "files", { value: [file], configurable: true });
    fireEvent.change(hiddenInput);
    expect(screen.getByText("a.csv")).toBeInTheDocument();
    const removeBtn = screen.getByRole("button", { name: /移除附件/ });
    fireEvent.click(removeBtn);
    expect(screen.queryByText("a.csv")).not.toBeInTheDocument();
  });

  it("streaming 时 Stop 按钮触发 onStop", () => {
    const { onStop } = renderComposer(true);
    const stopBtn = screen.getByRole("button", { name: /停止/ });
    fireEvent.click(stopBtn);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("streaming 时不渲染发送按钮，只显示停止提示", () => {
    renderComposer(true);
    expect(screen.queryByRole("button", { name: /发送/ })).not.toBeInTheDocument();
    expect(screen.getByText(/正在分析/)).toBeInTheDocument();
  });

  it("空 attachments + 空 text 时不调 onSubmit", () => {
    const { onSubmit } = renderComposer();
    // 输入再清空
    const textarea = screen.getByRole("textbox", { name: /提问/ });
    fireEvent.change(textarea, { target: { value: " " } });
    const sendBtn = screen.getByRole("button", { name: /发送/ });
    expect(sendBtn).toBeDisabled();
    fireEvent.click(sendBtn);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("粘贴图片会进入附件列表", () => {
    const { onSubmit } = renderComposer();
    const textarea = screen.getByRole("textbox", { name: /提问/ });
    // 真实剪贴板图片通常没有文件名；onPaste 会给它补 pasted-<ts>.<ext>
    const imgFile = new File(["data"], "", { type: "image/png" });
    const clipboardData = {
      items: [
        {
          kind: "file",
          getAsFile: () => imgFile,
        },
      ],
      types: ["Files"],
    } as unknown as DataTransfer;
    fireEvent.paste(textarea, { clipboardData });
    // 图片附件在 UI 中是缩略图，文件名挂在 alt 上（不是文本节点）
    expect(screen.getByAltText(/^pasted-.*\.png$/)).toBeInTheDocument();
  });
});
