// TeamPanel 覆盖盲区：成员列表渲染、admin 赋权 selector 交互
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

// mock @/lib/user 使 useAuth 返回 admin 身份 + mock API
const adminUser = {
  id: "u-admin",
  username: "boss",
  display_name: "管理员",
  email: "boss@x.com",
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

const members = {
  users: [
    adminUser,
    {
      id: "u-2",
      username: "alice",
      display_name: "Alice",
      email: "",
      phone: "",
      bio: "",
      role: "viewer",
      status: "active",
      tenant: "default",
      avatar: "",
      avatar_version: 0,
      source: "local",
      created_at: "",
      updated_at: "",
      last_login_at: "",
    },
  ],
  total: 2,
};

vi.mock("@/lib/user", () => ({
  useAuth: vi.fn(() => ({ user: adminUser, ready: true, busy: false, error: "", config: null })),
  fetchUsers: vi.fn(),
  adminUpdateUser: vi.fn(),
  adminDeleteUser: vi.fn(),
  roleLabel: vi.fn((r: string) => ({
    viewer: "查看者",
    analyst: "分析师",
    analyst_lead: "分析主管",
    admin: "管理员",
  }[r] ?? r)),
}));

import { TeamPanel } from "./TeamPanel";
import { fetchUsers, adminUpdateUser } from "@/lib/user";

vi.mock("@/components/Avatar", () => ({
  Avatar: vi.fn(() => null),
}));

describe("TeamPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("加载成员后显示成员列表", async () => {
    vi.mocked(fetchUsers).mockResolvedValue(members);
    render(<TeamPanel />);

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(screen.getByText(/显示 2 \/ 2/)).toBeInTheDocument();
  });

  it("RoleSelect：admin 赋权 → 调用 adminUpdateUser", async () => {
    vi.mocked(fetchUsers).mockResolvedValue(members);
    vi.mocked(adminUpdateUser).mockResolvedValue({
      ...members.users[1],
      role: "analyst",
    });

    render(<TeamPanel />);

    await screen.findByText("Alice");

    // 第二个成员的 select（第一个是 admin 自身、disabled）
    const selects = screen.getAllByRole("combobox");
    // alice 的 select 是第二个（第一个是 admin 自己的）
    fireEvent.change(selects[1], { target: { value: "analyst" } });

    await waitFor(() => {
      expect(adminUpdateUser).toHaveBeenCalledWith("u-2", { role: "analyst" });
    });
  });

  it("搜索过滤：输入关键字后只显示匹配成员", async () => {
    vi.mocked(fetchUsers).mockResolvedValue(members);
    render(<TeamPanel />);

    await screen.findByText("Alice");

    const searchInput = screen.getByPlaceholderText("搜索用户名 / 昵称 / 邮箱");
    fireEvent.change(searchInput, { target: { value: "ALICE-NO-MATCH" } });

    await waitFor(() => {
      expect(screen.getByText("没有匹配的成员")).toBeInTheDocument();
    });
  });

  it("fetchUsers 返回 403 → 显示「仅管理员可管理成员」", async () => {
    const err: any = new Error("Forbidden");
    err.status = 403;
    vi.mocked(fetchUsers).mockRejectedValue(err);
    render(<TeamPanel />);

    expect(
      await screen.findByText("仅管理员可管理成员"),
    ).toBeInTheDocument();
  });
});
