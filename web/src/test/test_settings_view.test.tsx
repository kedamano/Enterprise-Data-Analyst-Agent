// 本机 sandbox 受限，CI 可执行
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

// vi.mock 工厂内禁止引用外部变量，用工厂函数替代。
function makeAuthState() {
  return {
    user: null as unknown,
    config: null as unknown,
    ready: false,
    busy: false,
    error: "",
  };
}

vi.mock("@/lib/user", () => ({
  useAuth: vi.fn(() => makeAuthState()),
  initials: () => "?",
  roleLabel: (r: string) => r,
}));

// 子 panel 仅用于呈现区别。
// 源码用 ./settings/...（小写），mock 路径必须与之一致。
vi.mock("@/components/settings/AccountPanel", () => ({
  AccountPanel: () => <div data-testid="account">AccountPanel</div>,
  AuthEntry: () => <div data-testid="auth-entry">AuthEntry</div>,
}));
vi.mock("@/components/settings/SecurityPanel", () => ({
  SecurityPanel: () => <div data-testid="security">SecurityPanel</div>,
}));
vi.mock("@/components/settings/PermissionsPanel", () => ({
  PermissionsPanel: () => <div data-testid="permissions">PermissionsPanel</div>,
}));
vi.mock("@/components/settings/TeamPanel", () => ({
  TeamPanel: () => <div data-testid="team">TeamPanel</div>,
}));
vi.mock("@/components/settings/ExperiencePanel", () => ({
  ExperiencePanel: ({ onClearAll }: { onClearAll: () => void }) => (
    <div data-testid="experience">
      <button onClick={onClearAll}>clear</button>
    </div>
  ),
}));

// SettingsView 还引用 ./Settings/common（Loading），一并 mock。
vi.mock("@/components/settings/common", () => ({
  Loading: ({ what }: { what?: string }) => <div>{what}</div>,
  Section: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("@/lib/storage", () => ({
  useLocalStorage: (_k: string, def: unknown) => [def, vi.fn()],
}));

import { SettingsView } from "@/components/SettingsView";
import { useAuth } from "@/lib/user";

function makeUser(role: string) {
  return {
    id: "u1",
    username: "u1",
    display_name: "u1",
    email: "a@a",
    phone: "",
    bio: "",
    role,
    status: "active",
    tenant: "",
    avatar: "",
    avatar_version: 0,
    source: "",
    created_at: "",
    updated_at: "",
    last_login_at: "",
  };
}

function makeConfig(opts: { user_auth_enabled?: boolean } = {}) {
  return {
    user_auth_enabled: opts.user_auth_enabled ?? true,
    enforcement: true,
    registration_open: true,
    password_min_length: 6,
    has_users: true,
  };
}

function renderSettings(opts: { role?: string; authEnabled?: boolean } = {}) {
  const onClearAll = vi.fn();
  const user = opts.role ? makeUser(opts.role) : null;
  const config =
    opts.authEnabled === undefined
      ? makeConfig()
      : makeConfig({ user_auth_enabled: opts.authEnabled });
  vi.mocked(useAuth).mockReturnValue({
    user,
    config,
    ready: true,
    busy: false,
    error: "",
  });
  render(<SettingsView onClearAll={onClearAll} />);
  return { onClearAll };
}

describe("SettingsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("ready=false 时显示加载占位", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      config: null,
      ready: false,
      busy: false,
      error: "",
    });
    render(<SettingsView onClearAll={vi.fn()} />);
    expect(screen.getByText(/读取登录状态/)).toBeInTheDocument();
  });

  it("已登录管理员初始渲染出 account 页内容", () => {
    renderSettings({ role: "admin" });
    expect(screen.getByRole("tab", { name: "成员管理" })).toBeInTheDocument();
    expect(screen.getByTestId("account")).toBeInTheDocument();
  });

  it("切换 team tab → 管理员可看", () => {
    renderSettings({ role: "admin" });
    fireEvent.click(screen.getByRole("tab", { name: "成员管理" }));
    expect(screen.getByTestId("team")).toBeInTheDocument();
  });

  it("非管理员看不到成员管理 tab", () => {
    renderSettings({ role: "viewer" });
    expect(screen.queryByRole("tab", { name: "成员管理" })).not.toBeInTheDocument();
  });

  it("experience tab 触发 onClearAll", () => {
    const { onClearAll } = renderSettings({ role: "admin" });
    fireEvent.click(screen.getByRole("tab", { name: "体验" }));
    expect(screen.getByTestId("experience")).toBeInTheDocument();
    fireEvent.click(screen.getByText("clear"));
    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  it("user_auth_enabled=false 且未登录 → 直接显示导航", () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      config: {
        user_auth_enabled: false,
        enforcement: false,
        registration_open: false,
        password_min_length: 6,
        has_users: true,
      },
      ready: true,
      busy: false,
      error: "",
    });
    render(<SettingsView onClearAll={vi.fn()} />);
    expect(screen.queryByTestId("auth-entry")).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "账号" })).toBeInTheDocument();
  });

  it("user_auth_enabled=true 且未登录 → 显示 Login 入口", () => {
    renderSettings({ authEnabled: true });
    expect(screen.getByTestId("auth-entry")).toBeInTheDocument();
  });
});
