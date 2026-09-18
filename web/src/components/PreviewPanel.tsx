import { AlertTriangle, Copy, Download, FileText, GripVertical, Image as ImageIcon, Loader2, Table, X } from "@/components/icons";
import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useMediaQuery, useResizablePanel } from "@/lib/panel";

export type PreviewRender =
  | "text"
  | "code"
  | "markdown"
  | "table"
  | "image"
  | "pdf"
  | "unsupported";

export interface PreviewPanelProps {
  open: boolean;
  onClose: () => void;
  /** 文件名（标头展示） */
  title: string;
  /** 副标题：文件库传 mime；知识库传 "doc_type · source" */
  subtitle?: string;
  /** 渲染模式——决定用哪一档内容区 */
  render: PreviewRender;
  loading: boolean;
  error: string | null;
  /** 预览是否可用 */
  previewable: boolean;
  /** 不可预览时的中文 reason（后端返回） */
  reason: string | null;
  /** 文本类渲染的内容（text / code / markdown / table 共用） */
  text: string | null;
  truncated: boolean;
  encoding: string | null;
  chars: number;
  /** 文件库场景提供下载直链；知识库置空 */
  downloadUrl?: string | null;
  /** 内嵌直链（image / pdf 走 raw 字节流，不走下载弹窗） */
  rawUrl?: string | null;
}

/** tailwind class 省略：统一使用 design-token 类（已在项目全局可用） */

