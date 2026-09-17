import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Download,
  Eye,
  File,
  FileCode,
  FileSpreadsheet,
  FileText,
  Folder,
  FolderOpen,
  FolderPlus,
  HardDrive,
  Image as ImageIcon,
  Loader2,
  Pencil,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  createFsFolder,
  deleteFsNode,
  fetchFsList,
  fetchFsTree,
  fsDownloadUrl,
  fsRawUrl,
  previewFsFile,
  renameFsNode,
  searchFs,
  uploadFsFile,
} from "@/lib/api";
import type { FsPreviewResponse } from "@/lib/api";
import type { FsListResponse, FsNode } from "@/lib/api";
import { fmtBytes } from "@/lib/format";
import { PreviewPanel } from "@/components/PreviewPanel";

/** 按扩展名给文件配色，扫一眼就能区分数据类型。 */
function FileIcon({ name }: { name: string }) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["csv", "tsv", "xlsx", "xls"].includes(ext))
    return <FileSpreadsheet className="h-5 w-5 text-verified" />;
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext))
    return <ImageIcon className="h-5 w-5 text-brand" />;
  if (["json", "py", "js", "ts", "tsx", "sql", "md", "html", "css", "yml", "yaml"].includes(ext))
    return <FileCode className="h-5 w-5 text-brand" />;
  if (["txt", "pdf", "doc", "docx", "ppt", "pptx"].includes(ext))
    return <FileText className="h-5 w-5 text-danger" />;
  return <File className="h-5 w-5 text-ink-3" />;
}

/** 列表里的「类型」列：比单看文件名更容易扫读。 */
function typeLabel(node: FsNode): string {
  if (node.is_dir) return "文件夹";
  const ext = node.name.split(".").pop()?.toLowerCase() ?? "";
  if (!ext || ext === node.name.toLowerCase()) return "文件";
  return `${ext.toUpperCase()} 文件`;
}

