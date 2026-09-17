// 数据源面板覆盖盲区：数据源卡片渲染、点击测试连接 → 状态反馈
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { DataSourcesView } from "./DataSourcesView";

vi.mock("@/lib/api", () => ({
  fetchDataSources: vi.fn(),
  fetchHealth: vi.fn(),
  testDataSource: vi.fn(),
  createDataSource: vi.fn(),
  deleteDataSource: vi.fn(),
}));

import {
  fetchDataSources,
  fetchHealth,
  testDataSource,
} from "@/lib/api";

const sampleSources: Awaited<ReturnType<typeof fetchDataSources>> = {
  sources: [
    {
      name: "biz_mysql",
      dialect: "mysql",
      readonly: true,
      origin: "local",
    },
  ],
};

const sampleHealth: Awaited<ReturnType<typeof fetchHealth>> = {
  status: "ok",
  data_source: "mysql://localhost/biz",
  llm_degraded: false,
  mock_llm: false,
  knowledge_enabled: true,
  data_sources: ["biz_mysql"],
};

describe("DataSourcesView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("数据源卡片：显示连接名与类型", async () => {
    vi.mocked(fetchDataSources).mockResolvedValue(sampleSources);
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);

    render(<DataSourcesView />);

    expect(await screen.findByText("biz_mysql")).toBeInTheDocument();
    expect(screen.getByText("MySQL")).toBeInTheDocument();
    expect(screen.getByText(/只读/)).toBeInTheDocument();
  });

  it("空数据源 → 显示「还没有数据源」", async () => {
    vi.mocked(fetchDataSources).mockResolvedValue({ sources: [] });
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);

    render(<DataSourcesView />);

    expect(await screen.findByText("还没有数据源")).toBeInTheDocument();
    expect(screen.getByText(/点击右上角「新建连接」/)).toBeInTheDocument();
  });

  it("点击「新建连接」→ 打开弹窗并渲染表单", async () => {
    vi.mocked(fetchDataSources).mockResolvedValue(sampleSources);
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);

    render(<DataSourcesView />);

    await screen.findByText("biz_mysql");

    fireEvent.click(screen.getByRole("button", { name: /新建连接/ }));

    expect(
      await screen.findByRole("dialog", { name: "新建连接" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("连接名称")).toBeInTheDocument();
  });

  it("测试连接：输入表单 → 点击测试 → 显示成功反馈", async () => {
    vi.mocked(fetchDataSources).mockResolvedValue(sampleSources);
    vi.mocked(fetchHealth).mockResolvedValue(sampleHealth);
    vi.mocked(testDataSource).mockResolvedValue({ ok: true });

    render(<DataSourcesView />);

    await screen.findByText("biz_mysql");
    fireEvent.click(screen.getByRole("button", { name: /新建连接/ }));
    await screen.findByRole("dialog", { name: "新建连接" });

    // 填表
    const nameInput = screen.getByLabelText("连接名称");
    fireEvent.change(nameInput, { target: { value: "test_db" } });
    fireEvent.change(screen.getByLabelText("数据库名"), {
      target: { value: "testdb" },
    });

    fireEvent.click(screen.getByRole("button", { name: /测试连接/ }));

    expect(
      await screen.findByText(/连接成功/),
    ).toBeInTheDocument();
  });
});
