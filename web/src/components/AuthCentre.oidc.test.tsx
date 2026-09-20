// AuthCentre OIDC: 默认无 OIDC 按钮 / 探测到 OIDC 配置 / 跳转 / 登录失败 / 登录成功存 token
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import { AuthCentre } from "./AuthCentre";

// AuthCentre 依赖 @/lib/user 的 login/register 与 @/lib/auth 的 setApiKey。
// 另有 fetch /api/v1/auth/oidc/config（内嵌调用，非走 lib），mock global.fetch。
vi.mock("@/lib/auth", () => ({ setApiKey: vi.fn() }));
vi.mock("@/lib/user", () => ({
  login: vi.fn(),
  register: vi.fn(),
}));

import { setApiKey } from "@/lib/auth";
import { login, register } from "@/lib/user";

function activePane(): HTMLElement {
  const el = document.querySelector<HTMLElement>('[aria-hidden="false"]');
  if (!el) throw new Error("找不到活动的表单面板");
  return el;
}

function field(placeholder: string): HTMLElement {
  const pane = activePane();
  const all = pane.querySelectorAll<HTMLElement>(
    `input[placeholder="${placeholder}"]`,
  );
  if (all.length === 0) throw new Error(`找不到字段：${placeholder}`);
  return all[0];
}

function withinActive<T extends HTMLElement = HTMLElement>(selector: string): T {
  const pane = activePane();
  const el = pane.querySelector<T>(selector);
  if (!el) throw new Error(`活动面板内找不到：${selector}`);
  return el;
}

describe("AuthCentre OIDC 流程", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>;
  let setHref: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(login).mockReset();
    vi.mocked(register).mockReset();
    vi.mocked(setApiKey).mockReset();
    cleanup();

    // location.href 在 jsdom 只读，用 Proxy 拦截 setter
    setHref = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: new Proxy(originalLocation, {
        set(_t, prop, value) {
          if (prop === "href") {
            setHref(String(value));
          }
          return true;
        },
      }),
    });

    // 默认 fetch 返回 OIDC 未配置
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ configured: false, issuer: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
    cleanup();
  });

  it("默认渲染：用户名输入框可见；OIDC 按钮隐藏", async () => {
    render(<AuthCentre onAuthed={vi.fn()} />);

    // 用户名输入框可见
    expect(field("用户名")).toBeInTheDocument();

    // OIDC 按钮默认不存在（因为 fetch 返回 configured: false）
    await waitFor(() => {
      expect(screen.queryByText("使用 OIDC 登录")).not.toBeInTheDocument();
    });
  });

  it("OIDC 配置探测成功 → OIDC 按钮显示", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ configured: true, issuer: "https://auth.example.com" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<AuthCentre onAuthed={vi.fn()} />);

    await waitFor(() => {
      const oidcBtn = screen.getByRole("button", { name: "使用 OIDC 登录" });
      expect(oidcBtn).toBeInTheDocument();
    });
  });

  it("OIDC 跳转：点 OIDC 按钮 → location.href 含 /api/v1/auth/oidc/login", async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ configured: true, issuer: "https://auth.example.com" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<AuthCentre onAuthed={vi.fn()} />);

    const oidcBtn = await screen.findByRole("button", { name: "使用 OIDC 登录" });
    fireEvent.click(oidcBtn);

    await waitFor(() => {
      expect(setHref).toHaveBeenCalled();
      const calledWith = setHref.mock.calls[0]?.[0];
      expect(String(calledWith)).toContain("/api/v1/auth/oidc/login");
    });
  });

  it("登录失败：login 返回错误 + fallback 也失败 → 显示错误提示", async () => {
    // login 失败，且 fallback (setApiKey) 也失败 → 无法掩盖错误
    vi.mocked(login).mockRejectedValue(new Error("认证失败"));
    vi.mocked(setApiKey).mockImplementation(() => {
      throw new Error("本地写入失败");
    });

    const onAuthed = vi.fn();
    render(<AuthCentre onAuthed={onAuthed} />);

    fireEvent.change(field("用户名"), { target: { value: "baduser" } });
    fireEvent.change(field("密码"), { target: { value: "wrongpwd" } });

    fireEvent.click(withinActive("button[type='submit']"));

    await waitFor(() => {
      // 错误可能在出问题的 pane 或其祖先，全局搜索
      const errEl = document.querySelector(".text-danger");
      expect(errEl?.textContent).toBeTruthy();
    });
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("登录成功：login 成功 → onAuthed 调用", async () => {
    vi.mocked(login).mockResolvedValue({
      id: "1",
      username: "alice",
      role: "admin",
    } as never);

    const onAuthed = vi.fn();
    render(<AuthCentre onAuthed={onAuthed} />);

    fireEvent.change(field("用户名"), { target: { value: "alice" } });
    fireEvent.change(field("密码"), { target: { value: "correctPwd" } });

    fireEvent.click(withinActive("button[type='submit']"));

    await waitFor(() => {
      expect(onAuthed).toHaveBeenCalledTimes(1);
    });
    // login 内部走 user.ts 的 withToken 会调用 setUserToken → localStorage setItem
    // 这里只校验 onAuthed 被调用（token 写入由 user.ts 自己保证）
  });
});
