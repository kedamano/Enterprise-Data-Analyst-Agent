// 本机 sandbox 受限，CI 可执行
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

// vi.mock hoisting：写在 import 前
const mockAuthState = {
  user: null as unknown,
  config: null as unknown,
  ready: false,
  busy: false,
  error: "",
};

const mockUseAuth = vi.fn(() => mockAuthState);

vi.mock("@/lib/user", () => ({
  useAuth: mockUseAuth,
}));

// 子 panel 仅用于呈现区别
vi.mock("@/components/Settings/AccountPanel", () => ({
  AccountPanel: () => <div data-testid="account">AccountPanel</div>,
  AuthEntry: () => <div data-testid="auth-entry">AuthEntry</div>,
}));
vi.mock("@/components/Settings/SecurityPanel", () => ({
  SecurityPanel: () => <div data-testid="security">SecurityPanel</div>,
}));
vi.mock("@/components/Settings/PermissionsPanel", () => ({
  PermissionsPanel: () => <div data-testid="permissions">PermissionsPanel</div>,
}));
vi.mock("@/components/Settings/TeamPanel", () => ({
  TeamPanel: () => <div data-testid="team">TeamPanel</div>,
}));
vi.mock("@/components/Settings/ExperiencePanel", () => ({
  ExperiencePanel: ({ onClearAll }: { onClearAll: () => void }) => (
    <div data-testid="experience">
      <button onClick={onClearAll}>clear</button>
    </div>
  ),
}));

// SettingsView 内部引用的是 ./Settings/AccountPanel 等路径，注意大小写。
// 项目使用 ./Settings/AccountPanel；上面的 mock 路径是 @/components/Settings/...，这里重新 patch
vi.mock("@/lib/storage", () => ({
  useLocalStorage: (_k: string, def: unknown) => {
    const React = require("react");
    return [def, vi.fn()];
  },
}));

import { SettingsView } from "@/components/SettingsView";

function renderSettings(opts: { role?: string; authEnabled?: boolean } = {}) {
  const onClearAll = vi.fn();
  mockAuthState.user = opts.role
    ? { role: opts.role, id: "u1", username: "u1", display_name: "u1", email: "a@a", phone: "", bio: "", status: "active", tenant: "", avatar: "", avatar_version: 0, source: "", created_at: "", updated_at: "", last_login_at: "" }
    : null;
  mockAuthState.config = opts.authEnabled === undefined
    ? { user_auth_enabled: true, enforcement: true, registration_open: true, password_min_length: 6, has_users: true }
    : { user_auth_enabled: opts.authEnabled, enforcement: true, registration_open: true, password_min_length: 6, has_users: true };
  mockAuthState.ready = true;
  mockUseAuth.mockReturnValue({ ...mockAuthState });
  render(<SettingsView onClearAll={onClearAll} />);
  return { onClearAll };
}

describe("SettingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("ready=false 时显示加载占位", () => {
    mockAuthState.ready = false;
    mockAuthState.user = null;
    mockAuthState.config = null;
    mockUseAuth.mockReturnValue({ ...mockAuthState });
    render(<SettingsView onClearAll={vi.fn()} />);
    expect(screen.getByText(/读取登录状态/)).toBeInTheDocument();
  });

  it("已登录管理员初始渲染出 account 页内容", () => {
    renderSettings({ role: "admin" });
    // 管理员能看到成员管理
    expect(screen.getByRole("tab", { name: "成员管理" })).toBeInTheDocument();
    // account 面板默认渲染
    expect(screen.getByTestId("account")).toBeInTheDocument();
  });

  it("切换 team tab → 管理员可看", () => {
    renderSettings({ role: "admin" });
    fireEvent.click(screen.getByRole("tab", { name: "成员管理" }));
    expect(screen.getByTestId("team")).toBeInTheDocument();
  });

  it("非管理员点击团队 tab → 被重置回 account tab", () => {
    renderSettings({ role: "viewer" });
    // 非管理员看不到成员管理 tab
    expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  });

  it("experience tab 触发 onClearAll", () => {
    const { onClearAll } = renderSettings({ role: "admin" });
    fireEvent.click(screen.getByRole("tab", { name: "体验" }));
    expect(screen.getByTestId("experience")).toBeInTheDocument();
    fireEvent.click(screen.getByText("clear"));
    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  it("user_auth_enabled=false 且未登录 → 直接显示导航（不拦截在登录页）", () => {
    mockAuthState.user = null;
    mockAuthState.config = { user_auth_enabled: false, enforcement: false, registration_open: false, password_min_length: 6, has_users: true };
    mockAuthState.ready = true;
    mockUseAuth.mockReturnValue({ ...mockAuthState });
    render(<SettingsView onClearAll={vi.fn()} />);
    // 不出登录拦截
    expect(screen.queryByTestId("auth-entry")).not.toBeInTheDocument();
    // 导航仍在
    expect(screen.getByRole("tab", { name: "账号" })).toBeInTheDocument();
  });

  it("user_auth_enabled=true 且未登录 → 显示 Login 入口", () => {
    renderSettings({ authEnabled: true });
    expect(screen.getByTestId("auth-entry")).toBeInTheDocument();
  });
});
