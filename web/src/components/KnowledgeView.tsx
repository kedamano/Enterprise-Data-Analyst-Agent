import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  BookOpen,
  ChevronLeft,
  Clock,
  Eye,
  FileText,
  Globe,
  Link2,
  Loader2,
  Lock,
  Pencil,
  Plus,
  Search,
  Trash2,
  Upload,
} from "@/components/icons";
import { Modal, ModalBody, ModalContent } from "@/components/ui/animated-modal";
import {
  addKbText,
  addKbWebsite,
  createKbBase,
  deleteKbBase,
  deleteKbDocument,
  fetchKbBases,
  fetchKbDocuments,
  previewKbDocument,
  searchKb,
  updateKbBase,
  uploadKbDocument,
} from "@/lib/api";
import type { KbPreviewResponse } from "@/lib/api";
import { PreviewPanel } from "@/components/PreviewPanel";
import { SkeletonList } from "@/components/ui/skeleton";
import type {
  KbBase,
  KbBaseListResponse,
  KbDocument,
  KBHit,
} from "@/lib/api";

const KB_TYPE_LABEL: Record<string, string> = {
  general: "通用知识库",
  website: "网站知识库",
};

const DOC_TYPE_LABEL: Record<string, string> = {
  file: "文件",
  text: "文本",
  website: "网页",
};

function fmtBytes(n: number): string {
  if (!n) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return i === 0 ? `${v} B` : `${v.toFixed(1)} ${units[i]}`;
}

// ---------------------------------------------------------------- 知识库列表

export function KnowledgeView() {
  const [data, setData] = useState<KbBaseListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [sort, setSort] = useState<"updated" | "name" | "chunks">("updated");
  const [openId, setOpenId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    try {
      setData(await fetchKbBases());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const list = useMemo(() => {
    let out = data?.bases ?? [];
    const kw = q.trim().toLowerCase();
    if (kw) {
      out = out.filter(
        (b) =>
          b.name.toLowerCase().includes(kw) ||
          b.description.toLowerCase().includes(kw),
      );
    }
    if (typeFilter !== "all") out = out.filter((b) => b.kb_type === typeFilter);
    const sorted = [...out];
    if (sort === "name") sorted.sort((a, b) => a.name.localeCompare(b.name, "zh"));
    else if (sort === "chunks") sorted.sort((a, b) => b.chunks - a.chunks);
    else sorted.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
    return sorted;
  }, [data, q, typeFilter, sort]);

  if (openId) {
    return (
      <KbDetail
        kbId={openId}
        onBack={() => {
          setOpenId(null);
          void reload();
        }}
      />
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      {/* 工具条：搜索 + 类型 + 排序 + 新建（对齐参考图的信息层级） */}
      <div className="flex flex-wrap items-center gap-2.5 border-b border-rule bg-white px-6 py-3">
        <div className="mr-auto flex items-center gap-2.5">
          <BookOpen className="h-5 w-5 text-attention" />
          <h1 className="text-heading font-semibold text-ink">我的知识库</h1>
          {/* 只写对使用者有用的两项。原来的「· sqlite」是存储后端名，
              那是运维排查信息，不该出现在知识库页头。 */}
          {data && (
            <span className="ml-1 rounded-full bg-canvas px-2.5 py-1 text-small text-ink-3">
              {data.bases.length} 个知识库 · {data.total_chunks} 个分块
            </span>
          )}
        </div>

        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="知识库名称"
            aria-label="搜索知识库"
            className="w-52 rounded-control border border-rule bg-white py-2 pl-9 pr-3 text-small text-ink-2 outline-none transition placeholder:text-ink-3 focus:border-brand focus:ring-2 focus:ring-brand"
          />
        </div>

        {/* 筛选用 aria-label 而不是把「类型 / 排序」塞进 option 文本——
            屏幕阅读器读出来的是「类型 全部 / 通用知识库」，重复且无结构。 */}
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          aria-label="按类型筛选知识库"
          className="rounded-control border border-rule bg-white px-3 py-2 text-small text-ink-2 outline-none focus:border-brand"
        >
          <option value="all">全部类型</option>
          <option value="general">通用知识库</option>
          <option value="website">网站知识库</option>
        </select>

        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as "updated" | "name" | "chunks")}
          aria-label="知识库排序方式"
          className="rounded-control border border-rule bg-white px-3 py-2 text-small text-ink-2 outline-none focus:border-brand"
        >
          <option value="updated">最近修改</option>
          <option value="name">按名称</option>
          <option value="chunks">按分块数</option>
        </select>

        <button
          onClick={() => setCreateOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white shadow-sm transition hover:bg-brand-hover"
        >
          <Plus className="h-4 w-4" /> 新建
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto w-full max-w-[1400px]">
        {loading && <SkeletonList rows={5} />}
        {error && (
          <div className="rounded-panel border border-danger bg-danger-soft px-4 py-3 text-small text-danger">
            加载失败：{error}
          </div>
        )}
        {!loading && !error && list.length === 0 && (
          <div className="rounded-panel border border-dashed border-rule-strong bg-white px-6 py-14 text-center">
            <BookOpen className="mx-auto h-8 w-8 text-ink-3" />
            <p className="mt-3 text-small font-medium text-ink-2">
              {q || typeFilter !== "all" ? "没有匹配的知识库" : "还没有知识库"}
            </p>
            <p className="mt-1 text-small text-ink-3">
              新建知识库后，可以上传文件、粘贴文本或导入网页，供智能体检索取证。
            </p>
            <button
              onClick={() => setCreateOpen(true)}
              className="mt-4 inline-flex items-center gap-1.5 rounded-control bg-brand px-3 py-1.5 text-small font-medium text-white transition hover:bg-brand-hover"
            >
              <Plus className="h-4 w-4" /> 新建知识库
            </button>
          </div>
        )}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {list.map((b) => (
            <KbCard key={b.id} base={b} onOpen={() => setOpenId(b.id)} />
          ))}
        </div>
        </div>
      </div>

      <CreateKbModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false);
          void reload();
        }}
      />
    </div>
  );
}

