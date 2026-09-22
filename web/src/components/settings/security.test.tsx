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

/**
 * SecurityPanel 的 Row 用 div 做标签（无 label-for / id 关联），
 * 无法用 getByLabelText 访问。按顺序取密码输入框。
 */
function getPasswordInputs(): {
  oldPwd: HTMLInputElement;
  newPwd: HTMLInputElement;
  confirm: HTMLInputElement;
} {
  const pw = document.querySelectorAll<HTMLInputElement>("input[type=password]");
  if (pw.length < 3) {
    throw new Error(`期望 3 个密码输入框，实际只有 ${pw.length} 个`);
  }
  return {
    oldPwd: pw[0],
    newPwd: pw[1],
    confirm: pw[2],
  };
}

describe("SecurityPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("改密：新密码与旧密码相同 → 显示错误", async () => {
    render(<SecurityPanel />);
    await screen.findByText("登录密码");

    const { oldPwd, newPwd, confirm } = getPasswordInputs();

    fireEvent.change(oldPwd, { target: { value: "SamePwd01" } });
    fireEvent.change(newPwd, { target: { value: "SamePwd01" } });
    fireEvent.change(confirm, { target: { value: "SamePwd01" } });

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

    const { newPwd, confirm } = getPasswordInputs();

    fireEvent.change(newPwd, { target: { value: "NewPass123" } });
    fireEvent.change(confirm, { target: { value: "Diff45678" } });

    await waitFor(() => {
      expect(screen.getByText("两次输入的新密码不一致")).toBeInTheDocument();
    });

    const updateBtn = screen.getByRole("button", { name: /更新密码/ });
    fireEvent.click(updateBtn);

    expect(changePassword).not.toHaveBeenCalled();
  });

  it("改密：合法输入 → 调用 changePassword 并显示成功", async () => {
    vi.mocked(changePassword).mockResolvedValue(1);
    render(<SecurityPanel />);

    await screen.findByText("登录密码");

    const { oldPwd, newPwd, confirm } = getPasswordInputs();

    fireEvent.change(oldPwd, { target: { value: "OldPwd01" } });
    fireEvent.change(newPwd, { target: { value: "NewPass123" } });
    fireEvent.change(confirm, { target: { value: "NewPass123" } });
    fireEvent.click(screen.getByRole("button", { name: /更新密码/ }));

    await waitFor(() => {
      expect(changePassword).toHaveBeenCalledWith("OldPwd01", "NewPass123");
      expect(screen.getByText(/密码已更新/)).toBeInTheDocument();
      expect(screen.getByText(/1 台设备已退出/)).toBeInTheDocument();
    });
  });
});
