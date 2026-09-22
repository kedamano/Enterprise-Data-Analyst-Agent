// animated-modal: Modal 弹窗渲染、Esc 关闭、点击蒙层关闭、滚动锁定、子组件渲染
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

vi.mock("motion/react", async (importOriginal) => {
  const actual: any = await importOriginal();
  const passthrough = ({ children, className, initial, animate, exit, transition, onClick, onMouseEnter, onMouseLeave, ..._ }: any) =>
    require("react").createElement("div", { className: className ?? null, onClick, onMouseEnter, onMouseLeave }, children);
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
  };
});

import {
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalProvider,
} from "./animated-modal";

describe("Modal", () => {
  beforeEach(() => {
    cleanup();
    document.body.style.overflow = "auto";
  });

  it("open=true 渲染子内容与关闭按钮", () => {
    render(
      <Modal open onClose={() => {}}>
        <ModalBody>弹窗正文</ModalBody>
      </Modal>
    );
    expect(screen.getByText("弹窗正文")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭" })).toBeInTheDocument();
  });

  it("open=false 不渲染任何内容", () => {
    render(
      <Modal open={false} onClose={() => {}}>
        <ModalBody>不应出现</ModalBody>
      </Modal>
    );
    expect(screen.queryByText("不应出现")).not.toBeInTheDocument();
  });

  it("点击关闭按钮触发 onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("点击蒙层（backdrop）触发 onClose", () => {
    const onClose = vi.fn();
    const result = render(
      <Modal open onClose={onClose}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    // Overlay 是第一个 fixed 全屏 div
    const backdrop = result.container.querySelector(".bg-ink\\/60");
    expect(backdrop).toBeTruthy();
    fireEvent.click(backdrop!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Esc 键触发 onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("打开时锁定 body 滚动，关闭时还原", () => {
    const result = render(
      <Modal open={false} onClose={() => {}}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    expect(document.body.style.overflow).toBe("auto");

    result.rerender(
      <Modal open onClose={() => {}}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    expect(document.body.style.overflow).toBe("hidden");

    result.rerender(
      <Modal open={false} onClose={() => {}}>
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    expect(document.body.style.overflow).toBe("auto");
  });

  it("自定义 className 追加到弹窗容器", () => {
    const result = render(
      <Modal open onClose={() => {}} className="custom-modal">
        <ModalBody>内容</ModalBody>
      </Modal>
    );
    expect(result.container.querySelector(".custom-modal")).toBeTruthy();
  });
});

describe("Modal sub-components", () => {
  beforeEach(() => cleanup());

  it("ModalBody 渲染子元素并带默认 padding class", () => {
    const result = render(
      <ModalBody>
        <span>body-child</span>
      </ModalBody>
    );
    expect(screen.getByText("body-child")).toBeInTheDocument();
    const bodyEl = result.container.querySelector(".p-6");
    expect(bodyEl).toBeTruthy();
  });

  it("ModalContent 渲染子元素并带 flex 布局", () => {
    const result = render(
      <ModalContent>
        <span>content-child</span>
      </ModalContent>
    );
    expect(screen.getByText("content-child")).toBeInTheDocument();
    expect(result.container.querySelector(".flex.flex-col.flex-1")).toBeTruthy();
  });

  it("ModalFooter 渲染子元素并带顶部边框", () => {
    const result = render(
      <ModalFooter>
        <button>确认</button>
      </ModalFooter>
    );
    expect(screen.getByText("确认")).toBeInTheDocument();
    expect(result.container.querySelector(".border-t.border-rule")).toBeTruthy();
  });

  it("ModalProvider 直接透传 children（向后兼容空实现）", () => {
    render(
      <ModalProvider>
        <span>provided</span>
      </ModalProvider>
    );
    expect(screen.getByText("provided")).toBeInTheDocument();
  });
});
