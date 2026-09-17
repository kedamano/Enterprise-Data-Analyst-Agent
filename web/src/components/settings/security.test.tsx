// SecurityPanel 覆盖盲区：会话列表渲染、改密 form 校验逻辑
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
  display_name: "Alice",
  email: "",
  phone: "",
  bio: "",
  role: "admin",
  status: "active",
  tenant: "default",
  avatar: "",
  avatar_version: 0,
  source: "local",
  created_at: "",
  updated_at: "",
  last_login_at: "",
};

vi.mock("@/lib/user", () => ({
  useAuth: vi.fn(() => ({
    user: mockUser,
    ready: true,
    busy: false,
    error: "",
    config: { password_min_length: 8 },
  })),
  logout: vi.fn(),
  changePassword: vi.fn(),
  fetchMySessions: vi.fn(),
  fetchMyLogins: vi.fn(),
  roleLabel: (r: string) => r,
}));

import { SecurityPanel } from "./SecurityPanel";
import { changePassword, fetchMySessions } from "@/lib/user";

describe("SecurityPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("改密：新密码与旧密码相同 → 显示错误", async () => {
    render(<SecurityPanel />);
    await screen.findByText("登录密码");

    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "SamePwd01" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "SamePwd01" },
    });

    await waitFor(() => {
      expect(screen.getByText("新密码不能与当前密码相同")).toBeInTheDocument();
    });
  });

  it("加载会话列表：显示设备 user-agent", async () => {
    vi.mocked(fetchMySessions).mockResolvedValue({
      sessions: [
        {
          created_at: "2026-09-15T00:00:00Z",
          expires_at: "2026-09-22T00:00:00Z",
          user_agent: "Chrome/120",
          ip: "127.0.0.1",
        },
      ],
    });
    render(<SecurityPanel />);

    expect(await screen.findByText("Chrome/120")).toBeInTheDocument();
  });

  it("改密：两次不一致 → 不调用 changePassword", async () => {
    render(<SecurityPanel />);

    await screen.findByText("登录密码");

    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "NewPass123" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "Diff45678" },
    });
    fireEvent.click(screen.getByRole("button", { name: /更新密码/ }));

    expect(changePassword).not.toHaveBeenCalled();
  });

  it("改密：合法输入 → 调用 changePassword 并显示成功", async () => {
    vi.mocked(changePassword).mockResolvedValue(1);
    render(<SecurityPanel />);

    await screen.findByText("登录密码");

    fireEvent.change(screen.getByLabelText("当前密码"), {
      target: { value: "OldPwd01" },
    });
    fireEvent.change(screen.getByLabelText("新密码"), {
      target: { value: "NewPass123" },
    });
    fireEvent.change(screen.getByLabelText("确认新密码"), {
      target: { value: "NewPass123" },
    });
    fireEvent.click(screen.getByRole("button", { name: /更新密码/ }));

    await waitFor(() => {
      expect(changePassword).toHaveBeenCalledWith("OldPwd01", "NewPass123");
      expect(screen.getByText(/密码已更新/)).toBeInTheDocument();
      expect(screen.getByText(/1 台设备已退出/)).toBeInTheDocument();
    });
  });
});
