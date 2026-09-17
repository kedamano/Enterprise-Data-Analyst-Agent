import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, within } from "@testing-library/react";
import type { AuthUser } from "@/lib/user";
import { AuthCentre } from "./AuthCentre";

// AuthCentre 依赖两套后端：
//   - @/lib/auth  → setApiKey（静态 api key，写入 da_api_key）
//   - @/lib/user  → login / register（Bearer token 体系）
// 单测不跑链，全部 mock。
vi.mock("@/lib/auth", () => ({ setApiKey: vi.fn() }));
vi.mock("@/lib/user", () => ({
  login: vi.fn(),
  register: vi.fn(),
}));

import { setApiKey } from "@/lib/auth";
import { login, register } from "@/lib/user";

function renderAuth() {
  const onAuthed = vi.fn();
  const onSkip = vi.fn();
  render(<AuthCentre onAuthed={onAuthed} onSkip={onSkip} />);
  return { onAuthed, onSkip };
}

// overlay 引导按钮（切换登录/注册用）："创建账号" / "登录"。
// 注意 overlay 的两半都常驻 DOM，且与表单提交按钮**同名**，所以按 type 区分——
// 引导按钮是 type="button"，表单提交是 type="submit"（见下面的 submitBtn）。
function pickSwitch(name: string | RegExp) {
  const btn = screen
    .getAllByRole("button", { name })
    .find((b) => (b as HTMLButtonElement).type === "button");
  if (!btn) throw new Error(`找不到引导按钮：${name}`);
  return btn;
}
function getSwitchToRegisterBtn() {
  return pickSwitch("创建账号");
}
function getSwitchToLoginBtn() {
  return pickSwitch(/^登录$/);
}

// AuthCentre 是双滑块：两块面板都常驻 DOM，非活动的一块带 aria-hidden="true"。
// getByRole 默认跳过无障碍树里不可见的元素，但 getByPlaceholderText 不做这层过滤——
// 两块面板的字段 placeholder 完全相同，直接查会命中两个。所以一律限定到活动面板。
function activePane() {
  const el = document.querySelector<HTMLElement>('[aria-hidden="false"]');
  if (!el) throw new Error("找不到活动的表单面板");
  return el;
}
function field(placeholder: string) {
  return within(activePane()).getByPlaceholderText(placeholder);
}
/** 活动面板里的表单提交按钮。 */
function submitBtn(name: string | RegExp) {
  return within(activePane()).getByRole("button", { name });
}

