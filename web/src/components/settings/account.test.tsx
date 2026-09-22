// AccountPanel 覆盖盲区：主账号信息展示、登录入口、资料编辑交互
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

const mockUser = {
  id: "u-1",
  username: "alice",
  display_name: "Alice Wang",
  email: "alice@company.com",
  phone: "",
  bio: "数据分析师",
  role: "analyst",
  status: "active",
  tenant: "default",
  avatar: "",
  avatar_version: 0,
  source: "local",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  last_login_at: "2026-09-15T08:00:00Z",
};

const mockAuthState = {
  user: mockUser,
  config: {
    user_auth_enabled: true,
    enforcement: true,
    registration_open: true,
    password_min_length: 8,
    has_users: true,
  },
  ready: true,
  busy: false,
  error: "",
};

vi.mock("@/lib/user", () => ({
  useAuth: vi.fn(() => mockAuthState),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  updateProfile: vi.fn(),
  changePassword: vi.fn(),
  uploadAvatar: vi.fn(),
  removeAvatar: vi.fn(),
  roleLabel: vi.fn((r: string) => ({
    viewer: "查看者",
    analyst: "分析师",
    admin: "管理员",
  }[r] ?? r)),
  initials: vi.fn((u: { display_name?: string; username?: string } | null) => {
    if (!u) return "?";
    const name = u.display_name || u.username || "";
    return name.trim().slice(0, 1).toUpperCase() || "?";
  }),
}));

import { AccountPanel } from "./AccountPanel";
import { useAuth } from "@/lib/user";

describe("AccountPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
    // 重置为已登录的默认状态（clearAllMocks 会重置 mockReturnValue）
    mockAuthState.user = mockUser;
    mockAuthState.ready = true;
    mockAuthState.config = {
      user_auth_enabled: true,
      enforcement: true,
      registration_open: true,
      password_min_length: 8,
      has_users: true,
    };
    vi.mocked(useAuth).mockReturnValue({ ...mockAuthState });
  });

  it("展示主账号信息：用户名 / 角色", async () => {
    render(<AccountPanel />);

    // 等「账号信息」分区渲染
    expect(await screen.findByText("账号信息")).toBeInTheDocument();
    // 角色标签
    expect(screen.getByText("分析师")).toBeInTheDocument();
    // 用户名 "alice" 出现在 Row 中
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("展示租户与时间信息", async () => {
    render(<AccountPanel />);
    await screen.findByText("账号信息");

    // 租户值
    expect(screen.getByText("default")).toBeInTheDocument();
    // 至少一个 2026 年份（注册时间、更新时间、最近登录时间）
    expect(screen.getAllByText(/2026/).length).toBeGreaterThan(0);
  });

  it("未登录时显示 AuthEntry 登录入口", () => {
    mockAuthState.user = null;
    vi.mocked(useAuth).mockReturnValue({ ...mockAuthState, user: null });

    render(<AccountPanel />);
    // AuthEntry 含「登录」标题 + 「还没有账号」引导文案
    expect(screen.getByText("还没有账号？")).toBeInTheDocument();
  });

  it("有未保存修改时显示保存与撤销按钮", async () => {
    render(<AccountPanel />);
    // 等资料编辑 render
    const displayNameInput = await screen.findByDisplayValue("Alice Wang");

    fireEvent.change(displayNameInput, { target: { value: "Alice Updated" } });

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /保存修改/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /撤销/ })).toBeInTheDocument();
    });
  });
});
