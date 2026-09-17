/**
 * 设计令牌迁移脚本（一次性，迁移完成后可删）
 *
 * 目的：把「18 档硬编码字号 + 6 档圆角」收敛到 index.css 里定义的令牌。
 *
 * 字号 → 6 档：text-display(32) / title(22) / heading(17) / body(15) / small(13) / micro(11.5)
 * 圆角 → 2 档（+ 胶囊）：rounded-control(6) / rounded-panel(12) / rounded-full 保留
 *
 * 用法：
 *   node scripts/migrate-tokens.mjs           # dry-run，只报告
 *   node scripts/migrate-tokens.mjs --write   # 实际写入
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

/** 递归收集 .ts/.tsx 文件（不依赖实验性 API）。 */
function walk(dir, acc = []) {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name);
    if (statSync(p).isDirectory()) walk(p, acc);
    else if (/\.tsx?$/.test(name)) acc.push(p);
  }
  return acc;
}

const WRITE = process.argv.includes("--write");

// ─────────────────────────────────────────────────────────────
// 字号映射：按实测 px 值落到最近的令牌档
// ─────────────────────────────────────────────────────────────
const FS_TOKENS = [
  { max: 11.6, token: "text-micro", px: 11.5 },
  { max: 13.9, token: "text-small", px: 13 },
  { max: 15.9, token: "text-body", px: 15 },
  { max: 18.4, token: "text-heading", px: 17 },
  { max: 24.4, token: "text-title", px: 22 },
  { max: Infinity, token: "text-display", px: 32 },
];

// Tailwind 默认字号类 → 令牌
const FS_DEFAULT_MAP = {
  "text-xs": "text-small",     // 12 → 13
  "text-sm": "text-body",      // 14 → 15
  "text-base": "text-heading", // 16 → 17
  "text-lg": "text-heading",   // 18 → 17
  "text-xl": "text-title",     // 20 → 22
  "text-2xl": "text-title",    // 24 → 22
  "text-3xl": "text-display",  // 30 → 32
  "text-4xl": "text-display",  // 36 → 32
  "text-5xl": "text-display",
};

function mapPx(px) {
  return FS_TOKENS.find((t) => px <= t.max).token;
}

// ─────────────────────────────────────────────────────────────
// 圆角映射
// ─────────────────────────────────────────────────────────────
const RADIUS_MAP = {
  "rounded-sm": "rounded-control",
  "rounded-md": "rounded-control",
  "rounded-lg": "rounded-control",
  "rounded-xl": "rounded-panel",
  "rounded-2xl": "rounded-panel",
  "rounded-3xl": "rounded-panel",
};

const stats = {
  files: 0,
  fontSize: 0,
  radius: 0,
  details: {},
};

function bump(file, kind, from, to) {
  const key = `${file}|${kind}|${from}→${to}`;
  stats.details[key] = (stats.details[key] || 0) + 1;
  stats[kind === "fs" ? "fontSize" : "radius"] += 1;
}

function migrate(src, file) {
  let out = src;

  // 1) 任意 px 字号 → 最近的令牌档
  //    必须长值优先，避免 text-[14px] 误匹配 text-[14.5px] 的前缀
  out = out.replace(/text-\[(\d+(?:\.\d+)?)px\]/g, (m, n) => {
    const to = mapPx(parseFloat(n));
    bump(file, "fs", m, to);
    return to;
  });

  // 2) Tailwind 默认字号类 → 令牌（带词边界，避免打到 text-sm-xxx 之类）
  out = out.replace(
    /\btext-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl)\b/g,
    (m) => {
      // 排除 hover:text-xl / md:text-sm 这类带修饰前缀的情况由 \b 与修饰符共同保证；
      // 修饰前缀（sm: / hover: 等）在 \b 之外，不受影响。
      const to = FS_DEFAULT_MAP[m];
      if (!to) return m;
      bump(file, "fs", m, to);
      return to;
    },
  );

  // 3) 圆角 → 两档（rounded-full 与 rounded-none 保留）
  out = out.replace(/\brounded-(sm|md|lg|xl|2xl|3xl)\b/g, (m) => {
    const to = RADIUS_MAP[m];
    if (!to) return m;
    bump(file, "radius", m, to);
    return to;
  });

  return out;
}

// ─────────────────────────────────────────────────────────────
// 目标文件：全部业务源码（不动 index.css，那里是令牌定义本身）
// ─────────────────────────────────────────────────────────────
const root = path.resolve(process.cwd(), "src");
const files = walk(root);

for (const f of files) {
  const src = readFileSync(f, "utf8");
  const next = migrate(src, path.relative(root, f).replace(/\\/g, "/"));
  if (next !== src) {
    stats.files += 1;
    if (WRITE) writeFileSync(f, next, "utf8");
  }
}

// ─────────────────────────────────────────────────────────────
// 报告
// ─────────────────────────────────────────────────────────────
console.log(`${WRITE ? "已写入" : "DRY-RUN"}：${stats.files} 个文件有改动`);
console.log(`  字号替换 ${stats.fontSize} 处，圆角替换 ${stats.radius} 处`);
console.log();

const rows = Object.entries(stats.details)
  .map(([k, n]) => {
    const [file, kind, map] = k.split("|");
    return { file, kind, map, n };
  })
  .sort((a, b) => b.n - a.n);

const byKind = { fs: rows.filter((r) => r.kind === "fs"), radius: rows.filter((r) => r.kind === "radius") };
for (const [kind, label] of [["fs", "字号"], ["radius", "圆角"]]) {
  console.log(`── ${label}映射（按用量）──`);
  const agg = {};
  for (const r of byKind[kind]) {
    const key = r.map;
    agg[key] = (agg[key] || 0) + r.n;
  }
  for (const [k, n] of Object.entries(agg).sort((a, b) => b[1] - a[1])) {
    console.log(`  ${String(n).padStart(4)}  ${k}`);
  }
  console.log();
}
