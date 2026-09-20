// CommandPalette: hidden by default, Ctrl+K opens, search filters, click closes, Esc closes
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { CommandPalette } from "./CommandPalette";

// cmdk Dialog 在 jsdom 下行为受限，直接 mock 整个模块
vi.mock("cmdk", () => {
  const React = require("react");
  const h = React.createElement;
  return {
    Command: Object.assign(
      React.forwardRef(({ children, ...props }: any, ref: any) =>
        h("div", { ref, "data-testid": "cmdk-root", ...props }, children),
      ),
      {
        Dialog: ({ children, open, onOpenChange, ...props }: any) => {
          // 模拟 cmdk Dialog：监听 Escape 键调 onOpenChange(false)
          React.useEffect(() => {
            if (!open) return;
            const onKey = (e: KeyboardEvent) => {
              if (e.key === "Escape") onOpenChange?.(false);
            };
            window.addEventListener("keydown", onKey);
            return () => window.removeEventListener("keydown", onKey);
          }, [open, onOpenChange]);
          // Dialog 受控：open=false 时隐藏 children
          if (!open) {
            return h("div", { "data-testid": "cmdk-dialog", "data-open": "false", hidden: true, ...props });
          }
          return h("div", { "data-testid": "cmdk-dialog", "data-open": "true", ...props }, children);
        },
        Input: (props: any) => h("input", { "data-testid": "cmdk-input", ...props }),
        List: ({ children, ...props }: any) => h("div", { "data-testid": "cmdk-list", ...props }, children),
        Empty: (props: any) => h("div", props),
        Group: ({ children, heading, ...props }: any) =>
          h("div", { "data-testid": `group-${heading}`, ...props }, children),
        Item: ({ children, onSelect, value, ...props }: any) =>
          h("div", { "data-testid": "cmdk-item", role: "option", "data-value": value ?? "", onClick: onSelect, ...props }, children),
      },
    ),
  };
});

function renderPalette() {
  const onSwitchView = vi.fn();
  const onToggleTheme = vi.fn();
  const onFeedbackOpen = vi.fn();
  const onNewConversation = vi.fn();
  render(
    <CommandPalette
      onSwitchView={onSwitchView}
      onToggleTheme={onToggleTheme}
      onFeedbackOpen={onFeedbackOpen}
      onNewConversation={onNewConversation}
    />,
  );
  return { onSwitchView, onToggleTheme, onFeedbackOpen, onNewConversation };
}

describe("CommandPalette", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  afterEach(() => {
    cleanup();
  });

  it("默认不渲染面板（hidden）", () => {
    renderPalette();
    const dialog = screen.getByTestId("cmdk-dialog");
    expect(dialog.getAttribute("data-open")).toBe("false");
  });

  it("Ctrl+K → 面板显示（Command 组件渲染）", async () => {
    renderPalette();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    await waitFor(() => {
      const dialog = screen.getByTestId("cmdk-dialog");
      expect(dialog.getAttribute("data-open")).toBe("true");
    });
    // 搜索框出现
    expect(screen.getByTestId("cmdk-input")).toBeInTheDocument();
  });

  it("点击命令 → onSelect 触发、面板关闭", async () => {
    const { onNewConversation } = renderPalette();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    await waitFor(() => {
      expect(screen.getByTestId("cmdk-input")).toBeInTheDocument();
    });

    // 点「新对话」
    fireEvent.click(screen.getByText("新对话"));

    await waitFor(() => {
      const dialog = screen.getByTestId("cmdk-dialog");
      expect(dialog.getAttribute("data-open")).toBe("false");
    });
    expect(onNewConversation).toHaveBeenCalledTimes(1);
  });

  it("Esc → 面板关闭", async () => {
    renderPalette();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    await waitFor(() => {
      expect(screen.getByTestId("cmdk-input")).toBeInTheDocument();
    });

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      const dialog = screen.getByTestId("cmdk-dialog");
      expect(dialog.getAttribute("data-open")).toBe("false");
    });
  });

  it("Cmd+K → 面板显示", async () => {
    renderPalette();
    fireEvent.keyDown(window, { key: "k", metaKey: true });

    await waitFor(() => {
      const dialog = screen.getByTestId("cmdk-dialog");
      expect(dialog.getAttribute("data-open")).toBe("true");
    });
  });
});
