import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * LLM 偶尔会在 markdown 里夹带 HTML 换行标记（`<br>` / `<br/>` / `<br />`）。
 * react-markdown 默认不渲染 raw HTML → 字面量直接漏到页面上。
 * 这里把它翻译成 markdown 硬换行，而不是启用 rehype-raw（那会引入 XSS 面）。
 * 行内 `<br>2. xxx` 换行后正好落回 GFM 有序列表语义。
 */
function normalizeLineBreaks(md: string): string {
  return md.replace(/<br\s*\/?>/gi, "\n");
}

/** 一个单元格是否"看起来是数字"（金额/百分比/带千分位/正负号/括号负数）。 */
const NUMERIC_CELL = /^[¥$€£]?\s*[+-]?\(?\d[\d,\s]*(\.\d+)?\)?\s*(%|％|万|亿|千|元|人|天|次|单|倍)?$/;

function isNumericCell(raw: string): boolean {
  const s = raw.trim();
  if (!s || s === "-" || s === "—" || s === "/" || s === "N/A") return false;
  return NUMERIC_CELL.test(s);
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|");
}

/**
 * 报告表格的数字列右对齐。
 *
 * GFM 的列对齐由分隔行（`|:---:|`）决定，而 LLM 输出的表格几乎从不带对齐标记——
 * 于是数字列一律左对齐，位数参差时无法按位比较，这是数据表最影响可读性的缺陷。
 * 这里按**列内容**推断：正文行里数字占比达标的列，就在分隔行上补 `---:`，
 * 交给 remark-gfm 解析后由 th/td 渲染成右对齐 + tabular-nums。
 *
 * 只在数据行数 ≥2 时判定，避免小样本误判；标题行不参与统计。
 */
function alignNumericColumns(md: string): string {
  const lines = md.split("\n");
  for (let i = 0; i < lines.length - 1; i += 1) {
    const head = lines[i].trim();
    const delim = lines[i + 1]?.trim() ?? "";
    const isTable = head.startsWith("|") && /^\|[\s:|-]+\|?$/.test(delim) && delim.includes("-");
    if (!isTable) continue;

    // 收集本表数据行
    const body: string[][] = [];
    let j = i + 2;
    while (j < lines.length && lines[j].trim().startsWith("|")) {
      body.push(splitRow(lines[j]));
      j += 1;
    }
    if (body.length < 2) continue;

    const cols = splitRow(head).length;
    const delims = splitRow(delim);
    const next = [...delims];

    // 只遍历分隔行**实际存在**的列：LLM 输出的表格常有表头列数与分隔行列数不一致
    // （如表头 3 列、分隔行只写 2 个 `---`）。此时 next[c] 为 undefined，
    // 朴素写法 next[c].includes(...) 会抛 TypeError —— 而这是在渲染路径上，
    // 一抛就是整个报告卡片白屏。列数不匹配时以分隔行为准，不擅自扩充结构。
    for (let c = 0; c < Math.min(cols, next.length); c += 1) {
      const values = body.map((r) => r[c] ?? "").filter((v) => v.trim() !== "");
      if (values.length < 2) continue;
      const numeric = values.filter(isNumericCell).length;
      if (numeric / values.length >= 0.6 && !(next[c] ?? "").includes(":")) {
        next[c] = "---:"; // 右对齐，且让 remark-gfm 把它交给 td 渲染成 text-right
      }
    }
    lines[i + 1] = `|${next.join("|")}|`;
  }
  return lines.join("\n");
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="da-markdown text-body leading-[1.8] text-ink-2">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ ...p }) => (
            <h1
              className="mt-5 mb-2 text-title font-semibold text-ink"
              {...p}
            />
          ),
          h2: ({ ...p }) => (
            <h2
              className="mt-5 mb-2 text-heading font-semibold text-ink border-b border-rule pb-1"
              {...p}
            />
          ),
          h3: ({ ...p }) => (
            <h3
              className="mt-4 mb-1.5 text-heading font-semibold text-ink"
              {...p}
            />
          ),
          p: ({ ...p }) => <p className="my-2.5" {...p} />,
          ul: ({ ...p }) => (
            <ul className="my-2.5 list-disc pl-5 space-y-1" {...p} />
          ),
          ol: ({ ...p }) => (
            <ol className="my-2.5 list-decimal pl-5 space-y-1" {...p} />
          ),
          li: ({ ...p }) => <li className="pl-1" {...p} />,
          a: ({ ...p }) => (
            <a
              className="text-brand underline underline-offset-2 hover:text-brand"
              target="_blank"
              rel="noopener noreferrer"
              {...p}
            />
          ),
          // D51：报告图内嵌的图源（`![标题](/api/v1/chat/analyze/chart/...)`）。
          // 后端只提供位图（不含 svg）——同源 inline 的 SVG 可带脚本。
          img: ({ ...p }) => (
            <img
              className="my-3 h-auto max-w-full rounded-control border border-rule bg-white"
              loading="lazy"
              {...p}
            />
          ),
          strong: ({ ...p }) => (
            <strong className="font-semibold text-ink" {...p} />
          ),
          blockquote: ({ ...p }) => (
            <blockquote
              className="my-3 border-l-2 border-brand pl-3 text-ink-3 italic"
              {...p}
            />
          ),
          code: ({ className, children, ...props }) => {
            const isBlock = /language-/.test(className || "");
            if (isBlock) {
              return (
                <code
                  className="block rounded-control bg-ink border border-rule-strong p-3 my-3 overflow-x-auto text-small text-verified font-mono"
                  {...props}
                >
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-brand-soft px-1.5 py-0.5 text-small text-brand font-mono"
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: ({ ...p }) => (
            <pre className="bg-transparent p-0 my-0" {...p} />
          ),
          table: ({ ...p }) => (
            <div className="my-3 overflow-x-auto rounded-control border border-rule">
              <table
                className="w-full border-collapse text-small leading-snug"
                {...p}
              />
            </div>
          ),
          th: ({ ...p }) => (
            <th
              className="border-b border-rule bg-canvas px-3 py-1.5 text-left font-semibold text-ink"
              {...p}
            />
          ),
          // tabular-nums：等宽数字。右对齐只有在位数能对齐时才真正可读，
          // 否则 "1,204.50" 与 "980.00" 的小数点仍然错位。
          td: ({ ...p }) => (
            <td
              className="border-b border-rule px-3 py-1.5 text-ink-2 tabular-nums"
              {...p}
            />
          ),
          hr: ({ ...p }) => <hr className="my-4 border-rule" {...p} />,
        }}
      >
        {alignNumericColumns(normalizeLineBreaks(children))}
      </ReactMarkdown>
    </div>
  );
}
