// Composer: submit gating, placeholder, keyboard submit, skill picker, attachment add
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";
import { Composer } from "./Composer";
import { fetchSkills } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  fetchSkills: vi.fn(),
}));

import type { Skill } from "@/lib/api";

const SKILLS: Skill[] = [
  { id: "s1", name: "SQL 助拳", description: "写 SQL", enabled: true, origin: "manual", created_at: "", updated_at: "", chars: 0, path: "" },
  { id: "s2", name: "Python 计算", description: "写 Python", enabled: false, origin: "manual", created_at: "", updated_at: "", chars: 0, path: "" },
];

function renderComposer(opts: { streaming?: boolean } = {}) {
  const onSubmit = vi.fn();
  const onStop = vi.fn();
  render(<Composer onSubmit={onSubmit} onStop={onStop} streaming={opts.streaming ?? false} />);
  return { onSubmit, onStop };
}

describe("Composer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchSkills).mockResolvedValue({ skills: SKILLS, total: 2 });
    cleanup();
  });

  it("初始态：发送按钮 disabled（空输入 + 无附件）", () => {
    renderComposer();
    const sendBtn = screen.getByRole("button", { name: "发送" });
    expect(sendBtn).toBeDisabled();
  });

  it("输入文字后发送按钮 enabled；点击发送触发 onSubmit 并清空输入", async () => {
    const { onSubmit } = renderComposer();
    const ta = screen.getByPlaceholderText(/描述你的业务问题/);
    fireEvent.change(ta, { target: { value: "分析各渠道营收" } });

    const sendBtn = screen.getByRole("button", { name: "发送" });
    expect(sendBtn).not.toBeDisabled();

    fireEvent.click(sendBtn);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("分析各渠道营收", [], []);
    // 清空
    expect(ta).toHaveValue("");
  });

  it("Ctrl/⌘ + Enter 触发提交", () => {
    const { onSubmit } = renderComposer();
    const ta = screen.getByPlaceholderText(/描述你的业务问题/);
    fireEvent.change(ta, { target: { value: "hello" } });
    fireEvent.keyDown(ta, { key: "Enter", ctrlKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("输入框为空 + 无附件：即使按 Enter（Meta）也不提交", () => {
    const { onSubmit } = renderComposer();
    const ta = screen.getByPlaceholderText(/描述你的业务问题/);
    fireEvent.keyDown(ta, { key: "Enter", metaKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("技能选择器显示已启用技能；选中后触发 onSubmit 时携带 skillIds", async () => {
    const { onSubmit } = renderComposer();

    // 等 fetchSkills 完成 → 下拉渲染
    const pickerBtn = await screen.findByRole("button", { name: /技能/ });
    fireEvent.click(pickerBtn);

    // 只有 enabled 技能出现在选项里
    const listbox = await screen.findByRole("listbox");
    const options = within(listbox).getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("SQL 助拳");

    // 选中
    fireEvent.click(options[0]);

    // 输入 + 提交
    const ta = screen.getByPlaceholderText(/描述你的业务问题/);
    fireEvent.change(ta, { target: { value: "q" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(onSubmit).toHaveBeenCalledWith("q", [], ["s1"]);
  });

  it("callback 模式下（onStop）不渲染输入，渲染停止按钮 + 动画", () => {
    renderComposer({ streaming: true });
    expect(screen.queryByPlaceholderText(/描述你的业务问题/)).toBeNull();
    expect(screen.getByRole("button", { name: /停止/ })).toBeInTheDocument();
    expect(screen.getByText(/智能体正在分析/)).toBeInTheDocument();
  });

  it("附件追加：调用 hidden input 的 onChange 后预览行出现在 composer 内", () => {
    const { onSubmit } = renderComposer();
    // 构造一个 File
    const f = new File(["a,b\n1,2"], "demo.csv", { type: "text/csv" });
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(fileInput).toBeTruthy();
    Object.defineProperty(fileInput, "files", { value: [f], configurable: true });
    fireEvent.change(fileInput);

    // 预览 chip 出现（文件名）
    expect(screen.getByText("demo.csv")).toBeInTheDocument();
  });

  it("fetchSkills 失败静默：输入框仍可用，技能选择器空", async () => {
    vi.mocked(fetchSkills).mockRejectedValue(new Error("network"));
    renderComposer();
    const ta = screen.getByPlaceholderText(/描述你的业务问题/);
    fireEvent.change(ta, { target: { value: "still works" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    // 不抛错即可
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });
});
