// Report: renders Markdown directly, unwraps JSON envelope, degraded state
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";
import { Report } from "./Report";

const MARKDOWN = `# 营收分析

| 渠道 | 营收 |
|------|------|
| 线上 | 100 |
| 线下 | 80 |

总营收 180 万元。
`;

// Report 只接受 content / markdown / text / report 字段
const JSON_ENVELOPE = JSON.stringify({
  content: "# 营收分析\n\n| 渠道 | 营收 |\n|------|------|\n| 线上 | 100 |\n\n总营收 100 万元。",
  status_code: 0,
});

const JSON_DEGRADED = JSON.stringify({ __markdown__: true, status_code: 0, ts: 1 });

describe("Report", () => {
  beforeEach(() => cleanup());

  it("Markdown 直接传入：渲染为 <h1> 与 <table>", () => {
    render(<Report raw={MARKDOWN} />);
    expect(screen.getByRole("heading", { level: 1, name: "营收分析" })).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("线上")).toBeInTheDocument();
    expect(within(table).getByText("100")).toBeInTheDocument();
  });

  it("JSON 包膜：自动解析 content 字段后渲染", () => {
    render(<Report raw={JSON_ENVELOPE} />);
    expect(screen.getByRole("heading", { level: 1, name: "营收分析" })).toBeInTheDocument();
  });

  it("JSON 仅有 __markdown__ 信号：降级为友好提示", () => {
    render(<Report raw={JSON_DEGRADED} />);
    // degraded 节点同时渲染 h 级别的"报告未生成"与段落说明
    expect(screen.getByText("报告未生成")).toBeInTheDocument();
    expect(screen.getByText(/请稍后重试/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  });

  it("空 / 空白字符串：降级提示", () => {
    render(<Report raw={"   "} />);
    expect(screen.getByText("报告未生成")).toBeInTheDocument();
  });

  it("不可解析的 JSON 字符串：按纯文本渲染", () => {
    render(<Report raw={"plain text not json"} />);
    expect(screen.getByText(/plain text not json/)).toBeInTheDocument();
  });

  it("有 sessionId 时：渲染 FeedbackWidget", () => {
    render(<Report raw={MARKDOWN} sessionId="sess-1" />);
    expect(screen.getByText(/这个分析结果对你有帮助吗/)).toBeInTheDocument();
  });

  it("无 sessionId 时：不渲染 FeedbackWidget", () => {
    render(<Report raw={MARKDOWN} />);
    expect(screen.queryByText(/这个分析结果对你有帮助吗/)).toBeNull();
  });
});
