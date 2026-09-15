import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { Paperclip, Send, Square, X, Image as ImageIcon, FileText, FileCode2 } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/utils";
import type { Attachment } from "@/lib/types";

const PLACEHOLDERS = [
  "对比各区域营收表现，找出增长最快的地区",
  "分析最近半年的月度销售趋势并预测下个月",
  "诊断数据质量：缺失值、重复记录与异常分布",
  "把示例数据画成趋势图（上传 CSV 我帮你分析）",
];

const ACCEPT = "image/*,.pdf,.csv,.tsv,.txt,.md,.json,.xlsx,.xls,.docx,.doc,.log,.py,.sql";

/** 把 File / Blob 转成可在 UI 预览的 blob URL。调用方负责 revoke。 */
function fileToPreviewUrl(file: File): string {
  return URL.createObjectURL(file);
}

function inferKind(file: File): Attachment["kind"] {
  return file.type.startsWith("image/") ? "image" : "file";
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function extIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["csv", "tsv", "xlsx", "xls"].includes(ext)) return <FileText className="h-3.5 w-3.5" />;
  if (["json", "py", "sql", "md", "log"].includes(ext)) return <FileCode2 className="h-3.5 w-3.5" />;
  return <FileText className="h-3.5 w-3.5" />;
}

export function Composer({
  onSubmit,
  onStop,
  streaming,
}: {
  onSubmit: (text: string, attachments: Attachment[]) => void;
  onStop: () => void;
  streaming: boolean;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  const [dragging, setDragging] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const previewUrlsRef = useRef<string[]>([]);
  // 记录 blob URL，组件卸载时统一 revoke
  useEffect(() => {
    previewUrlsRef.current = attachments
      .map((a) => a.previewUrl)
      .filter((u): u is string => Boolean(u));
    return () => {
      previewUrlsRef.current.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [attachments]);

  // 跑马灯占位符
  useEffect(() => {
    const id = setInterval(() => setPlaceholderIdx((i) => (i + 1) % PLACEHOLDERS.length), 3500);
    return () => clearInterval(id);
  }, []);

  // 自动撑高 textarea（1~8 行）
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const lineH = 22;
    const next = Math.min(8, Math.max(1, Math.round(ta.scrollHeight / lineH)));
    ta.style.height = `${next * lineH + 18}px`;
  }, [text]);

  const addFiles = useCallback((files: FileList | File[]) => {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    setAttachments((prev) => [
      ...prev,
      ...arr.map((f) => ({
        id: crypto.randomUUID(),
        name: f.name,
        mime: f.type || "application/octet-stream",
        size: f.size,
        kind: inferKind(f),
        previewUrl: f.type.startsWith("image/") ? fileToPreviewUrl(f) : undefined,
        file: f, // 保留原始 File，供 App 真正 POST 到后端
      })),
    ]);
    taRef.current?.focus();
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const tgt = prev.find((a) => a.id === id);
      if (tgt?.previewUrl) URL.revokeObjectURL(tgt.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  const onFilePick = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addFiles(e.target.files);
    e.target.value = "";
  };

  // 拖拽
  const onDragEnter = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes("Files")) setDragging(true);
  };
  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
  };
  const onDragLeave = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragging(false);
  };
  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setDragging(false);
    if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files);
  };

  // 粘贴（图片 + 文件）
  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) {
          // 粘贴板里的图片常常没有文件名，给它补一个
          if (!f.name) {
            const ext = (f.type.split("/")[1] || "png").split(";")[0];
            const renamed = new File([f], `pasted-${Date.now()}.${ext}`, { type: f.type });
            files.push(renamed);
          } else {
            files.push(f);
          }
        }
      }
    }
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  };

  const submit = () => {
    const t = text.trim();
    if (!t && attachments.length === 0) return;
    onSubmit(t, attachments);
    attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    setText("");
    setAttachments([]);
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) ta.style.height = "auto";
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  // 流式：只显示「停止」控件，不允许编辑
  if (streaming) {
    return (
      <div className="flex items-center gap-3 rounded-panel border border-indigo-200/70 bg-indigo-50/70 px-4 py-3.5 shadow-sm">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-indigo-400 opacity-70" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-indigo-500" />
        </span>
        <span className="text-body font-medium text-indigo-900">智能体正在分析，请稍候…</span>
        <button
          onClick={onStop}
          className="ml-auto inline-flex items-center gap-1.5 rounded-control border border-indigo-200 bg-white px-3.5 py-2 text-small font-medium text-indigo-700 shadow-sm transition hover:bg-indigo-50"
        >
          <Square className="h-3.5 w-3.5" /> 停止
        </button>
      </div>
    );
  }

  const canSubmit = Boolean(text.trim()) || attachments.length > 0;

  return (
    <div
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn(
        "relative rounded-panel border border-slate-200/80 bg-white shadow-sm shadow-slate-200/50 transition",
        dragging && "ring-2 ring-indigo-400 ring-offset-2 ring-offset-slate-50",
      )}
    >
      {/* 拖拽时的全屏提示层 */}
      <AnimatePresence>
        {dragging && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="pointer-events-none absolute inset-0 z-20 flex flex-col items-center justify-center rounded-panel border-2 border-dashed border-indigo-400 bg-indigo-50/80 backdrop-blur-sm"
          >
            <Paperclip className="h-6 w-6 text-indigo-500" />
            <p className="mt-2 text-body font-medium text-indigo-700">松开即可上传文件或图片</p>
            <p className="mt-0.5 text-small text-indigo-500/80">支持图片 · CSV · Excel · PDF · JSON · 代码文件</p>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 已选附件预览行 */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 border-b border-slate-200/70 px-3 pt-3 pb-2">
          {attachments.map((a) =>
            a.kind === "image" && a.previewUrl ? (
              <div
                key={a.id}
                className="group relative h-16 w-16 overflow-hidden rounded-control border border-slate-200 bg-slate-50"
                title={`${a.name} · ${fmtSize(a.size)}`}
              >
                <img
                  src={a.previewUrl}
                  alt={a.name}
                  className="h-full w-full object-cover"
                />
                <button
                  type="button"
                  onClick={() => removeAttachment(a.id)}
                  aria-label="移除附件"
                  className="absolute right-0.5 top-0.5 rounded-full bg-slate-900/70 p-0.5 text-white opacity-0 transition group-hover:opacity-100"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ) : (
              <div
                key={a.id}
                className="group inline-flex max-w-[200px] items-center gap-1.5 rounded-control border border-slate-200 bg-slate-50 px-2 py-1.5 text-small text-slate-700"
                title={`${a.name} · ${fmtSize(a.size)}`}
              >
                {extIcon(a.name)}
                <span className="truncate font-medium">{a.name}</span>
                <span className="shrink-0 text-slate-400">{fmtSize(a.size)}</span>
                <button
                  type="button"
                  onClick={() => removeAttachment(a.id)}
                  aria-label="移除附件"
                  className="ml-0.5 rounded p-0.5 text-slate-400 transition hover:bg-slate-200 hover:text-slate-700"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ),
          )}
        </div>
      )}

      {/* 输入区 */}
      <div className="flex items-end gap-2 px-3 py-2.5">
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          multiple
          accept={ACCEPT}
          onChange={onFilePick}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          aria-label="上传文件或图片"
          title="上传文件或图片（支持拖拽 / Ctrl+V 粘贴）"
          className="grid h-9 w-9 shrink-0 place-items-center rounded-panel text-slate-400 transition hover:bg-slate-100 hover:text-indigo-600"
        >
          <Paperclip className="h-5 w-5" />
        </button>

        <div className="relative flex-1">
          <textarea
            ref={taRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
            rows={1}
            aria-label="提问输入框"
            placeholder={PLACEHOLDERS[placeholderIdx]}
            className={cn(
              "block w-full resize-none bg-transparent px-1 py-2 text-body leading-[22px] text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-0",
            )}
          />
        </div>

        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit}
          aria-label="发送"
          title="发送（⌘/Ctrl + Enter）"
          className={cn(
            "grid h-9 w-9 shrink-0 place-items-center rounded-panel transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300",
            canSubmit
              ? "bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-md shadow-indigo-500/30 hover:from-indigo-600 hover:to-violet-700"
              : "bg-slate-100 text-slate-300",
          )}
        >
          <Send className="h-4 w-4" />
        </button>
      </div>

      {/* 底部小提示行 */}
      <div className="flex items-center justify-between border-t border-slate-100 px-3.5 py-2 text-small text-slate-400">
        <span className="inline-flex items-center gap-1.5">
          <ImageIcon className="h-3.5 w-3.5" />
          支持图片、CSV、Excel、PDF、文本、代码文件
        </span>
        <span className="hidden sm:inline">
          Enter 换行 · <span className="rounded bg-slate-100 px-1 py-0.5 font-mono">⌘/Ctrl</span> + Enter 发送
        </span>
      </div>
    </div>
  );
}
