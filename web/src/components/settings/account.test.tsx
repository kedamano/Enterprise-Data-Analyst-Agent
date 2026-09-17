// AccountPanel 覆盖盲区：主账号信息展示、改密 form 校验
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

// AccountPanel 依赖 @/lib/user 的 useAuth 全局单态；模拟出登录态。
vi.mock("@/lib/user", () => {
  const user = {
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
  return {
    useAuth: vi.fn(() => ({
      user,
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
    })),
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
    // Avatar 会用到；漏掉它会让整块渲染抛 "No 'initials' export"
    initials: vi.fn((u: { display_name?: string; username?: string } | null) => {
      if (!u) return "?";
      const name = u.display_name || u.username || "";
      return name.trim().slice(0, 1).toUpperCase() || "?";
    }),
  };
});

import { AccountPanel } from "./AccountPanel";
import { changePassword } from "@/lib/user";

describe("AccountPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("展示主账号信息：用户名 / 角色 / 注册时间", async () => {
    render(<AccountPanel />);

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("分析师")).toBeInTheDocument();
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("改密 form：字段为空时按钮 disabled", async () => {
    render(<AccountPanel />);

    const updateBtn = await screen.findByRole("button", { name: /更新密码/ });
    expect(updateBtn).toBeDisabled();
  });

  it("改密 form：两次新密码不一致 → 显示错误 Notice", async () => {
    render(<AccountPanel />);

    const inputs = await screen.findAllByLabelText(/^新密码/);
    fireEvent.change(inputs[0], { target: { value: "NewPass123" } });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "Different456" },
    });

    await waitFor(() => {
      expect(screen.getByText("两次输入的新密码不一致")).toBeInTheDocument();
    });
  });

  it("改密 form：合法输入 → 调 changePassword 并显示成功", async () => {
    vi.mocked(changePassword).mockResolvedValue(0);
    render(<AccountPanel />);

    await screen.findByText("alice");
    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "OldPass1" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "NewPass123" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "NewPass123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /更新密码/ }));

    await waitFor(() => {
      expect(changePassword).toHaveBeenCalledWith("OldPass1", "NewPass123");
      expect(screen.getByText(/密码已更新/)).toBeInTheDocument();
    });
  });
});
