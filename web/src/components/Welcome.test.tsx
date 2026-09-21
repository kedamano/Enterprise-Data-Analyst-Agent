// Welcome: highlights, examples, action buttons (upload / data sources / knowledge)
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { Welcome } from "./Welcome";

describe("Welcome", () => {
  beforeEach(() => cleanup());

  it("渲染主标题与副标语", () => {
    render(<Welcome onPick={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: /企业数据分析智能体/ })).toBeInTheDocument();
    expect(
      screen.getByText("描述一个业务问题，智能体会自行取数、计算并给出结论，每一步都留下可复核的依据。"),
    ).toBeInTheDocument();
  });

  it("三个 HIGHLIGHTS 卡片", () => {
    render(<Welcome onPick={() => {}} />);
    expect(screen.getByText("连接真实数据")).toBeInTheDocument();
    expect(screen.getByText("引用业务口径")).toBeInTheDocument();
    expect(screen.getByText("结论可追溯")).toBeInTheDocument();
  });

  it("6 个 EXAMPLES：渲染并被点击时调用 onPick", () => {
    const calls: string[] = [];
    render(<Welcome onPick={(t) => calls.push(t)} />);

    // Welcome 的示例不是 button[a11y name=正则], 而是 <button><span>{text}</span></button>
    // 用文本全文匹配
    const btn = screen.getByRole("button", { name: /对比各区域营收表现，识别增长最快的地区/ });
    expect(btn).toBeInTheDocument();
    fireEvent.click(btn);
    expect(calls.length).toBe(1);
    expect(calls[0]).toContain("对比各区域营收表现");
  });

  it("上传文件按钮：点击时调用 onUpload", () => {
    const onUpload = vi.fn();
    render(<Welcome onPick={() => {}} onUpload={onUpload} />);
    fireEvent.click(screen.getByRole("button", { name: "上传文件" }));
    expect(onUpload).toHaveBeenCalledTimes(1);
  });

  it("上传文件按钮不传 onUpload 时不渲染", () => {
    render(<Welcome onPick={() => {}} />);
    expect(screen.queryByRole("button", { name: "上传文件" })).toBeNull();
  });

  it("添加知识按钮 + 查看数据源按钮：仅在有回调时出现并触发回调", () => {
    const onAddKnowledge = vi.fn();
    const onDataSources = vi.fn();
    render(
      <Welcome
        onPick={() => {}}
        onAddKnowledge={onAddKnowledge}
        onDataSources={onDataSources}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "查看数据源" }));
    expect(onDataSources).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "添加知识" }));
    expect(onAddKnowledge).toHaveBeenCalledTimes(1);
  });

  it("未提供 onAddKnowledge / onDataSources：不渲染对应按钮", () => {
    render(<Welcome onPick={() => {}} />);
    expect(screen.queryByRole("button", { name: "添加知识" })).toBeNull();
    expect(screen.queryByRole("button", { name: "查看数据源" })).toBeNull();
  });
});
