// 文件库面板覆盖盲区：加载态 / 空态、文件列表渲染、上传触发、预览入口
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { FilesView } from "./FilesView";

vi.mock("@/lib/api", () => ({
  fetchFsTree: vi.fn(),
  fetchFsList: vi.fn(),
  uploadFsFile: vi.fn(),
  createFsFolder: vi.fn(),
  deleteFsNode: vi.fn(),
  renameFsNode: vi.fn(),
  searchFs: vi.fn(),
  previewFsFile: vi.fn(),
  fsDownloadUrl: vi.fn(),
  fsRawUrl: vi.fn(),
}));

import {
  fetchFsTree,
  fetchFsList,
  previewFsFile,
} from "@/lib/api";

vi.mock("@/components/PreviewPanel", () => ({
  PreviewPanel: vi.fn(() => null),
}));

const emptyTree = { nodes: [], stats: { folders: 0, files: 0, bytes: 0 } };
const emptyList = { nodes: [], breadcrumb: [] };
const sampleList = {
  nodes: [
    {
      id: "f-1",
      name: "orders.csv",
      is_dir: false,
      parent_id: "",
      bytes: 2048,
      updated_at: "2026-09-15T10:00:00Z",
      path: "/orders.csv",
    },
  ],
  breadcrumb: [],
};

describe("FilesView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("空态：提示「这个文件夹是空的」", async () => {
    vi.mocked(fetchFsTree).mockResolvedValue(emptyTree);
    vi.mocked(fetchFsList).mockResolvedValue(emptyList);

    render(<FilesView />);

    expect(
      await screen.findByText("这个文件夹是空的"),
    ).toBeInTheDocument();
    // 拖拽提示可能出现在空态 <td> 和页脚两处，用 getAllByText
    const dragHints = screen.getAllByText(/拖拽文件到此处/);
    expect(dragHints.length).toBeGreaterThan(0);
  });

  it("文件列表渲染：显示文件名与大小", async () => {
    vi.mocked(fetchFsTree).mockResolvedValue(emptyTree);
    vi.mocked(fetchFsList).mockResolvedValue(sampleList);

    render(<FilesView />);

    expect(await screen.findByText("orders.csv")).toBeInTheDocument();
    // 大小既在行 <td> 又在页脚摘要出现，用 getAllByText 取所有匹配
    const sizes = screen.getAllByText("2.0 KB");
    expect(sizes.length).toBeGreaterThan(0);
  });

  it("点击上传按钮触发 file input", async () => {
    vi.mocked(fetchFsTree).mockResolvedValue(emptyTree);
    vi.mocked(fetchFsList).mockResolvedValue(emptyList);

    render(<FilesView />);

    await screen.findByText("这个文件夹是空的");

    const uploadBtn = screen.getByRole("button", { name: /^上传/ });
    expect(uploadBtn).toBeInTheDocument();
  });

  it("点击文件名 → 打开预览面板", async () => {
    vi.mocked(fetchFsTree).mockResolvedValue(emptyTree);
    vi.mocked(fetchFsList).mockResolvedValue(sampleList);
    vi.mocked(previewFsFile).mockResolvedValue({
      id: "f-1",
      name: "orders.csv",
      mime: "text/csv",
      render: "text",
      text: "id,name\n1,foo",
      chars: 12,
      truncated: false,
      previewable: true,
      reason: null,
      encoding: "utf-8",
    });

    render(<FilesView />);

    const fileBtn = await screen.findByText("orders.csv");
    fireEvent.click(fileBtn);

    await waitFor(() => {
      expect(previewFsFile).toHaveBeenCalledWith("f-1", expect.any(Object));
    });
  });
});
