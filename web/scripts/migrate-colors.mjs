/**
 * 色彩语义化迁移（一次性脚本）。
 *
 * 背景：审查发现全站用 6 个色系但 0 个语义 —— 同一个靛蓝在侧栏表示「选中」、
 * 在按钮表示「主操作」、在徽章表示「进行中」。本脚本把「色系 + 明度」的
 * 视觉描述替换为「含义」的语义令牌（见 src/index.css 的 @theme）：
 *
 *   slate-[900..50]   → ink / ink-2 / ink-3 / rule / rule-strong / canvas
 *   indigo/violet/sky → brand / brand-hover / brand-soft
 *   emerald           → verified / verified-soft
 *   amber             → attention / attention-soft
 *   rose              → danger / danger-soft
 *
 * 用法：
 *   node scripts/migrate-colors.mjs          # dry-run，只打印统计
 *   node scripts/migrate-colors.mjs --write  # 落盘
 *
 * 说明：匹配从属性词（bg/text/border/…）开始，因此任意前缀
 * （hover: / focus: / group-hover: / aria-current: …）都会被保留。
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

const WRITE = process.argv.includes("--write");
const root = path.resolve(process.cwd(), "src");

/** 递归收集 ts/tsx 源文件。 */
function collect(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) collect(p, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

/* ---------------------------------------------------------------------------
   映射表：family + shade → 语义令牌
   key 为 `${family}-${shade}`，value 为替换后的调色板后缀（不含属性前缀）。
   分组按「明度越高越淡」的规律，使同一属性在不同明度上仍落在同一语义族。
--------------------------------------------------------------------------- */
const MAP = {
  /* ── slate：表面层级与文字层级 ─────────────────────────────────────── */
  // 文字层级
  "slate-900": { text: "ink", border: "rule-strong", bg: "ink", ring: "rule-strong" },
  "slate-800": { text: "ink", border: "rule-strong", bg: "ink", ring: "rule-strong" },
  "slate-700": { text: "ink-2", border: "rule-strong", bg: "ink-2", ring: "rule-strong" },
  "slate-600": { text: "ink-2", border: "rule-strong", bg: "ink-2", ring: "rule-strong" },
  "slate-500": { text: "ink-3", border: "rule-strong", bg: "ink-3", ring: "rule-strong" },
  "slate-400": { text: "ink-3", border: "rule", bg: "rule", ring: "rule" },
  "slate-300": { text: "ink-3", border: "rule-strong", bg: "rule", ring: "rule" },
  "slate-200": { text: "ink-3", border: "rule", bg: "rule", ring: "rule" },
  "slate-100": { text: "ink-3", border: "rule", bg: "canvas", ring: "rule" },
  "slate-50": { text: "ink-3", border: "rule", bg: "canvas", ring: "rule" },

  /* ── indigo / violet / sky：统一为单一品牌色 ──────────────────────── */
  // 深色 → 主操作；中等 → 强调边框；浅色 → 浅底
  "indigo-900": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "indigo-800": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "indigo-700": { text: "brand", border: "brand", bg: "brand-hover", ring: "brand", shadow: null },
  "indigo-600": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "indigo-500": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "indigo-400": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "indigo-300": { text: "brand", border: "brand", bg: "brand-soft", ring: "brand", shadow: null },
  "indigo-200": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "indigo-100": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "indigo-50": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },

  "violet-700": { text: "brand", border: "brand", bg: "brand-hover", ring: "brand", shadow: null },
  "violet-600": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "violet-500": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "violet-300": { text: "brand", border: "brand", bg: "brand-soft", ring: "brand", shadow: null },
  "violet-200": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "violet-100": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "violet-50": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },

  "sky-700": { text: "brand", border: "brand", bg: "brand-hover", ring: "brand", shadow: null },
  "sky-600": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "sky-500": { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  "sky-300": { text: "brand", border: "brand", bg: "brand-soft", ring: "brand", shadow: null },
  "sky-200": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "sky-100": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },
  "sky-50": { text: "brand", border: "rule-strong", bg: "brand-soft", ring: "brand", shadow: null },

  /* ── emerald：成功 / 已完成 / 健康 ─────────────────────────────────── */
  "emerald-700": { text: "verified", border: "verified", bg: "verified", ring: "verified", shadow: null },
  "emerald-600": { text: "verified", border: "verified", bg: "verified", ring: "verified", shadow: null },
  "emerald-500": { text: "verified", border: "verified", bg: "verified", ring: "verified", shadow: null },
  "emerald-300": { text: "verified", border: "verified", bg: "verified", ring: "verified", shadow: null },
  "emerald-200": { text: "verified", border: "verified", bg: "verified-soft", ring: "verified", shadow: null },
  "emerald-100": { text: "verified", border: "verified", bg: "verified-soft", ring: "verified", shadow: null },
  "emerald-50": { text: "verified", border: "verified", bg: "verified-soft", ring: "verified", shadow: null },

  /* ── amber：待办 / 进行中 / 需注意 ────────────────────────────────── */
  "amber-700": { text: "attention", border: "attention", bg: "attention", ring: "attention", shadow: null },
  "amber-600": { text: "attention", border: "attention", bg: "attention", ring: "attention", shadow: null },
  "amber-500": { text: "attention", border: "attention", bg: "attention", ring: "attention", shadow: null },
  "amber-300": { text: "attention", border: "attention", bg: "attention", ring: "attention", shadow: null },
  "amber-200": { text: "attention", border: "attention", bg: "attention-soft", ring: "attention", shadow: null },
  "amber-100": { text: "attention", border: "attention", bg: "attention-soft", ring: "attention", shadow: null },
  "amber-50": { text: "attention", border: "attention", bg: "attention-soft", ring: "attention", shadow: null },

  /* ── rose：失败 / 错误 / 危险 ─────────────────────────────────────── */
  "rose-700": { text: "danger", border: "danger", bg: "danger", ring: "danger", shadow: null },
  "rose-600": { text: "danger", border: "danger", bg: "danger", ring: "danger", shadow: null },
  "rose-500": { text: "danger", border: "danger", bg: "danger", ring: "danger", shadow: null },
  "rose-300": { text: "danger", border: "danger", bg: "danger", ring: "danger", shadow: null },
  "rose-200": { text: "danger", border: "danger", bg: "danger-soft", ring: "danger", shadow: null },
  "rose-100": { text: "danger", border: "danger", bg: "danger-soft", ring: "danger", shadow: null },
  "rose-50": { text: "danger", border: "danger", bg: "danger-soft", ring: "danger", shadow: null },
};

