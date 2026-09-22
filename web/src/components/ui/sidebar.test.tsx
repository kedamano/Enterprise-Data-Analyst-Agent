// sidebar: SidebarProvider / useSidebar / SidebarLink / DesktopSidebar / MobileSidebar
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

vi.mock("motion/react", async (importOriginal) => {
  const actual: any = await importOriginal();
  const withClass = ({ children, className, ..._ }: any) =>
    require("react").createElement("div", { className: className ?? null }, children);
  const justChildren = ({ children, ..._ }: any) => children;
  return {
    ...actual,
    motion: new Proxy(actual?.motion ?? {}, {
      get: (_t, prop) => {
        if (prop === "div") return withClass;
        return (props: any) => require("react").createElement("div", null, props.children);
      },
    }),
    AnimatePresence: justChildren,
  };
});

import {
  SidebarProvider,
  Sidebar,
  SidebarBody,
  DesktopSidebar,
  MobileSidebar,
  SidebarLink,
  useSidebar,
} from "./sidebar";

function ContextReader() {
  const { open, setOpen, animate } = useSidebar();
  return (
    <div>
      <span data-testid="open">{String(open)}</span>
      <span data-testid="animate">{String(animate)}</span>
      <button
        data-testid="toggle"
        onClick={() => setOpen((v) => !v)}
      >
        toggle
      </button>
    </div>
  );
}

describe("SidebarProvider", () => {
  beforeEach(() => cleanup());

  it("默认 open=false / animate=true", () => {
    render(
      <SidebarProvider>
        <ContextReader />
      </SidebarProvider>
    );
    expect(screen.getByTestId("open").textContent).toBe("false");
    expect(screen.getByTestId("animate").textContent).toBe("true");
  });

  it("可控 open / setOpen 由 props 接管", () => {
    const setExt = (_v: boolean) => {};
    render(
      <SidebarProvider open={true} setOpen={setExt} animate={false}>
        <ContextReader />
      </SidebarProvider>
    );
    expect(screen.getByTestId("open").textContent).toBe("true");
    expect(screen.getByTestId("animate").textContent).toBe("false");
  });

  it("toggle 按钮调用 setOpen 函数", () => {
    const setOpenFn = vi.fn();
    render(
      <SidebarProvider open={false} setOpen={setOpenFn}>
        <ContextReader />
      </SidebarProvider>
    );
    fireEvent.click(screen.getByTestId("toggle"));
    expect(setOpenFn).toHaveBeenCalledTimes(1);
  });
});

describe("useSidebar", () => {
  beforeEach(() => cleanup());

  it("在 Provider 外抛出错误", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      render(<ContextReader />);
    }).toThrow("useSidebar must be used within a SidebarProvider");
    spy.mockRestore();
  });
});

describe("Sidebar 组件", () => {
  beforeEach(() => cleanup());

  it("组合 Provider + 子元素渲染", () => {
    render(
      <Sidebar>
        <span>sidebar-content</span>
      </Sidebar>
    );
    expect(screen.getByText("sidebar-content")).toBeInTheDocument();
  });
});

describe("SidebarBody", () => {
  beforeEach(() => cleanup());

  it("同时渲染 DesktopSidebar 与 MobileSidebar", () => {
    const result = render(
      <Sidebar>
        <SidebarBody>
          <span>body-child</span>
        </SidebarBody>
      </Sidebar>
    );
    // Desktop: hidden md:flex；Mobile: md:hidden
    expect(result.container.querySelector(".hidden.md\\:flex")).toBeTruthy();
    expect(result.container.querySelector(".flex.md\\:hidden")).toBeTruthy();
  });
});

describe("DesktopSidebar", () => {
  beforeEach(() => cleanup());

  it("渲染子元素", () => {
    render(
      <Sidebar>
        <DesktopSidebar>
          <span>desktop-child</span>
        </DesktopSidebar>
      </Sidebar>
    );
    expect(screen.getByText("desktop-child")).toBeInTheDocument();
  });

  it("mouseEnter 触发 setOpen(true) - 不抛异常即可", () => {
    const result = render(
      <Sidebar>
        <DesktopSidebar>
          <span>desktop-child</span>
        </DesktopSidebar>
      </Sidebar>
    );
    const motionDiv = result.container.querySelector(".bg-white");
    expect(motionDiv).toBeTruthy();
    fireEvent.mouseEnter(motionDiv!);
    // 内部调 setOpen 由 Provider 接管，无异常即通过
  });
});

describe("MobileSidebar", () => {
  beforeEach(() => cleanup());

  it("默认隐藏弹窗，显示汉堡按钮", () => {
    render(
      <Sidebar>
        <MobileSidebar>
          <span>mobile-child</span>
        </MobileSidebar>
      </Sidebar>
    );
    // mobile-child 只在 open=true 才渲染；默认 open=false，不出现
    expect(screen.queryByText("mobile-child")).not.toBeInTheDocument();
  });

  it("点击菜单图标后 toggle open 状态", () => {
    render(
      <Sidebar>
        <MobileSidebar>
          <span>mobile-child</span>
        </MobileSidebar>
      </Sidebar>
    );
    // 找到 Menu2 所在 svg，其外层有 onClick
    const svg = document.querySelector("svg");
    expect(svg).toBeTruthy();
    fireEvent.click(svg!);
    // 打开后 mobile-child 应出现
    expect(screen.getByText("mobile-child")).toBeInTheDocument();
  });
});

describe("SidebarLink", () => {
  beforeEach(() => cleanup());

  it("渲染链接文本与 href", () => {
    const iconSpan = <span data-testid="icon">icon</span>;
    render(
      <Sidebar>
        <SidebarLink link={{ label: "首页", href: "/home", icon: iconSpan }} />
      </Sidebar>
    );
    const link = screen.getByText("首页").closest("a") as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.href).toContain("/home");
    expect(screen.getByTestId("icon")).toBeInTheDocument();
  });

  it("自定义 className 追加到 a 标签", () => {
    render(
      <Sidebar>
        <SidebarLink
          link={{ label: "设置", href: "/settings", icon: <span>i</span> }}
          className="extra-class"
        />
      </Sidebar>
    );
    const link = screen.getByText("设置").closest("a");
    expect(link!.className).toContain("extra-class");
  });
});