function KbCard({ base, onOpen }: { base: KbBase; onOpen: () => void }) {
  const isWeb = base.kb_type === "website";
  return (
    <button
      onClick={onOpen}
      className="group flex flex-col rounded-panel border border-rule bg-white p-5 text-left transition-colors hover:border-brand"
    >
      <div className="flex items-start gap-3.5">
        {/* 图标底衬统一用品牌色浅底：知识库类型不是"状态"，不占用语义色。 */}
        <span className="grid h-11 w-11 shrink-0 place-items-center rounded-panel bg-brand-soft text-brand">
          {isWeb ? <Globe className="h-5 w-5" /> : <BookOpen className="h-5 w-5" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-body font-semibold text-ink group-hover:text-brand">
              {base.name}
            </span>
            <Lock className="h-4 w-4 shrink-0 text-ink-3" />
          </div>
          <span className="mt-1 inline-block rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
            {KB_TYPE_LABEL[base.kb_type] ?? base.kb_type}
          </span>
        </div>
      </div>

      {/* 空描述用与其余文案一致的书面语气。原来的「~」是全站唯一的波浪号。 */}
      <p className="mt-3.5 line-clamp-2 min-h-[42px] text-small leading-relaxed text-ink-3">
        {base.description || "暂无说明"}
      </p>

      {/* 卡片元信息只保留两个真正影响"这个库值不值得点开"的字段：
          文档数（有没有内容）与最近更新（新不新）。
          创建者与分块数属于详情页信息，堆在卡片上只会变成视觉噪点。 */}
      <div className="mt-3.5 flex flex-wrap items-center gap-x-3.5 gap-y-1 border-t border-rule pt-3 text-small text-ink-3">
        <span className="inline-flex items-center gap-1.5">
          <FileText className="h-4 w-4" /> {base.documents} 文档
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5">
          <Clock className="h-4 w-4" />
          更新于 {base.updated_at.slice(5, 10)}
        </span>
      </div>
    </button>
  );
}

// ---------------------------------------------------------------- 新建知识库

function CreateKbModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [kbType, setKbType] = useState("general");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setKbType("general");
      setError(null);
    }
  }, [open]);

  const submit = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createKbBase({ name: name.trim(), description, kb_type: kbType });
      onCreated();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose}>
      <ModalBody className="max-w-md">
        <ModalContent>
          <h2 className="text-heading font-semibold text-ink">新建知识库</h2>
          <p className="mt-1 text-small text-ink-3">
            知识库是智能体检索取证的依据来源，可以装文件、文本和网页。
          </p>

          {/* label 与控件必须显式关联（htmlFor + id）。此前 <label> 只是浮在
              输入框上方的文字，点击它不会聚焦，屏幕阅读器也读不出对应关系。 */}
          <label
            htmlFor="kb-create-name"
            className="mt-4 block text-small font-medium text-ink-2"
          >
            名称 <span className="text-danger">*</span>
          </label>
          <input
            id="kb-create-name"
            value={name}
            autoFocus
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
            placeholder="如：人事制度、指标口径"
            className="mt-1.5 w-full rounded-control border border-rule px-3 py-2 text-small outline-none transition focus:border-brand focus:ring-2 focus:ring-brand"
          />

          <label
            htmlFor="kb-create-desc"
            className="mt-3.5 block text-small font-medium text-ink-2"
          >
            描述
          </label>
          <textarea
            id="kb-create-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            placeholder="这个知识库收录什么内容（可选）"
            className="mt-1.5 w-full resize-none rounded-control border border-rule px-3 py-2 text-small outline-none transition focus:border-brand focus:ring-2 focus:ring-brand"
          />

          <label className="mt-3.5 block text-small font-medium text-ink-2">
            类型
          </label>
          <div className="mt-1.5 grid grid-cols-2 gap-2">
            {[
              { v: "general", label: "通用知识库", icon: BookOpen, hint: "文件 / 文本" },
              { v: "website", label: "网站知识库", icon: Globe, hint: "导入网页" },
            ].map(({ v, label, icon: Icon, hint }) => (
              <button
                key={v}
                type="button"
                onClick={() => setKbType(v)}
                className={`flex items-center gap-2 rounded-control border px-3 py-2 text-left transition ${
                  kbType === v
                    ? "border-brand bg-brand-soft text-brand"
                    : "border-rule text-ink-2 hover:border-rule-strong"
                }`}
              >
                <Icon className="h-4 w-4 shrink-0" />
                <span className="flex flex-col leading-tight">
                  <span className="text-small font-medium">{label}</span>
                  <span className="text-micro text-ink-3">{hint}</span>
                </span>
              </button>
            ))}
          </div>

          {error && (
            <p className="mt-3 rounded-control bg-danger-soft px-3 py-2 text-small text-danger">
              {error}
            </p>
          )}

          <div className="mt-5 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-control border border-rule px-3.5 py-1.5 text-small text-ink-2 transition hover:bg-canvas"
            >
              取消
            </button>
            <button
              onClick={() => void submit()}
              disabled={!name.trim() || busy}
              className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-1.5 text-small font-medium text-white transition hover:bg-brand-hover disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} 创建
            </button>
          </div>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

