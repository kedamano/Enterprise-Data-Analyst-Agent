// 会话侧栏覆盖盲区：渲染会话列表、点击切换会话、新建按钮、删除交互
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import type { Conversation } from "@/lib/types";
import { ConversationList } from "./ConversationList";

function makeConversation(id: string, title: string, updatedAt: number): Conversation {
  return {
    id,
    title,
    messages: [],
    createdAt: updatedAt - 100000,
    updatedAt,
  };
}

const sampleConversations: Conversation[] = [
  makeConversation("c-1", "营收分析", Date.now() - 60000),
  makeConversation("c-2", "区域对比", Date.now() - 3600000),
];

function renderList(overrides = {}) {
  const props = {
    conversations: sampleConversations,
    activeId: "c-1",
    open: true,
    onSelect: vi.fn(),
    onNew: vi.fn(),
    onDelete: vi.fn(),
    onToggle: vi.fn(),
    ...overrides,
  };
  return render(<ConversationList {...props} />);
}

describe("ConversationList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("渲染会话列表：显示会话标题", () => {
    renderList();
    expect(screen.getByText("营收分析")).toBeInTheDocument();
    expect(screen.getByText("区域对比")).toBeInTheDocument();
  });

  it("点击会话 → 触发 onSelect 回调", () => {
    const onSelect = vi.fn();
    renderList({ onSelect });

    fireEvent.click(screen.getByText("区域对比"));
    expect(onSelect).toHaveBeenCalledWith("c-2");
  });

  it("点击「新建对话」按钮 → 触发 onNew", () => {
    const onNew = vi.fn();
    renderList({ onNew });

    fireEvent.click(screen.getByRole("button", { name: /新建对话/ }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it("删除按钮 → 触发 onDelete", () => {
    const onDelete = vi.fn();
    renderList({ onDelete });

    // 删除按钮有 aria-label="删除会话"，取第一个
    const delBtn = screen.getAllByRole("button", { name: /删除会话/ })[0];
    fireEvent.click(delBtn);
    expect(onDelete).toHaveBeenCalledWith("c-1");
  });

  it("空会话列表 → 显示引导文案", () => {
    renderList({ conversations: [], activeId: null });
    expect(screen.getByText("还没有会话")).toBeInTheDocument();
  });
});