export function FilesView() {
  const [tree, setTree] = useState<FsNode[]>([]);
  const [stats, setStats] = useState<{ folders?: number; files?: number; bytes?: number }>({});
  const [parentId, setParentId] = useState("");
  const [list, setList] = useState<FsListResponse | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const [q, setQ] = useState("");
  const [results, setResults] = useState<FsNode[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 预览弹窗
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<FsPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewAbort = useRef<AbortController | null>(null);

  const reloadTree = useCallback(async () => {
    try {
      const res = await fetchFsTree();
      setTree(res.nodes);
      setStats(res.stats ?? {});
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  }, []);

  const reloadList = useCallback(async (pid: string) => {
    setLoading(true);
    try {
      setList(await fetchFsList(pid));
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reloadTree();
  }, [reloadTree]);

  useEffect(() => {
    void reloadList(parentId);
  }, [parentId, reloadList]);

  // 进入目录时自动展开其祖先链，保证左树能看到当前位置
  useEffect(() => {
    if (!list) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      list.breadcrumb.forEach((b) => next.add(b.id));
      return next;
    });
  }, [list]);

  // 搜索防抖：每次按键都打后端会把文件树查穿
  useEffect(() => {
    const kw = q.trim();
    if (!kw) {
      setResults(null);
      return;
    }
    const timer = setTimeout(async () => {
      setSearching(true);
      try {
        const res = await searchFs(kw);
        setResults(res.results);
      } catch (e) {
        setToast({ kind: "err", text: (e as Error).message });
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [q]);

  const refresh = useCallback(
    async (pid = parentId) => {
      await Promise.all([reloadTree(), reloadList(pid)]);
    },
    [parentId, reloadList, reloadTree],
  );

  const doUpload = async (files: FileList | File[]) => {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    setBusy(true);
    setToast(null);
    try {
      for (const f of arr) await uploadFsFile(parentId, f);
      setToast({ kind: "ok", text: `已上传 ${arr.length} 个文件` });
      await refresh();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const submitFolder = async () => {
    const nm = newName.trim();
    if (!nm) {
      setCreating(false);
      return;
    }
    try {
      await createFsFolder(parentId, nm);
      setCreating(false);
      setNewName("");
      await refresh();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  };

  const submitRename = async () => {
    if (!renaming) return;
    const nm = renaming.value.trim();
    if (!nm) {
      setRenaming(null);
      return;
    }
    try {
      await renameFsNode(renaming.id, nm);
      setRenaming(null);
      await refresh();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  };

  const doDelete = async (node: FsNode) => {
    const isTree = node.is_dir;
    const msg = isTree
      ? `删除文件夹「${node.name}」及其中所有内容？此操作不可撤销。`
      : `删除文件「${node.name}」？此操作不可撤销。`;
    if (!window.confirm(msg)) return;
    try {
      await deleteFsNode(node.id);
      setToast({ kind: "ok", text: `已删除「${node.name}」` });
      if (results) setResults(results.filter((r) => r.id !== node.id));
      await refresh();
    } catch (e) {
      setToast({ kind: "err", text: (e as Error).message });
    }
  };

  // Esc 关闭预览：编辑器/预览器的标准快捷键，不打断数据
  useEffect(() => {
    if (!previewOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        previewAbort.current?.abort();
        setPreviewOpen(false);
        setPreviewLoading(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previewOpen]);

  const openPreview = (node: FsNode) => {
    previewAbort.current?.abort();
    const ac = new AbortController();
    previewAbort.current = ac;
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    setPreviewOpen(true);
    previewFsFile(node.id, ac.signal)
      .then((res) => {
        if (ac.signal.aborted) return;
        setPreview(res);
      })
      .catch((e) => {
        if (ac.signal.aborted) return;
        setPreviewError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!ac.signal.aborted) setPreviewLoading(false);
      });
  };

  const folders = useMemo(() => tree.filter((n) => n.is_dir), [tree]);

  const childrenOf = useCallback(
    (id: string) => folders.filter((f) => f.parent_id === id),
    [folders],
  );

  // 左树递归节点
  const renderNode = (node: FsNode, depth: number) => {
    const kids = childrenOf(node.id);
    const open = expanded.has(node.id);
    const selected = parentId === node.id;
    return (
      <div key={node.id}>
        <button
          onClick={() => setParentId(node.id)}
          className={`group flex w-full items-center gap-1.5 rounded-control py-1.5 pr-2 text-left text-small transition ${
            selected
              ? "bg-brand-soft font-medium text-brand"
              : "text-ink-2 hover:bg-canvas"
          }`}
          style={{ paddingLeft: 6 + depth * 13 }}
        >
          <span
            role="presentation"
            onClick={(e) => {
              e.stopPropagation();
              if (kids.length === 0) return;
              setExpanded((prev) => {
                const next = new Set(prev);
                if (next.has(node.id)) next.delete(node.id);
                else next.add(node.id);
                return next;
              });
            }}
            className="grid h-5 w-5 shrink-0 place-items-center text-ink-3"
          >
            {kids.length === 0 ? null : open ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )}
          </span>
          {open ? (
            <FolderOpen className="h-4 w-4 shrink-0 text-attention" />
          ) : (
            <Folder className="h-4 w-4 shrink-0 text-attention" />
          )}
          <span className="truncate">{node.name}</span>
        </button>
        {open && kids.map((k) => renderNode(k, depth + 1))}
      </div>
    );
  };

  const rows = results ?? list?.nodes ?? [];
  const rowsBytes = rows.reduce((sum, n) => sum + (n.bytes ?? 0), 0);

  return (
    <div className="flex h-full min-h-0 bg-canvas">
      {/* 左：目录树 */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-rule bg-white">
        <div className="flex h-14 items-center gap-2.5 border-b border-rule px-4">
          <HardDrive className="h-5 w-5 text-brand" />
          <span className="text-body font-semibold text-ink">文件库</span>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2.5">
          <button
            onClick={() => setParentId("")}
            className={`flex w-full items-center gap-2 rounded-control px-2 py-2 text-left text-small transition ${
              parentId === "" && !results
                ? "bg-brand-soft font-medium text-brand"
                : "text-ink-2 hover:bg-canvas"
            }`}
          >
            <FolderOpen className="h-4 w-4 text-attention" />
            全部文件
          </button>
          <div className="mt-0.5">
            {folders.filter((f) => f.parent_id === "").map((f) => renderNode(f, 1))}
          </div>
        </div>
        <div className="border-t border-rule px-4 py-3 text-small text-ink-3">
          {stats.folders ?? 0} 个文件夹 · {stats.files ?? 0} 个文件
          <span className="mt-0.5 block text-ink-3">{fmtBytes(stats.bytes ?? 0)} 已占用</span>
        </div>
      </aside>

      {/* 右：工具条 + 列表（预览打开时整块隐藏，让位给预览面板） */}
      {!previewOpen && (<section
        className="flex min-w-0 flex-1 flex-col"
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (e.dataTransfer.files?.length) void doUpload(e.dataTransfer.files);
        }}
      >
        <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule bg-white px-6">
          <button
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white shadow-sm transition hover:bg-brand-hover disabled:opacity-50"
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Upload className="h-4 w-4" />
            )}
            上传
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files) void doUpload(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            onClick={() => {
              setCreating(true);
              setNewName("");
            }}
            className="inline-flex items-center gap-2 rounded-control border border-rule bg-white px-3.5 py-2 text-small font-medium text-ink-2 shadow-sm transition hover:border-attention hover:text-attention"
          >
            <FolderPlus className="h-4 w-4" /> 新建文件夹
          </button>

          <div className="relative ml-auto">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索全部文件"
              aria-label="搜索全部文件"
              className="w-64 rounded-control border border-rule bg-white py-2 pl-9 pr-8 text-small outline-none transition placeholder:text-ink-3 focus:border-brand focus:ring-2 focus:ring-brand"
            />
            {q && (
              <button
                onClick={() => setQ("")}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink-3 hover:text-ink-2"
                aria-label="清空搜索"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>

        {/* 面包屑（搜索时显示结果范围） */}
        <div className="flex shrink-0 items-center gap-1 border-b border-rule bg-white px-6 py-2.5 text-small text-ink-3">
          {results ? (
            <span className="inline-flex items-center gap-2">
              <Search className="h-4 w-4" />
              搜索「{q.trim()}」· {rows.length} 个结果
              {searching && <Loader2 className="h-4 w-4 animate-spin" />}
            </span>
          ) : (
            <>
              <button
                onClick={() => setParentId("")}
                className="rounded px-1.5 py-0.5 transition hover:bg-canvas hover:text-ink"
              >
                全部文件
              </button>
              {(list?.breadcrumb ?? []).map((b) => (
                <span key={b.id} className="flex items-center gap-1">
                  <ChevronRight className="h-4 w-4 text-ink-3" />
                  <button
                    onClick={() => setParentId(b.id)}
                    className="rounded px-1.5 py-0.5 transition hover:bg-canvas hover:text-ink"
                  >
                    {b.name}
                  </button>
                </span>
              ))}
            </>
          )}
        </div>

        {toast && (
          <div
            className={`mx-6 mt-4 rounded-control px-4 py-2.5 text-small ${
              toast.kind === "ok"
                ? "border border-verified bg-verified-soft text-verified"
                : "border border-danger bg-danger-soft text-danger"
            }`}
          >
            {toast.text}
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="mx-auto w-full max-w-[1200px]">
            <div
              className={`overflow-hidden rounded-panel border bg-white transition ${
                dragOver ? "border-brand ring-2 ring-brand" : "border-rule"
              }`}
            >
              <table className="w-full text-left">
                <thead>
                  <tr className="border-b border-rule bg-canvas text-small font-medium text-ink-3">
                    <th className="px-5 py-3 font-medium">名称</th>
                    <th className="w-32 px-3 py-3 font-medium">类型</th>
                    <th className="w-24 px-3 py-3 font-medium">大小</th>
                    <th className="w-40 px-3 py-3 font-medium">修改时间</th>
                    <th className="w-44 px-5 py-3 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {creating && !results && (
                    <tr className="border-b border-rule bg-brand-soft">
                      <td className="px-5 py-3" colSpan={5}>
                        <div className="flex items-center gap-2.5">
                          <Folder className="h-5 w-5 text-attention" />
                          <input
                            autoFocus
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void submitFolder();
                              if (e.key === "Escape") setCreating(false);
                            }}
                            placeholder="文件夹名称"
                            aria-label="新文件夹名称"
                            className="w-64 rounded-control border border-rule-strong px-2.5 py-1.5 text-small outline-none focus:ring-2 focus:ring-brand"
                          />
                          <button
                            onClick={() => void submitFolder()}
                            className="grid h-7 w-7 place-items-center rounded-control text-verified hover:bg-verified-soft"
                            aria-label="确认新建"
                          >
                            <Check className="h-4 w-4" />
                          </button>
                          <button
                            onClick={() => setCreating(false)}
                            className="grid h-7 w-7 place-items-center rounded-control text-ink-3 hover:bg-canvas"
                            aria-label="取消新建"
                          >
                            <X className="h-4 w-4" />
                          </button>
                        </div>
                      </td>
                      <td />
                    </tr>
                  )}

                  {loading && !results ? (
                    <tr>
                      <td colSpan={5} className="px-4 py-8">
                        <div className="space-y-3" aria-hidden>
                          {Array.from({ length: 4 }).map((_, i) => (
                            <div key={i} className="flex items-center gap-3">
                              <div className="h-8 w-8 animate-pulse rounded-control bg-rule" />
                              <div className="h-3 flex-1 animate-pulse rounded-control bg-rule" />
                              <div className="h-3 w-20 animate-pulse rounded-control bg-rule" />
                              <div className="h-3 w-24 animate-pulse rounded-control bg-rule" />
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  ) : rows.length === 0 && !creating ? (
                    <tr>
                      <td colSpan={5} className="px-4 py-16 text-center">
                        <Folder className="mx-auto h-9 w-9 text-ink-3" />
                        <p className="mt-3 text-body font-medium text-ink-2">
                          {results ? "没有匹配的文件" : "这个文件夹是空的"}
                        </p>
                        <p className="mt-1.5 text-small text-ink-3">
                          拖拽文件到此处，或点上方「上传」按钮。
                        </p>
                      </td>
                    </tr>
                  ) : (
                    rows.map((node) => {
                      const isRenaming = renaming?.id === node.id;
                      return (
                        <tr
                          key={node.id}
                          className="group border-b border-rule last:border-0 hover:bg-canvas"
                        >
                          <td className="px-5 py-3">
                            <div className="flex items-center gap-2.5">
                              {node.is_dir ? (
                                <Folder className="h-5 w-5 shrink-0 text-attention" />
                              ) : (
                                <FileIcon name={node.name} />
                              )}
                              {isRenaming ? (
                                <span className="flex items-center gap-2">
                                  <input
                                    autoFocus
                                    value={renaming.value}
                                    onChange={(e) =>
                                      setRenaming({ id: node.id, value: e.target.value })
                                    }
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") void submitRename();
                                      if (e.key === "Escape") setRenaming(null);
                                    }}
                                    aria-label={`重命名 ${node.name}`}
                                    className="w-64 rounded-control border border-rule-strong px-2.5 py-1.5 text-small outline-none focus:ring-2 focus:ring-brand"
                                  />
                                  <button
                                    onClick={() => void submitRename()}
                                    className="grid h-7 w-7 place-items-center rounded-control text-verified hover:bg-verified-soft"
                                    aria-label="确认重命名"
                                  >
                                    <Check className="h-4 w-4" />
                                  </button>
                                  <button
                                    onClick={() => setRenaming(null)}
                                    className="grid h-7 w-7 place-items-center rounded-control text-ink-3 hover:bg-canvas"
                                    aria-label="取消重命名"
                                  >
                                    <X className="h-4 w-4" />
                                  </button>
                                </span>
                              ) : node.is_dir ? (
                                <button
                                  onClick={() => setParentId(node.id)}
                                  className="truncate text-left text-body font-medium text-ink-2 hover:text-brand"
                                >
                                  {node.name}
                                </button>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => openPreview(node)}
                                  className="truncate text-left text-body text-ink-2 transition hover:text-brand"
                                  title={`预览 ${node.name}`}
                                >
                                  {node.name}
                                </button>
                              )}
                              {results && node.path && (
                                <span className="ml-1.5 truncate text-small text-ink-3">
                                  {node.path}
                                </span>
                              )}
                            </div>
                          </td>
                          <td className="px-3 py-3">
                            <span className="rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
                              {typeLabel(node)}
                            </span>
                          </td>
                          <td className="px-3 py-3 text-small text-ink-3 tabular-nums">
                            {node.is_dir ? "—" : fmtBytes(node.bytes)}
                          </td>
                          <td className="px-3 py-3 text-small text-ink-3 tabular-nums">
                            {node.updated_at.slice(0, 16)}
                          </td>
                          <td className="px-5 py-3">
                            {/*
                              操作常驻可见（静默态用 40% 不透明度，行 hover / 键盘聚焦时提亮到 100%）。
                              此前是 opacity-0 + group-hover:opacity-100 —— 表头写着「操作」却看不到任何操作，
                              用户必须先把鼠标扫过整行才会发现功能存在，等于把功能藏起来了。
                            */}
                            <div className="flex items-center justify-end gap-0.5 opacity-40 transition group-hover:opacity-100 focus-within:opacity-100">
                              {!node.is_dir && (
                                <button
                                  onClick={() => openPreview(node)}
                                  className="grid h-7 w-7 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-brand"
                                  title="预览"
                                  aria-label={`预览 ${node.name}`}
                                >
                                  <Eye className="h-4 w-4" />
                                </button>
                              )}
                              {!node.is_dir && (
                                <a
                                  href={fsDownloadUrl(node.id)}
                                  download={node.name}
                                  className="grid h-7 w-7 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-brand"
                                  title="下载"
                                  aria-label={`下载 ${node.name}`}
                                >
                                  <Download className="h-4 w-4" />
                                </a>
                              )}
                              <button
                                onClick={() => setRenaming({ id: node.id, value: node.name })}
                                className="grid h-7 w-7 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink-2"
                                title="重命名"
                                aria-label={`重命名 ${node.name}`}
                              >
                                <Pencil className="h-4 w-4" />
                              </button>
                              <button
                                onClick={() => void doDelete(node)}
                                className="grid h-7 w-7 place-items-center rounded-control text-ink-3 transition hover:bg-danger-soft hover:text-danger"
                                title="删除"
                                aria-label={`删除 ${node.name}`}
                              >
                                <Trash2 className="h-4 w-4" />
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>

              {/* 列表页脚：给表格一个收口，避免内容少时下方出现"断崖式"空白 */}
              <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 border-t border-rule bg-canvas px-5 py-3 text-small text-ink-3">
                <span>
                  共 <span className="font-medium text-ink-2">{rows.length}</span> 项
                </span>
                <span>
                  总大小{" "}
                  <span className="font-medium text-ink-2 tabular-nums">
                    {fmtBytes(rowsBytes)}
                  </span>
                </span>
                <span className="text-ink-3">
                  {creating || renaming ? "编辑中…" : "拖拽文件到此处可直接上传"}
                </span>
              </div>
            </div>

            {results && results.length > 0 && (
              <p className="mt-3 text-small text-ink-3">
                提示：点文件夹名可进入所在目录（上方面包屑会同步）。
              </p>
            )}
          </div>
        </div>
      </section>)}

      {/* 内嵌预览面板：打开时占据整个右栏（非弹窗、非挤压） */}
      {previewOpen && (
        <div className="flex min-w-0 flex-1">
          <PreviewPanel
            open={previewOpen}
            onClose={() => {
              previewAbort.current?.abort();
              setPreviewOpen(false);
              setPreviewLoading(false);
            }}
            title={preview?.name ?? "正在加载…"}
            subtitle={preview?.mime ?? undefined}
            render={(preview?.render ?? "text") as any}
            loading={previewLoading}
            error={previewError}
            previewable={preview?.previewable ?? false}
            reason={preview?.reason ?? null}
            text={preview?.text ?? null}
            truncated={preview?.truncated ?? false}
            encoding={preview?.encoding ?? null}
            chars={preview?.chars ?? 0}
            downloadUrl={preview?.previewable ? fsDownloadUrl(preview.id) : null}
            rawUrl={preview?.previewable ? fsRawUrl(preview.id) : null}
          />
        </div>
      )}
    </div>
  );
}
