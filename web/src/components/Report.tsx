import { FeedbackWidget } from "./FeedbackWidget";
import { Markdown } from "./Markdown";

/**
 * 报告归一化：
 *  - 后端 reporter 的真实输出是 Markdown 字符串
 *  - 某些路径（MockLLM 信号、JSON 双重序列化等）会发来形如 `{"__markdown__": true}` 的
 *    "信号包"，无实质内容——这种渲染前必须降级到友好提示，绝不能让原始 JSON 字面量
 *    泄露到 UI 上。
 *  - 容忍两种 JSON 包装：① 仅含 `__markdown__: true` 信号；② 包了 Markdown 文本字段。
 */
function unwrapReport(raw: string): { content: string; degraded: boolean } {
  const text = (raw ?? "").trim();
  if (!text) return { content: "", degraded: true };

  // 仅在文本像 JSON 时才尝试解析（避免把 `# 标题` 当 JSON 解析）
  const looksJson = text.startsWith("{") && text.endsWith("}");
  if (!looksJson) return { content: raw, degraded: false };

  try {
    const obj = JSON.parse(text);
    if (obj && typeof obj === "object") {
      // 仅信号、无实质内容
      if (
        Object.keys(obj).length <= 3 &&
        "__markdown__" in obj &&
        !obj.content &&
        !obj.markdown &&
        !obj.text &&
        !obj.report
      ) {
        return { content: "", degraded: true };
      }
      // 包了 Markdown 内容字段 → 取出
      const inner =
        obj.content ?? obj.markdown ?? obj.text ?? obj.report ?? null;
      if (typeof inner === "string" && inner.trim().length > 0) {
        return { content: inner, degraded: false };
      }
    }
  } catch {
    /* 不是合法 JSON，当成 Markdown */
  }
  return { content: raw, degraded: false };
}

/**
 * 推断"单行连续管道符"里的列数（表头长度）。三个层次：
 *  1. snake_case 前导块（SQL 列名预览，如 channel_id / channel_name）
 *  2. 首个数字单元格出现在 i，则列数 = i - 1（假定首列非数字）
 *  3. 兜底：偶数个单元格 → 对半
 * 无法可靠推断时返回 0，调用方原样保留，避免「越修越坏」。
 */
function inferColCount(cells: string[]): number {
  if (cells.length < 4) return 0;
  const snake = /^[a-z_][a-z0-9_]*$/;
  let lead = 0;
  while (lead < cells.length && snake.test(cells[lead])) lead++;
  if (lead >= 2 && lead < cells.length) return lead;
  const numish = /^[¥$]?-?\d[\d,]*(\.\d+)?%?(万|亿|千|百)?$/;
  for (let i = 3; i < cells.length; i++) {
    if (numish.test(cells[i])) return i - 1;
  }
  if (cells.length % 2 === 0) return cells.length / 2;
  return 0;
}

/**
 * 把 LLM / 工具_digest 里常见的"伪表格"修成标准 GFM 表格：
 *  1. 单行连续管道符：`预览： | a | b | | 1 | 2 | ...`
 *  2. 多行有表头但没分隔线：`| a | b |\n| 1 | 2 |`
 *
 * 处理时保护 fenced code block 内的内容不被破坏。
 */
function normalizeMarkdownTables(md: string): string {
  if (!md) return md;

  // 先把 code block 占位保护起来
  const codeBlocks: string[] = [];
  const protectCode = (m: string) => {
    codeBlocks.push(m);
    return `\n__CODE_BLOCK_${codeBlocks.length - 1}__\n`;
  };
  let text = md.replace(/```[\s\S]*?```/g, protectCode);
  text = text.replace(/`[^`\n]+`/g, protectCode);

  // 1) 单行连续管道：把 "前缀： |a|b| |1|2| |3|4" 拆成多行表格
  text = text.replace(
    /([^\n|]*?\s*[:：]\s*)((?:\s*\|\s*[^|\n]+){2,}\s*\|[^\n]*)/g,
    (_match, prefix, pipePart) => {
      const cells = pipePart
        .split("|")
        .map((s: string) => s.trim())
        .filter((s: string) => s !== "");
      const colCount = inferColCount(cells);
      if (colCount < 2 || colCount >= cells.length) return _match;

      const header = cells.slice(0, colCount);
      const bodyRows: string[][] = [];
      for (let i = colCount; i < cells.length; i += colCount) {
        bodyRows.push(cells.slice(i, i + colCount));
      }
      const sep = Array(colCount).fill("---");
      const renderRow = (row: string[]) =>
        "| " + row.map((c) => c || " ").join(" | ") + " |";
      return (
        prefix.trim() +
        "\n\n" +
        [
          renderRow(header),
          renderRow(sep),
          ...bodyRows.map(renderRow),
        ].join("\n")
      );
    },
  );

  // 2) 多行表格缺分隔线：扫描每一行，若当前行是表格行且下一行不是分隔线也不是空行，
  //    则插入 `|---|---|`。
  const lines = text.split("\n");
  const tableRowRe = /^\s*\|(?:[^|]*\|){2,}\s*$/;
  const sepRowRe = /^\s*\|(?:\s*:?-+:?\s*\|){2,}\s*$/;
  const isDataRow = (l: string) => tableRowRe.test(l) && !sepRowRe.test(l);
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    out.push(line);
    // 仅当当前行是「表格首行/表头」（上一行不是任何表格行），且下一行是数据行
    // 且非分隔线时，才补一条分隔线。数据行之间不再误插。
    if (isDataRow(line)) {
      const prev = lines[i - 1] ?? "";
      const next = lines[i + 1];
      if (!tableRowRe.test(prev) && next && isDataRow(next)) {
        const cols = line.split("|").length - 1;
        out.push("|" + Array(Math.max(cols, 2)).fill(" --- ").join("|") + "|");
      }
    }
  }
  text = out.join("\n");

  // 还原 code block
  text = text.replace(
    /\n__CODE_BLOCK_(\d+)__\n/g,
    (_m, idx) => "\n" + codeBlocks[Number(idx)] + "\n",
  );

  return text;
}

export function Report({ raw, sessionId }: { raw: string; sessionId?: string }) {
  const { content, degraded } = unwrapReport(raw);

  if (degraded) {
    return (
      <div className="rounded-panel border border-attention bg-attention/[0.06] p-4 text-body text-attention">
        <div className="mb-1 font-medium">报告未生成</div>
        <p className="text-small leading-relaxed text-attention">
          智能体在报告生成阶段未产出有效内容（可能是上游 LLM
          暂时不可用）。请稍后重试，或换一个表述更具体的问题再试。
        </p>
      </div>
    );
  }

  return (
    <>
      <Markdown>{normalizeMarkdownTables(content)}</Markdown>
      {sessionId ? <FeedbackWidget sessionId={sessionId} /> : null}
    </>
  );
}
