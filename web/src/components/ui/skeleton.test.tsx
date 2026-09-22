// skeleton: SkeletonLine / SkeletonCard / SkeletonList / SkeletonTable 渲染 & aria-hidden
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  SkeletonLine,
  SkeletonCard,
  SkeletonList,
  SkeletonTable,
} from "./skeleton";

describe("SkeletonLine", () => {
  beforeEach(() => cleanup());

  it("默认渲染：h-3 高度 + animate-pulse + rounded-control", () => {
    const { container } = render(<SkeletonLine />);
    const el = container.querySelector(".animate-pulse");
    expect(el).not.toBeNull();
    expect(el!.className).toContain("h-3");
    expect(el!.className).toContain("rounded-control");
  });

  it("自定义 className 追加", () => {
    const { container } = render(<SkeletonLine className="w-1/2" />);
    const el = container.querySelector(".animate-pulse");
    expect(el!.className).toContain("w-1/2");
  });

  it("内联 style 透传", () => {
    const { container } = render(<SkeletonLine style={{ width: "50%" }} />);
    const el = container.querySelector(".animate-pulse") as HTMLElement;
    expect(el.style.width).toBe("50%");
  });

  it("带 aria-hidden 标记", () => {
    const { container } = render(<SkeletonLine />);
    const el = container.querySelector('[aria-hidden]');
    expect(el).not.toBeNull();
  });
});

describe("SkeletonCard", () => {
  beforeEach(() => cleanup());

  it("默认 2 行文本骨架", () => {
    const { container } = render(<SkeletonCard />);
    // 1 行标题 + 2 行正文 = 3 个 animate-pulse 行 + 1 个头像 = 4 个 animate-pulse
    const pulses = container.querySelectorAll(".animate-pulse");
    expect(pulses.length).toBe(4);
  });

  it("自定义 lines 数量", () => {
    const { container } = render(<SkeletonCard lines={4} />);
    // 1 标题 + 4 行正文 + 1 头像 = 6
    const pulses = container.querySelectorAll(".animate-pulse");
    expect(pulses.length).toBe(6);
  });

  it("使用 borderRadius 容器且带 border-rule", () => {
    const { container } = render(<SkeletonCard />);
    expect(container.querySelector(".rounded-panel.border.border-rule")).toBeTruthy();
  });
});

describe("SkeletonList", () => {
  beforeEach(() => cleanup());

  it("默认渲染 4 行", () => {
    const { container } = render(<SkeletonList />);
    const pulses = container.querySelectorAll(".animate-pulse");
    // 每行: 1 头像 + 2 行文本 = 3, 4 行共 12
    expect(pulses.length).toBe(12);
  });

  it("自定义 rows 数量", () => {
    const { container } = render(<SkeletonList rows={2} />);
    const pulses = container.querySelectorAll(".animate-pulse");
    expect(pulses.length).toBe(6);
  });

  it("根容器带 aria-hidden", () => {
    const { container } = render(<SkeletonList />);
    const root = container.querySelector('[aria-hidden]');
    expect(root).not.toBeNull();
  });
});

describe("SkeletonTable", () => {
  beforeEach(() => cleanup());

  it("默认 5 行数据 + 表头", () => {
    const { container } = render(<SkeletonTable />);
    const pulses = container.querySelectorAll(".animate-pulse");
    // 表头 4 + 每行 4 × 5 行 = 24
    expect(pulses.length).toBe(24);
  });

  it("自定义 rows=3", () => {
    const { container } = render(<SkeletonTable rows={3} />);
    const pulses = container.querySelectorAll(".animate-pulse");
    // 4 + 4×3 = 16
    expect(pulses.length).toBe(16);
  });

  it("根容器带 aria-hidden", () => {
    const { container } = render(<SkeletonTable />);
    const root = container.querySelector('[aria-hidden]');
    expect(root).not.toBeNull();
  });
});
