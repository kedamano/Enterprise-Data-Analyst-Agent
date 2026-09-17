// ExperiencePanel 覆盖盲区：reduce-motion toggle 交互、服务信息展示
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
  display_name: "",
  email: "",
  phone: "",
  bio: "",
  role: "analyst",
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
    config: {
      user_auth_enabled: true,
      enforcement: false,
      registration_open: true,
      password_min_length: 8,
      has_users: true,
    },
    ready: true,
    busy: false,
    error: "",
  })),
  roleLabel: (r: string) => r,
}));

vi.mock("@/lib/api", () => ({
  fetchHealth: vi.fn(),
}));

import { ExperiencePanel } from "./ExperiencePanel";
import { fetchHealth } from "@/lib/api";

const sampleHealth: Awaited<ReturnType<typeof fetchHealth>> = {
  status: "ok",
  data_source: "sqlite:///./data/sample.db",
  llm_degraded: false,
  mock_llm: false,
  knowledge_enabled: true,
  data_sources: ["main"],
};

describe("ExperiencePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
    // 清理 data-reduce-motion 残留
    document.documentElement.removeAttribute("data-reduce-motion");
  });

  it("服务信息加载后显示状态行", async () => {
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);
    render(<ExperiencePanel onClearAll={vi.fn()} />);

    expect(await screen.findByText("本地 SQLite 文件")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
  });

  it("reduce-motion toggle：点击后切换状态", async () => {
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);
    render(<ExperiencePanel onClearAll={vi.fn()} />);

    const toggle = await screen.findByLabelText("减少界面动效");
    expect(toggle).not.toBeChecked();

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(toggle).toBeChecked();
      expect(
        document.documentElement.getAttribute("data-reduce-motion"),
      ).toBe("1");
    });
  });

  it("未开启鉴权 → 显示警告 Notice", async () => {
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);
    render(<ExperiencePanel onClearAll={vi.fn()} />);

    expect(
      await screen.findByText(/当前未开启登录校验/),
    ).toBeInTheDocument();
  });

  it("无法连接后端 → 显示错误 Notice", async () => {
    vi.mocked(fetchHealth).mockRejectedValue(new Error("net error"));
    render(<ExperiencePanel onClearAll={vi.fn()} />);

    expect(
      await screen.findByText(/无法连接后端服务/),
    ).toBeInTheDocument();
  });
});