/* ---------------------------------------------------------------------------
   兜底规则：MAP 未覆盖的明度档（如 emerald-800/amber-400）按色系落位。
   不逐档枚举是为了「新增一个明度别让整条替换失败」；命中兜底的会单独列出，
   便于人工确认没有误落语义。
--------------------------------------------------------------------------- */
const FALLBACK = {
  slate: { text: "ink-2", border: "rule", bg: "canvas", ring: "rule" },
  indigo: { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  violet: { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  sky: { text: "brand", border: "brand", bg: "brand", ring: "brand", shadow: null },
  emerald: { text: "verified", border: "verified", bg: "verified", ring: "verified", shadow: null },
  amber: { text: "attention", border: "attention", bg: "attention", ring: "attention", shadow: null },
  rose: { text: "danger", border: "danger", bg: "danger", ring: "danger", shadow: null },
};

const PROPS = "bg|text|border|ring|divide|from|to|via|shadow|fill|stroke|placeholder|decoration|outline|accent|caret";
const FAMILIES = "slate|indigo|violet|sky|emerald|amber|rose";
const RE = new RegExp(`\\b(${PROPS})-(${FAMILIES})-(\\d{2,3})(\\/\\d+)?\\b`, "g");

const stats = new Map();
const fallbacks = new Map();
const changedFiles = [];

for (const file of collect(root)) {
  const src = readFileSync(file, "utf8");
  const out = src.replace(RE, (whole, prop, family, shade) => {
    let entry = MAP[`${family}-${shade}`];
    if (!entry) {
      entry = FALLBACK[family];
      if (!entry) {
        stats.set(`?? ${whole}`, (stats.get(`?? ${whole}`) || 0) + 1);
        return whole;
      }
      fallbacks.set(whole, (fallbacks.get(whole) || 0) + 1);
    }
    const target = entry[prop];
    const next = target === undefined || target === null ? "" : `${prop}-${target}`;
    const label = `${whole} → ${next || "(移除)"}`;
    stats.set(label, (stats.get(label) || 0) + 1);
    return next;
  });

  if (out !== src) {
    changedFiles.push(path.relative(root, file));
    if (WRITE) writeFileSync(file, out, "utf8");
  }
}

console.log(`\n${WRITE ? "已写入" : "DRY-RUN"} — 受影响文件 ${changedFiles.length} 个\n`);
console.log("替换明细（按次数降序）：");
const rows = [...stats.entries()].sort((a, b) => b[1] - a[1]);
for (const [k, v] of rows) console.log(`  ${String(v).padStart(4)}  ${k}`);
console.log(`\n合计 ${rows.reduce((s, [, v]) => s + v, 0)} 处替换`);
console.log(`\n文件清单：\n  ${changedFiles.join("\n  ")}`);
if (!WRITE) console.log("\n（dry-run，未落盘。加 --write 执行）");
