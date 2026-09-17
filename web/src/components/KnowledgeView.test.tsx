// 知识库面板覆盖盲区：空态引导、列表渲染 → 详情跳转、搜索过滤
// CI 走国际出口可执行；本机 sandbox 可能无法 run（依赖 IBMIcons / motion）。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { KnowledgeView } from "./KnowledgeView";

vi.mock("@/lib/api", () => ({
  fetchKbBases: vi.fn(),
  createKbBase: vi.fn(),
  fetchKbDocuments: vi.fn(),
  searchKb: vi.fn(),
  uploadKbDocument: vi.fn(),
  addKbText: vi.fn(),
  addKbWebsite: vi.fn(),
  deleteKbDocument: vi.fn(),
  deleteKbBase: vi.fn(),
  updateKbBase: vi.fn(),
  previewKbDocument: vi.fn(),
}));

import {
  fetchKbBases,
  createKbBase,
  searchKb,
} from "@/lib/api";

import { PreviewPanel } from "@/components/PreviewPanel";
vi.mock("@/components/PreviewPanel", () => ({
  PreviewPanel: vi.fn(() => null),
}));

function sampleBases() {
  return {
    bases: [
      {
        id: "kb-1",
        name: "人事制度库",
        description: "收录公司人事相关制度",
        kb_type: "general",
        documents: 5,
        chunks: 42,
        visibility: "private",
        updated_at: "2026-09-15T10:00:00Z",
        created_at: "2026-09-01T00:00:00Z",
      },
    ],
    total_chunks: 42,
  };
}

describe("KnowledgeView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it("空列表显示引导文案与新建按钮", async () => {
    vi.mocked(fetchKbBases).mockResolvedValue({ bases: [], total_chunks: 0 });
    render(<KnowledgeView />);

    expect(screen.getByText("还没有知识库")).toBeInTheDocument();
    expect(
      screen.getByText(/新建知识库后，可以上传文件/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /新建知识库/ }),
    ).toBeInTheDocument();
  });

  it("加载后渲染知识库卡片（名称 + 文档数）", async () => {
    vi.mocked(fetchKbBases).mockResolvedValue(sampleBases());
    render(<KnowledgeView />);

    const card = await screen.findByText("人事制度库");
    expect(card).toBeInTheDocument();
    expect(screen.getByText(/5 文档/)).toBeInTheDocument();
  });

  it("点击新建按钮 → 打开 CreateKbModal 并渲染表单", async () => {
    vi.mocked(fetchKbBases).mockResolvedValue(sampleBases());
    vi.mocked(createKbBase).mockResolvedValue({
      id: "kb-new",
      name: "新库",
      description: "",
      kb_type: "general",
      documents: 0,
      chunks: 0,
      visibility: "private",
      updated_at: "2026-09-20T00:00:00Z",
      created_at: "2026-09-20T00:00:00Z",
    });
    render(<KnowledgeView />);

    fireEvent.click(await screen.findByRole("button", { name: /^\s*新建$/ }));

    expect(await screen.findByText("新建知识库")).toBeInTheDocument();
    expect(screen.getByLabelText("名称")).toBeInTheDocument();
  });

  it("搜索框输入后过滤卡片列表", async () => {
    vi.mocked(fetchKbBases).mockResolvedValue({
      bases: [
        ...sampleBases().bases,
        {
          id: "kb-2",
          name: "指标口径",
          description: "数据指标",
          kb_type: "general",
          documents: 2,
          chunks: 10,
          visibility: "private",
          updated_at: "2026-09-14T10:00:00Z",
          created_at: "2026-09-02T00:00:00Z",
        },
      ],
      total_chunks: 52,
    });
    render(<KnowledgeView />);

    await screen.findByText("人事制度库");

    const searchInput = screen.getByPlaceholderText("知识库名称");
    fireEvent.change(searchInput, { target: { value: "指标" } });

    await waitFor(() => {
      expect(screen.queryByText("人事制度库")).not.toBeInTheDocument();
      expect(screen.getByText("指标口径")).toBeInTheDocument();
    });
  });
});