describe("AuthCentre", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(login).mockReset();
    vi.mocked(register).mockReset();
    vi.mocked(setApiKey).mockReset();
    cleanup();
  });

  it("初始态显示登录表单与注册引导 overlay", () => {
    renderAuth();
    expect(screen.getByRole("heading", { name: "登录" })).toBeInTheDocument();
    expect(screen.getByText("你好，访客！")).toBeInTheDocument();
    expect(getSwitchToRegisterBtn()).toBeInTheDocument();
  });

  it("login 模式下按钮在字段空时 disabled", () => {
    renderAuth();
    const submit = submitBtn(/^登录$/);
    expect(submit).toBeDisabled();
  });

  it("切换到注册模式：显示创建账号表单与登录引导", () => {
    renderAuth();
    fireEvent.click(getSwitchToRegisterBtn());

    expect(screen.getByRole("heading", { name: "创建账号" })).toBeInTheDocument();
    expect(screen.getByText("欢迎回来！")).toBeInTheDocument();
    expect(getSwitchToLoginBtn()).toBeInTheDocument();
  });

  it("用户按 Escape 键 → 调 onSkip", () => {
    const { onSkip } = renderAuth();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onSkip).toHaveBeenCalledTimes(1);
  });

  it("右上角「跳过」按钮 → 调 onSkip 不调 onAuthed", () => {
    const { onAuthed, onSkip } = renderAuth();
    const skipBtn = screen.getByRole("button", { name: /跳过/ });
    expect(skipBtn).toBeInTheDocument();
    fireEvent.click(skipBtn);
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("注册：密码不足 6 位时显示错误且不调用 onAuthed", () => {
    const { onAuthed } = renderAuth();
    fireEvent.click(getSwitchToRegisterBtn());

    fireEvent.change(field("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(field("访问密钥（至少 6 位）"), {
      target: { value: "12345" },
    });
    fireEvent.change(field("再输一次访问密钥"), {
      target: { value: "12345" },
    });
    fireEvent.click(submitBtn("创建账号"));

    expect(screen.getByText("访问密钥至少 6 位")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("注册：两次密码不一致显示错误", () => {
    const { onAuthed } = renderAuth();
    fireEvent.click(getSwitchToRegisterBtn());

    fireEvent.change(field("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(field("访问密钥（至少 6 位）"), {
      target: { value: "123456" },
    });
    fireEvent.change(field("再输一次访问密钥"), {
      target: { value: "654321" },
    });
    fireEvent.click(submitBtn("创建账号"));

    expect(screen.getByText("两次输入的密钥不一致")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("注册成功（后端可达）：调 register 并触发 onAuthed", async () => {
    const { onAuthed } = renderAuth();
    vi.mocked(register).mockResolvedValue({
      id: "1", username: "alice", role: "admin",
    } as unknown as AuthUser);
    fireEvent.click(getSwitchToRegisterBtn());

    await fireEvent.change(field("用户名"), {
      target: { value: "alice" },
    });
    await fireEvent.change(field("访问密钥（至少 6 位）"), {
      target: { value: "123456" },
    });
    await fireEvent.change(field("再输一次访问密钥"), {
      target: { value: "123456" },
    });
    await fireEvent.click(submitBtn("创建账号"));

    expect(register).toHaveBeenCalledWith("alice", "123456");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });

  it("注册时后端不可达：fallback 到 setApiKey", async () => {
    const { onAuthed } = renderAuth();
    vi.mocked(register).mockRejectedValue(new Error("network down"));
    fireEvent.click(getSwitchToRegisterBtn());

    await fireEvent.change(field("用户名"), {
      target: { value: "bob" },
    });
    await fireEvent.change(field("访问密钥（至少 6 位）"), {
      target: { value: "abcdef" },
    });
    await fireEvent.change(field("再输一次访问密钥"), {
      target: { value: "abcdef" },
    });
    await fireEvent.click(submitBtn("创建账号"));

    expect(register).toHaveBeenCalled();
    // fallback 分支触发
    expect(setApiKey).toHaveBeenCalledWith("abcdef");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });

  it("登录：填用户名+密码并成功后触发 onAuthed", async () => {
    const { onAuthed } = renderAuth();
    vi.mocked(login).mockResolvedValue({
      id: "1", username: "admin", role: "admin",
    } as unknown as AuthUser);

    await fireEvent.change(field("用户名"), {
      target: { value: "admin" },
    });
    await fireEvent.change(field("密码", { exact: true }), {
      target: { value: "secretkey" },
    });
    await fireEvent.click(submitBtn(/^登录$/));

    expect(login).toHaveBeenCalledWith("admin", "secretkey");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });

  it("登录时后端不可达：fallback 到 setApiKey", async () => {
    const { onAuthed } = renderAuth();
    vi.mocked(login).mockRejectedValue(new Error("connection refused"));

    await fireEvent.change(field("用户名"), {
      target: { value: "admin" },
    });
    await fireEvent.change(field("密码", { exact: true }), {
      target: { value: "123456" },
    });
    await fireEvent.click(submitBtn(/^登录$/));

    expect(login).toHaveBeenCalled();
    expect(setApiKey).toHaveBeenCalledWith("123456");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });

  it("登录：空字段按钮 disabled，不触发任何调用", () => {
    const { onAuthed } = renderAuth();
    const submit = submitBtn(/^登录$/);
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(login).not.toHaveBeenCalled();
    expect(onAuthed).not.toHaveBeenCalled();
  });

  it("注册表单 Enter 键触发 submit", async () => {
    const { onAuthed } = renderAuth();
    vi.mocked(register).mockResolvedValue({
      id: "1", username: "bob", role: "viewer",
    } as unknown as AuthUser);
    fireEvent.click(getSwitchToRegisterBtn());

    await fireEvent.change(field("用户名"), {
      target: { value: "bob" },
    });
    await fireEvent.change(field("访问密钥（至少 6 位）"), {
      target: { value: "123456" },
    });
    const confirm = field("再输一次访问密钥");
    await fireEvent.change(confirm, { target: { value: "123456" } });
    await fireEvent.keyDown(confirm, { key: "Enter" });

    expect(register).toHaveBeenCalledWith("bob", "123456");
    expect(onAuthed).toHaveBeenCalledTimes(1);
  });
});
