import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { Paperclip, Send, Square, X, Image as ImageIcon, FileText, FileCode2, Sparkles, Check } from "@/components/icons";
import { AnimatePresence, motion } from "motion/react";
import { cn } from "@/lib/utils";
import { fetchSkills, type Skill } from "@/lib/api";
import type { Attachment } from "@/lib/types";

/**
 * 占位提示固定为一句。
 *
 * 原先这里是 3.5 秒轮播的多条示例问题——那属于「非用户触发的持续动效」：
 * 输入框里每隔几秒自行变化，容易被误读成「框里已经有内容」；
 * 示例问题本该出现在落地页（那里用户有充足的空间挑选），
 * 输入框只该安静地说明「这里能做什么」。
 */
const PLACEHOLDER = "描述你的业务问题，或上传 CSV / Excel 文件让智能体分析";

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
  if (["csv", "tsv", "xlsx", "xls"].includes(ext)) return <FileText className="h-4 w-4" />;
  if (["json", "py", "sql", "md", "log"].includes(ext)) return <FileCode2 className="h-4 w-4" />;
  return <FileText className="h-4 w-4" />;
}

export function Composer({
  onSubmit,
  onStop,
  streaming,
}: {
  onSubmit: (text: string, attachments: Attachment[], skillIds: string[]) => void;
  onStop: () => void;
  streaming: boolean;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  // Skills：本次对话要注入的技能（仅已启用的才出现在选择器里）。
  // 选择**跨消息保留**——用户勾一次，后续提问继续生效，直到手动取消。
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);

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
    onSubmit(t, attachments, selectedSkills);
    attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl));
    setText("");
    setAttachments([]);
    requestAnimationFrame(() => {
      const ta = taRef.current;
      if (ta) ta.style.height = "auto";
    });
  };

  // 拉取已启用技能供勾选。失败静默——技能是增强项，不能挡住输入框。
  useEffect(() => {
    let alive = true;
    fetchSkills()
      .then((r) => {
        if (!alive) return;
        setSkills((r.skills ?? []).filter((s) => s.enabled));
      })
      .catch(() => {
        /* 技能不可用时选择器留空即可 */
      });
    return () => {
      alive = false;
    };
  }, []);

  const toggleSkill = (id: string) => {
    setSelectedSkills((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
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
      <div className="flex items-center gap-3 rounded-panel border border-rule-strong bg-brand-soft px-4 py-3.5 shadow-sm">
        <span className="relative flex h-2.5 w-2.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand opacity-70" />
          <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-brand" />
        </span>
        <span className="text-body font-medium text-brand">智能体正在分析，请稍候…</span>
        <button
          onClick={onStop}
          className="ml-auto inline-flex items-center gap-1.5 rounded-control border border-rule-strong bg-white px-3.5 py-2 text-small font-medium text-brand shadow-sm transition hover:bg-brand-soft"
        >
          <Square className="h-4 w-4" /> 停止
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
        "relative rounded-panel border border-rule bg-white shadow-sm  transition",
        dragging && "ring-2 ring-brand ring-offset-2 ring-offset-slate-50",
      )}
    >
      {/* 拖拽时的全屏提示层 */}
      <AnimatePresence>
        {dragging && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="pointer-events-none absolute inset-0 z-20 flex flex-col items-center justify-center rounded-panel border-2 border-dashed border-brand bg-brand-soft backdrop-blur-sm"
          >
            <Paperclip className="h-6 w-6 text-brand" />
            <p className="mt-2 text-body font-medium text-brand">松开即可上传文件或图片</p>
            <p className="mt-0.5 text-small text-brand">支持图片 · CSV · Excel · PDF · JSON · 代码文件</p>
          </motion.div>
        )}
      </AnimatePresence>

      {/* 已选附件预览行 */}
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 border-b border-rule px-3 pt-3 pb-2">
          {attachments.map((a) =>
            a.kind === "image" && a.previewUrl ? (
              <div
                key={a.id}
                className="group relative h-16 w-16 overflow-hidden rounded-control border border-rule bg-canvas"
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
                  className="absolute right-0.5 top-0.5 rounded-full bg-ink p-0.5 text-white opacity-0 transition group-hover:opacity-100"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ) : (
              <div
                key={a.id}
                className="group inline-flex max-w-[200px] items-center gap-1.5 rounded-control border border-rule bg-canvas px-2 py-1.5 text-small text-ink-2"
                title={`${a.name} · ${fmtSize(a.size)}`}
              >
                {extIcon(a.name)}
                <span className="truncate font-medium">{a.name}</span>
                <span className="shrink-0 text-ink-3">{fmtSize(a.size)}</span>
                <button
                  type="button"
                  onClick={() => removeAttachment(a.id)}
                  aria-label="移除附件"
                  className="ml-0.5 rounded p-0.5 text-ink-3 transition hover:bg-rule hover:text-ink-2"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            ),
          )}
        </div>
      )}

      {/* 已勾选技能（跨消息保留，显式可移除） */}
      {selectedSkills.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-rule px-3.5 pt-2.5 pb-2">
          <span className="text-small text-ink-3">已选技能</span>
          {selectedSkills.map((id) => {
            const s = skills.find((x) => x.id === id);
            return (
              <span
                key={id}
                className="inline-flex items-center gap-1 rounded-control bg-brand-soft px-2 py-0.5 text-small font-medium text-brand"
              >
                <Sparkles className="h-3.5 w-3.5" />
                {s?.name ?? id}
                <button
                  type="button"
                  onClick={() => toggleSkill(id)}
                  aria-label={`移除技能 ${s?.name ?? id}`}
                  className="rounded p-0.5 transition hover:bg-brand/10"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </span>
            );
          })}
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
          className="grid h-9 w-9 shrink-0 place-items-center rounded-panel text-ink-3 transition hover:bg-canvas hover:text-brand"
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
            placeholder={PLACEHOLDER}
            className={cn(
              "block w-full resize-none bg-transparent px-1 py-2 text-body leading-[22px] text-ink placeholder:text-ink-3 focus:outline-none focus:ring-0",
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
            "grid h-9 w-9 shrink-0 place-items-center rounded-panel transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand",
            canSubmit
              ? "bg-brand text-white hover:bg-brand-hover"
              : "bg-canvas text-ink-3",
          )}
        >
          <Send className="h-4 w-4" />
        </button>
      </div>

      {/* 底部小提示行 + 技能选择器 */}
      <div className="flex items-center justify-between border-t border-rule px-3.5 py-2 text-small text-ink-3">
        <div className="flex items-center gap-2">
          <div className="relative">
            <button
              type="button"
              onClick={() => setPickerOpen((v) => !v)}
              aria-haspopup="listbox"
              aria-expanded={pickerOpen}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-control border px-2.5 py-1 text-small font-medium transition",
                selectedSkills.length > 0
                  ? "border-brand bg-brand-soft text-brand"
                  : "border-rule text-ink-3 hover:border-brand hover:text-brand",
              )}
              title="选择本次对话要注入的技能"
            >
              <Sparkles className="h-3.5 w-3.5" />
              技能
              {selectedSkills.length > 0 && (
                <span className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-white">
                  {selectedSkills.length}
                </span>
              )}
            </button>

            {pickerOpen && (
              <>
                {/* 点击外部关闭 */}
                <div
                  className="fixed inset-0 z-30"
                  aria-hidden
                  onClick={() => setPickerOpen(false)}
                />
                <div
                  role="listbox"
                  aria-label="选择技能"
                  className="absolute bottom-full left-0 z-40 mb-2 max-h-72 w-72 overflow-y-auto rounded-panel border border-rule bg-white p-2 shadow-2xl"
                >
                  {skills.length === 0 ? (
                    <p className="px-2 py-2 text-small leading-relaxed text-ink-3">
                      还没有已启用的技能。到左侧「技能」页新建或导入后即可在此勾选。
                    </p>
                  ) : (
                    skills.map((s) => {
                      const on = selectedSkills.includes(s.id);
                      return (
                        <button
                          key={s.id}
                          type="button"
                          role="option"
                          aria-selected={on}
                          onClick={() => toggleSkill(s.id)}
                          className="flex w-full items-start gap-2 rounded-control px-2 py-2 text-left transition hover:bg-canvas"
                        >
                          <span
                            className={cn(
                              "mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded border",
                              on ? "border-brand bg-brand text-white" : "border-rule-strong",
                            )}
                          >
                            {on && <Check className="h-3 w-3" />}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-small font-medium text-ink">
                              {s.name}
                            </span>
                            {s.description && (
                              <span className="mt-0.5 block line-clamp-2 text-small text-ink-3">
                                {s.description}
                              </span>
                            )}
                          </span>
                        </button>
                      );
                    })
                  )}
                </div>
              </>
            )}
          </div>
          <span className="hidden items-center gap-1.5 md:inline-flex">
            <ImageIcon className="h-4 w-4" />
            支持图片、CSV、Excel、PDF、文本、代码文件
          </span>
        </div>
        <span className="hidden sm:inline">
          Enter 换行 · <span className="rounded bg-canvas px-1 py-0.5 font-mono">⌘/Ctrl</span> + Enter 发送
        </span>
      </div>
    </div>
  );
}