// ---------------------------------------------------------------- 知识库详情

type IngestMode = "file" | "text" | "website" | null;

function KbDetail({ kbId, onBack }: { kbId: string; onBack: () => void }) {
  const [base, setBase] = useState<KbBase | null>(null);
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<IngestMode>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 库设置（改名 / 描述）
  const [editOpen, setEditOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  // 检索预览
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KBHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  // 文档内容预览
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<KbPreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewAbort = useRef<AbortController | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const [bases, docRes] = await Promise.all([
        fetchKbBases(),
        fetchKbDocuments(kbId),
      ]);
      const found = bases.bases.find((b) => b.id === kbId) ?? null;
      setBase(found);
      if (found) {
        setName(found.name);
        setDescription(found.description);
      }
      setDocs(docRes.documents);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const ingestFile = async (file: File) => {
    setBusy(true);
    setHint(null);
    setError(null);
    try {
      const res = await uploadKbDocument(kbId, file);
      setHint(res.hint ?? `已入库 ${res.document.chunks} 个分块：${res.document.name}`);
      setMode(null);
      await reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runSearch = async () => {
    const kw = query.trim();
    if (!kw) return;
    setSearching(true);
    setError(null);
    try {
      const res = await searchKb(kbId, kw, 5);
      setHits(res.hits);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSearching(false);
    }
  };

  const removeDoc = async (doc: KbDocument) => {
    if (!window.confirm(`删除文档「${doc.name}」及其 ${doc.chunks} 个分块？`)) return;
    try {
      await deleteKbDocument(kbId, doc.id);
      await reload();
      setHits(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Esc 关闭预览（与文件库一致）
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

  const openDocPreview = (doc: KbDocument) => {
    previewAbort.current?.abort();
    const ac = new AbortController();
    previewAbort.current = ac;
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(true);
    setPreviewOpen(true);
    previewKbDocument(kbId, doc.id, ac.signal)
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

  const removeBase = async () => {
    if (!base) return;
    if (!window.confirm(`删除知识库「${base.name}」及其全部分块？此操作不可撤销。`)) return;
    try {
      await deleteKbBase(kbId);
      onBack();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const saveBase = async () => {
    try {
      await updateKbBase(kbId, { name: name.trim(), description });
      setEditOpen(false);
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (loading) {
    return (
      <div className="flex h-full flex-col bg-canvas">
        <div className="border-b border-rule bg-white px-6 py-4">
          <div className="h-6 w-32 animate-pulse rounded-control bg-rule" aria-hidden />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div className="mx-auto w-full max-w-[1400px]">
            <SkeletonList rows={5} />
          </div>
        </div>
      </div>
    );
  }

  const isWeb = base?.kb_type === "website";

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      <div className="border-b border-rule bg-white px-6 py-3">
        <div className="flex items-center gap-2.5">
          <button
            onClick={onBack}
            className="inline-flex items-center gap-1 rounded-control border border-rule px-2.5 py-1.5 text-small text-ink-2 transition hover:bg-canvas"
          >
            <ChevronLeft className="h-4 w-4" /> 返回
          </button>
          <span
            className={`grid h-9 w-9 place-items-center rounded-control ${
              isWeb ? "bg-brand-soft text-brand" : "bg-brand-soft text-brand"
            }`}
          >
            {isWeb ? <Globe className="h-5 w-5" /> : <BookOpen className="h-5 w-5" />}
          </span>
          <h1 className="text-heading font-semibold text-ink">{base?.name}</h1>
          <span className="rounded-control bg-canvas px-2 py-0.5 text-small text-ink-3">
            {KB_TYPE_LABEL[base?.kb_type ?? ""] ?? base?.kb_type}
          </span>
          <span className="inline-flex items-center gap-1 text-small text-ink-3">
            <Lock className="h-4 w-4" /> {base?.visibility === "public" ? "公开" : "私有"}
          </span>
          <span className="ml-auto text-small text-ink-3">
            {base?.documents ?? 0} 文档 · {base?.chunks ?? 0} 分块
          </span>
          <button
            onClick={() => setEditOpen(true)}
            className="grid h-9 w-9 place-items-center rounded-control border border-rule text-ink-3 transition hover:text-ink"
            title="编辑知识库"
            aria-label="编辑知识库"
          >
            <Pencil className="h-4 w-4" />
          </button>
          <button
            onClick={() => void removeBase()}
            className="grid h-9 w-9 place-items-center rounded-control border border-rule text-ink-3 transition hover:border-danger hover:text-danger"
            title="删除知识库"
            aria-label="删除知识库"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
        {base?.description && (
          <p className="mt-2 pl-[50px] text-small text-ink-3">{base.description}</p>
        )}
        </div>

      {/* 主体：预览打开时整栏让位给预览面板（非弹窗、非挤压） */}
      {previewOpen ? (
        <div className="flex min-h-0 flex-1">
          <PreviewPanel
            open={previewOpen}
            onClose={() => {
              previewAbort.current?.abort();
              setPreviewOpen(false);
              setPreviewLoading(false);
            }}
            title={preview?.name ?? "正在加载…"}
            subtitle={
              preview
                ? preview.doc_type
                  ? `${DOC_TYPE_LABEL[preview.doc_type] ?? preview.doc_type} · ${preview.source}`
                  : preview.source
                : undefined
            }
            render={(preview?.render ?? "text") as any}
            loading={previewLoading}
            error={previewError}
            previewable={preview?.previewable ?? false}
            reason={preview?.reason ?? null}
            text={preview?.text ?? null}
            truncated={preview?.truncated ?? false}
            encoding={null}
            chars={preview?.chars ?? 0}
            downloadUrl={null}
            rawUrl={null}
          />
        </div>
      ) : (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-6 py-5">
        {error && (
          <div className="mb-4 rounded-panel border border-danger bg-danger-soft px-4 py-2.5 text-small text-danger">
            {error}
          </div>
        )}
        {hint && (
          <div className="mb-4 rounded-panel border border-verified bg-verified-soft px-4 py-2.5 text-small text-verified">
            {hint}
          </div>
        )}

        {/* 入库动作 */}
        <div className="mt-1 flex flex-wrap items-center gap-2.5">
          <button
            onClick={() => fileRef.current?.click()}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-control bg-brand px-3.5 py-2 text-small font-medium text-white transition hover:bg-brand-hover disabled:opacity-50"
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            上传文件
          </button>
          <button
            onClick={() => setMode("text")}
            className="inline-flex items-center gap-2 rounded-control border border-rule bg-white px-3.5 py-2 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand"
          >
            <FileText className="h-4 w-4" /> 粘贴文本
          </button>
          <button
            onClick={() => setMode("website")}
            className="inline-flex items-center gap-2 rounded-control border border-rule bg-white px-3.5 py-2 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand"
          >
            <Link2 className="h-4 w-4" /> 导入网页
          </button>
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void ingestFile(f);
              e.target.value = "";
            }}
          />
        </div>

        {/* 文档列表 */}
        <div className="mt-5 overflow-hidden rounded-panel border border-rule bg-white">
          <div className="flex items-center gap-2 border-b border-rule px-4 py-2.5">
            <span className="text-small font-semibold text-ink-2">已入库文档</span>
            <span className="rounded-full bg-canvas px-2 py-0.5 text-micro text-ink-3">
              {docs.length}
            </span>
          </div>
          {docs.length === 0 ? (
            <p className="px-4 py-10 text-center text-small text-ink-3">
              还没有文档。用上面的按钮上传文件、粘贴文本或导入网页。
            </p>
          ) : (
            <table className="w-full text-left">
              <thead>
                <tr className="border-b border-rule text-micro text-ink-3">
                  <th className="px-4 py-2 font-medium">名称</th>
                  <th className="w-20 px-3 py-2 font-medium">类型</th>
                  <th className="w-20 px-3 py-2 font-medium">大小</th>
                  <th className="w-20 px-3 py-2 font-medium">分块</th>
                  <th className="w-32 px-3 py-2 font-medium">入库时间</th>
                  <th className="w-20 px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id} className="group border-b border-rule last:border-0 hover:bg-canvas">
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <span className="grid h-6 w-6 shrink-0 place-items-center rounded-control bg-canvas text-ink-3">
                          {d.doc_type === "website" ? (
                            <Globe className="h-4 w-4" />
                          ) : d.doc_type === "text" ? (
                            <FileText className="h-4 w-4" />
                          ) : (
                            <FileText className="h-4 w-4" />
                          )}
                        </span>
                        <button
                          type="button"
                          onClick={() => openDocPreview(d)}
                          className="truncate text-left text-small text-ink-2 transition hover:text-brand"
                          title={`预览 ${d.name}`}
                        >
                          {d.name}
                        </button>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-small text-ink-3">
                      {DOC_TYPE_LABEL[d.doc_type] ?? d.doc_type}
                    </td>
                    <td className="px-3 py-2.5 text-small text-ink-3">
                      {fmtBytes(d.bytes)}
                    </td>
                    <td className="px-3 py-2.5 text-small text-ink-3">{d.chunks}</td>
                    <td className="px-3 py-2.5 text-micro text-ink-3">
                      {d.created_at.slice(0, 16)}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      {/* 删除常驻可见（静默 40% 不透明，行 hover / 键盘聚焦提亮）。
                          与文件库表格同一条规则：opacity-0 + group-hover 让触屏用户
                          根本触达不到，等于把功能藏起来了。 */}
                      <div className="flex items-center justify-end gap-0.5 opacity-40 transition group-hover:opacity-100 focus-within:opacity-100">
                        <button
                          onClick={() => openDocPreview(d)}
                          className="grid h-7 w-7 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-brand"
                          title="预览内容"
                          aria-label={`预览文档 ${d.name}`}
                        >
                          <Eye className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => void removeDoc(d)}
                          className="rounded-control p-1 text-ink-3 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100 focus-visible:opacity-100"
                          title="删除文档"
                          aria-label={`删除文档 ${d.name}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* 检索预览 */}
        <div className="mt-5 rounded-panel border border-rule bg-white p-5">
          <div className="flex items-center gap-2.5">
            <span className="text-body font-semibold text-ink-2">检索预览</span>
            <span className="text-small text-ink-3">
              用问题验证这个库召回了什么（只在本库内检索）
            </span>
          </div>
          <div className="mt-3 flex gap-2.5">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void runSearch()}
              placeholder="如：营收的口径是什么"
              aria-label="检索预览关键词"
              className="flex-1 rounded-control border border-rule px-3.5 py-2.5 text-small outline-none transition focus:border-brand focus:ring-2 focus:ring-brand"
            />
            <button
              onClick={() => void runSearch()}
              disabled={searching || !query.trim()}
              className="inline-flex items-center gap-2 rounded-control border border-rule px-3.5 py-2.5 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand disabled:opacity-50"
            >
              {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              检索
            </button>
          </div>

          {hits !== null && (
            <div className="mt-3.5 space-y-2.5">
              {hits.length === 0 ? (
                <p className="rounded-control bg-canvas px-4 py-3.5 text-small text-ink-3">
                  没有命中。可能该库还没有相关内容，或换个说法再试。
                </p>
              ) : (
                hits.map((h, i) => (
                  <div key={`${h.source}-${i}`} className="rounded-control border border-rule bg-canvas px-4 py-3">
                    <div className="flex items-center gap-2.5 text-small text-ink-3">
                      <span className="rounded bg-white px-2 py-0.5 font-mono text-small text-ink-3">
                        {h.score.toFixed(4)}
                      </span>
                      <span className="truncate">{h.source}</span>
                    </div>
                    <p className="mt-1.5 whitespace-pre-wrap text-small leading-relaxed text-ink-2">
                      {h.text.length > 320 ? `${h.text.slice(0, 320)}…` : h.text}
                    </p>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
      </div>
    )}

      <TextIngestModal
        open={mode === "text"}
        onClose={() => setMode(null)}
        onSubmit={async (text, docName) => {
          setBusy(true);
          setError(null);
          try {
            const res = await addKbText(kbId, text, docName);
            setHint(`已入库 ${res.document.chunks} 个分块：${res.document.name}`);
            setMode(null);
            await reload();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      />

      <WebsiteIngestModal
        open={mode === "website"}
        onClose={() => setMode(null)}
        onSubmit={async (url) => {
          setBusy(true);
          setError(null);
          try {
            const res = await addKbWebsite(kbId, url);
            setHint(`已入库 ${res.document.chunks} 个分块：${res.document.name}${res.hint ? `（${res.hint}）` : ""}`);
            setMode(null);
            await reload();
          } catch (e) {
            setError((e as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      />

        <Modal open={editOpen} onClose={() => setEditOpen(false)}>
          <ModalBody className="max-w-md">
            <ModalContent>
          <h2 className="text-heading font-semibold text-ink">编辑知识库</h2>
          <label
            htmlFor="kb-edit-name"
            className="mt-4 block text-small font-medium text-ink-2"
          >
            名称
          </label>
          <input
            id="kb-edit-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1.5 w-full rounded-control border border-rule px-3 py-2 text-small outline-none focus:border-brand focus:ring-2 focus:ring-brand"
          />
          <label
            htmlFor="kb-edit-desc"
            className="mt-3.5 block text-small font-medium text-ink-2"
          >
            描述
          </label>
          <textarea
            id="kb-edit-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className="mt-1.5 w-full resize-none rounded-control border border-rule px-3 py-2 text-small outline-none focus:border-brand focus:ring-2 focus:ring-brand"
          />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setEditOpen(false)}
                className="rounded-control border border-rule px-3.5 py-1.5 text-small text-ink-2 hover:bg-canvas"
              >
                取消
              </button>
              <button
                onClick={() => void saveBase()}
                disabled={!name.trim()}
                className="rounded-control bg-brand px-3.5 py-1.5 text-small font-medium text-white hover:bg-brand-hover disabled:opacity-50"
              >
                保存
              </button>
            </div>
          </ModalContent>
        </ModalBody>
      </Modal>

    </div>
  );
}

function TextIngestModal({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (text: string, name: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setText("");
      setName("");
    }
  }, [open]);

  return (
    <Modal open={open} onClose={onClose}>
      <ModalBody className="max-w-lg">
        <ModalContent>
          <h2 className="text-heading font-semibold text-ink">粘贴文本入库</h2>
          <p className="mt-1 text-small text-ink-3">
            适合把口径定义、会议结论这类零散知识直接存进知识库。
          </p>
          <label
            htmlFor="kb-text-name"
            className="mt-4 block text-small font-medium text-ink-2"
          >
            文档名
          </label>
          <input
            id="kb-text-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：季度口径说明"
            className="mt-1.5 w-full rounded-control border border-rule px-3 py-2 text-small outline-none focus:border-brand focus:ring-2 focus:ring-brand"
          />
          <label
            htmlFor="kb-text-body"
            className="mt-3.5 block text-small font-medium text-ink-2"
          >
            内容
          </label>
          <textarea
            id="kb-text-body"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={8}
            placeholder="粘贴知识内容…"
            className="mt-1.5 w-full resize-y rounded-control border border-rule px-3 py-2 text-small leading-relaxed outline-none focus:border-brand focus:ring-2 focus:ring-brand"
          />
          <div className="mt-5 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-control border border-rule px-3.5 py-1.5 text-small text-ink-2 hover:bg-canvas"
            >
              取消
            </button>
            <button
              onClick={async () => {
                setBusy(true);
                try {
                  await onSubmit(text, name.trim() || "粘贴文本");
                } finally {
                  setBusy(false);
                }
              }}
              disabled={!text.trim() || busy}
              className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-1.5 text-small font-medium text-white hover:bg-brand-hover disabled:opacity-50"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} 入库
            </button>
          </div>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

function WebsiteIngestModal({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (url: string) => Promise<void>;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) setUrl("");
  }, [open]);

  return (
    <Modal open={open} onClose={onClose}>
      <ModalBody className="max-w-md">
        <ModalContent>
          <h2 className="text-heading font-semibold text-ink">导入网页</h2>
          <p className="mt-1 text-small text-ink-3">
            抓取页面正文（自动剥掉导航/脚本）并切块入库，之后可用于检索。
          </p>
          <label
            htmlFor="kb-web-url"
            className="mt-4 block text-small font-medium text-ink-2"
          >
            网页链接
          </label>
          <input
            id="kb-web-url"
            value={url}
            autoFocus
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && url.trim() && void onSubmit(url.trim())}
            placeholder="https://…"
            className="mt-1.5 w-full rounded-control border border-rule px-3 py-2 text-small outline-none focus:border-brand focus:ring-2 focus:ring-brand"
          />
          <div className="mt-5 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-control border border-rule px-3.5 py-1.5 text-small text-ink-2 hover:bg-canvas"
            >
              取消
            </button>
            <button
              onClick={async () => {
                setBusy(true);
                try {
                  await onSubmit(url.trim());
                } finally {
                  setBusy(false);
                }
              }}
              disabled={!url.trim() || busy}
              className="inline-flex items-center gap-1.5 rounded-control bg-brand px-3.5 py-1.5 text-small font-medium text-white hover:bg-brand-hover disabled:opacity-50"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />} 抓取并入库
            </button>
          </div>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}