/** 简单的 CSV/TSV → 二维数组解析（不用后端 pandas/tabulate，纯前端实现）。 */
function parseTable(text: string): string[][] {
  const trimmed = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trimEnd();
  if (!trimmed) return [];
  // 第一行判定分隔符：含 tab 走 TSV，否则逗号（逗号比 tab 常见得多）。
  const firstLine = trimmed.split("\n")[0] ?? "";
  const isTsv = firstLine.includes("\t") && !firstLine.includes(",");
  const delimiter = isTsv ? "\t" : ",";

  // 极简分割：不处理带换行的引号字段（预览用量足够）。
  // 若出现复杂 CSV（含引号嵌套），回退到纯文本展示以避免错列。
  const rows = trimmed.split("\n").map((line) => {
    if ((line.match(/"/g) || []).length % 2 !== 0) {
      // 引号奇数 → 复杂 CSV，标记为 fallback
      throw new Error("complex-csv");
    }
    const out: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (ch === '"') {
        if (inQ && line[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQ = !inQ;
        }
      } else if (ch === delimiter && !inQ) {
        out.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    out.push(cur);
    return out;
  });
  return rows;
}

function TableView({ text }: { text: string }) {
  const parsed = useMemo(() => {
    try {
      const rows = parseTable(text);
      if (rows.length === 0 || (rows.length === 1 && rows[0].length <= 1 && !text.includes("\t"))) {
        return null;
      }
      return rows;
    } catch {
      return null;
    }
  }, [text]);

  if (!parsed) {
    // 复杂 CSV 等回退到等宽纯文本
    return (
      <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-control border border-rule bg-canvas p-4 font-mono text-small leading-relaxed text-ink-2">
        {text}
      </pre>
    );
  }
  return (
    <div className="max-h-[55vh] overflow-auto rounded-control border border-rule">
      <table className="w-full border-collapse text-small text-ink-2">
        <thead className="sticky top-0 z-10 bg-canvas">
          <tr>
            {parsed[0].map((h, i) => (
              <th
                key={i}
                className="border-b border-rule bg-canvas px-3 py-2 text-left font-semibold text-ink"
              >
                {h.trim() || `列 ${i + 1}`}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {parsed.slice(1).map((row, ri) => (
            <tr key={ri} className="even:bg-canvas/60">
              {row.map((cell, ci) => (
                <td key={ci} className="border-b border-rule/60 px-3 py-1.5 align-top">
                  {cell.trim() || <span className="text-ink-3">—</span>}
                </td>
              ))}
              {/* 列数不齐时补齐 */}
              {row.length < parsed[0].length &&
                Array.from({ length: parsed[0].length - row.length }).map((_, k) => (
                  <td key={`pad-${k}`} className="border-b border-rule/60 px-3 py-1.5 text-ink-3">
                    —
                  </td>
                ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CodeView({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 静默失败 */
    }
  };
  return (
    <div className="relative">
      <button
        onClick={onCopy}
        title="复制代码"
        className="absolute right-2 top-2 z-10 grid h-7 w-7 place-items-center rounded-control border border-rule bg-canvas text-ink-3 transition hover:bg-canvas/80 hover:text-ink-2"
      >
        <Copy className="h-3.5 w-3.5" />
        {copied && (
          <span className="absolute -bottom-6 right-0 text-micro text-brand">已复制</span>
        )}
      </button>
      <pre className="max-h-[55vh] overflow-auto whitespace-pre break-words rounded-control border border-rule bg-canvas p-4 pr-12 font-mono text-small leading-relaxed text-ink-2">
        {text}
      </pre>
    </div>
  );
}

function PlainTextView({ text }: { text: string }) {
  return (
    <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded-control border border-rule bg-canvas p-4 text-small leading-relaxed text-ink-2">
      {text}
    </pre>
  );
}

function MarkdownView({ text }: { text: string }) {
  return (
    <div className="prose prose-sm max-h-[55vh] overflow-auto rounded-control border border-rule bg-white p-5 text-ink-2 marker:text-ink-3">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => (
            <a href={href} target="_blank" rel="noreferrer" className="text-brand hover:underline" {...props}>
              {children}
            </a>
          ),
          table: ({ children, ...props }) => (
            <div className="overflow-x-auto">
              <table className="my-2 border-collapse text-small" {...props}>
                {children}
              </table>
            </div>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}

function ImageViewer({ rawUrl, alt }: { rawUrl: string | null; alt: string }) {
  if (!rawUrl) {
    return (
      <div className="flex items-center gap-2 rounded-control border border-rule bg-canvas px-4 py-3 text-small text-ink-3">
        <AlertTriangle className="h-4 w-4 text-attention" />
        图片地址缺失
      </div>
    );
  }
  return (
    <div className="flex max-h-[60vh] items-center justify-center overflow-auto rounded-control border border-rule bg-[repeating-conic-gradient(#f3f4f6_0%_25%,#fff_0%_50%)] bg-[length:16px_16px] p-2">
      <img
        src={rawUrl}
        alt={alt}
        className="max-h-[58vh] max-w-full rounded-control object-contain shadow-card"
      />
    </div>
  );
}

function PdfViewer({ rawUrl }: { rawUrl: string | null }) {
  if (!rawUrl) {
    return (
      <div className="flex items-center gap-2 rounded-control border border-rule bg-canvas px-4 py-3 text-small text-ink-3">
        <AlertTriangle className="h-4 w-4 text-attention" />
        PDF 地址缺失
      </div>
    );
  }
  return (
    <div className="flex h-[60vh] flex-col overflow-hidden rounded-control border border-rule">
      {/* 顶栏：单独的下载按钮，让用户也可以另存为 */}
      <div className="flex shrink-0 items-center justify-between border-b border-rule bg-canvas px-3 py-2 text-small text-ink-2">
        <span className="text-ink-3">内嵌 PDF 预览</span>
        <a
          href={rawUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 rounded-control border border-rule px-2.5 py-1 transition hover:bg-canvas/80"
        >
          <Download className="h-3.5 w-3.5" /> 在新标签页打开
        </a>
      </div>
      <iframe
        src={`${rawUrl}#toolbar=1&navpanes=0`}
        title="PDF preview"
        className="h-full w-full"
      />
    </div>
  );
}

function RenderIcon({ render }: { render: PreviewRender }) {
  const cls = "h-4 w-4";
  switch (render) {
    case "markdown":
      return <FileText className={cls} />;
    case "table":
      return <Table className={cls} />;
    case "code":
      return <FileText className={cls} />;
    case "image":
    case "pdf":
      return <ImageIcon className={cls} />;
    default:
      return <FileText className={cls} />;
  }
}

function renderLabel(render: PreviewRender): string {
  switch (render) {
    case "markdown":
      return "Markdown";
    case "table":
      return "表格";
    case "code":
      return "代码";
    case "image":
      return "图片";
    case "pdf":
      return "PDF";
    case "unsupported":
      return "不可预览";
    default:
      return "文本";
  }
}

/**
 * 文件库 / 知识库共用的**内嵌**预览面板（非弹窗）。
 *
 * 直接嵌入页面右侧 / 抽屉区；按 `render` 字段在一组内嵌视图之间切换：
 * text / code / markdown / table / image / pdf / unsupported。
 * `loading` → spinner；`error` → 错误条；`previewable=false` → reason + 下载。
 */
export function PreviewPanel({
  open,
  onClose,
  title,
  subtitle,
  render,
  loading,
  error,
  previewable,
  reason,
  text,
  truncated,
  encoding,
  chars,
  downloadUrl,
  rawUrl,
}: PreviewPanelProps) {
  // 这些 hooks 必须在 early-return 之前调用（React hooks 顺序不可变）。
  // `md`（>=768px）分界：桌面端把预览面板做成条**可拖拽调宽**的侧栏（宽度写进
  // localStorage，刷新后仍生效）；移动端整屏抽屉化——顶条只留「关闭」，不显示侧栏把手。
  const isDesktop = useMediaQuery("(min-width:768px)");
  const { size: panelWidth, handleProps } = useResizablePanel({
    storageKey: "previewPanel.width",
    defaultSize: 460,
    min: 320,
    max: 720,
    orientation: "horizontal",
  });

  if (!open) return null;

  const rootPadBottom = isDesktop ? "pb-4" : "pb-[max(1rem,env(safe-area-inset-bottom))]";

  return (
    <aside
      className={
        "relative flex h-full min-h-0 flex-col bg-white " +
        (isDesktop ? "shrink-0 border-l border-rule" : "w-full border-t border-rule")
      }
      style={isDesktop ? { width: panelWidth } : undefined}
      aria-label="预览面板"
    >
      {/* 桌面端：侧栏左沿拖拽把手（`col-resize`），左右拖拽调宽。 */}
      {isDesktop && (
        <div
          {...handleProps}
          title="拖拽调整预览宽度"
          className="absolute inset-y-0 -left-1.5 z-20 flex w-3 cursor-col-resize items-center justify-center opacity-0 transition hover:opacity-100 focus-visible:opacity-100"
        >
          <span className="pointer-events-none flex h-10 w-1.5 items-center justify-center rounded-full bg-brand/40">
            <GripVertical className="h-3 w-3 text-white" />
          </span>
        </div>
      )}

      {/* ─── 标头 ─── */}
      <div className="flex shrink-0 items-start gap-3 border-b border-rule px-4 py-3">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-panel bg-brand-soft text-brand">
          <FileText className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h2 className="truncate text-body font-semibold text-ink">{title}</h2>
            <span className="inline-flex shrink-0 items-center gap-1 rounded-pill bg-canvas px-2 py-0.5 text-micro text-ink-3">
              <RenderIcon render={render} /> {renderLabel(render)}
            </span>
          </div>
          {subtitle && (
            <p className="mt-0.5 truncate text-micro text-ink-3">{subtitle}</p>
          )}
        </div>
        {downloadUrl && previewable && (
          <a
            href={downloadUrl}
            target="_blank"
            rel="noreferrer"
            title="下载原文件"
            className="grid h-8 w-8 shrink-0 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink-2"
          >
            <Download className="h-4 w-4" />
          </a>
        )}
        <button
          onClick={onClose}
          title="关闭预览"
          className="grid h-8 w-8 shrink-0 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink-2"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* ─── 内容区 ─── */}
      <div className={"flex-1 overflow-y-auto px-4 pt-4 " + rootPadBottom}>
        {loading ? (
          <div className="flex items-center gap-2.5 py-16 text-body text-ink-3">
            <Loader2 className="h-4 w-4 animate-spin" /> 正在加载预览…
          </div>
        ) : error ? (
          <div className="flex items-start gap-2.5 rounded-control border border-danger bg-danger-soft px-4 py-3 text-small text-danger">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        ) : !previewable ? (
          <div className="flex items-start gap-2.5 rounded-control border border-rule bg-canvas px-4 py-3 text-small text-ink-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-attention" />
            <span>
              {reason ?? "该内容不支持预览"}
              {downloadUrl && "。"}
              {downloadUrl && (
                <>
                  {" "}
                  可以
                  <a
                    href={downloadUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium text-brand underline-offset-2 hover:underline"
                  >
                    下载原文件
                  </a>
                  后查看。
                </>
              )}
            </span>
          </div>
        ) : (
          <div className="space-y-3">
            {render === "markdown" && text && <MarkdownView text={text} />}
            {render === "table" && text && <TableView text={text} />}
            {render === "code" && text && <CodeView text={text} />}
            {render === "text" && text && <PlainTextView text={text} />}
            {render === "image" && <ImageViewer rawUrl={rawUrl ?? null} alt={title} />}
            {render === "pdf" && <PdfViewer rawUrl={rawUrl ?? null} />}

            {text && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-micro text-ink-3">
                {encoding && <span>编码：{encoding}</span>}
                <span>{chars.toLocaleString()} 字符</span>
                {truncated && (
                  <span className="font-medium text-attention">
                    内容过长，已截断显示（仅展示前段）
                  </span>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
