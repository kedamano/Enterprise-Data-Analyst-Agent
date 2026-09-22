// Timeline: 数据渲染、title/content 映射、tone 色板 class
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { Timeline } from "./timeline";

vi.mock("motion/react", async (importOriginal) => {
  const actual: any = await importOriginal();
  const passthrough = ({ children, className, ..._ }: any) =>
    require("react").createElement("div", { className: className ?? null }, children);
  return {
    ...actual,
    motion: new Proxy(actual?.motion ?? {}, {
      get: (_t, prop) => {
        if (prop === "div") return passthrough;
        return (props: any) => require("react").createElement("div", null, props.children);
      },
    }),
    useScroll: () => ({ scrollYProgress: { get: () => 0, on: () => {} } }),
    useTransform: (_: any, __: any, out: any) => ({
      get: () => out?.[0] ?? 0,
      on: () => {},
    }),
  };
});

const sampleData = [
  { title: "数据接入", content: <span>接入正文</span> },
  { title: "数据分析", content: <span>分析正文</span>, tone: "active" as const },
  { title: "报告生成", content: <span>报告正文</span>, tone: "success" as const },
  { title: "异常步骤", content: <span>异常正文</span>, tone: "error" as const },
];

describe("Timeline", () => {
  beforeEach(() => cleanup());

  it("渲染所有条目的 title 与 content", () => {
    render(<Timeline data={sampleData} />);
    for (const entry of sampleData) {
      expect(screen.getAllByText(entry.title).length).toBeGreaterThan(0);
    }
    expect(screen.getByText("接入正文")).toBeInTheDocument();
    expect(screen.getByText("分析正文")).toBeInTheDocument();
    expect(screen.getByText("报告正文")).toBeInTheDocument();
    expect(screen.getByText("异常正文")).toBeInTheDocument();
  });

  it("默认 tone=idle 条目使用规则灰点 class", () => {
    const result = render(<Timeline data={[sampleData[0]]} />);
    expect(result.container.querySelector(".bg-rule.border-rule-strong")).toBeTruthy();
  });

  it("active tone 条目圆点含 brand 配色", () => {
    const result = render(<Timeline data={[sampleData[1]]} />);
    expect(result.container.querySelector(".bg-brand.border-brand")).toBeTruthy();
  });

  it("success tone 条目圆点含 verified 配色", () => {
    const result = render(<Timeline data={[sampleData[2]]} />);
    expect(result.container.querySelector(".bg-verified.border-verified")).toBeTruthy();
  });

  it("error tone 条目圆点含 danger 配色", () => {
    const result = render(<Timeline data={[sampleData[3]]} />);
    expect(result.container.querySelector(".bg-danger.border-danger")).toBeTruthy();
  });

  it("warn tone 条目标题文字使用 attention 色", () => {
    render(
      <Timeline
        data={[
          { title: "警告步骤", content: "x", tone: "warn" as const },
        ]}
      />
    );
    const titleEls = screen.getAllByText("警告步骤");
    expect(titleEls.length).toBeGreaterThan(0);
    expect(titleEls[0].className).toContain("text-attention");
  });

  it("空数组数据不报错", () => {
    const result = render(<Timeline data={[]} />);
    expect(result.container.querySelector(".max-w-7xl")).toBeTruthy();
  });
});
