import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, KeyRound, Loader2 } from "@/components/icons";

/**
 * 设置页各面板共用的最小 UI 原语。
 *
 * 2026-09-17 重构：从「卡片 + 表单块」换成「分区 + 行」。
 *
 * 原来每个面板都套一层 `Card`（圆角 + 边框 + 阴影），字段是「标签在上、输入框在下」。
 * 结果是盒子套盒子、一屏放不下几条信息。参考形态改成了扁平的「分区 + 行」：
 * 分区靠标题和一条细分隔线划开，行是「标签左（定宽）/ 值右 / 操作按钮最右」。
 * 因此 `Card` 与 `KV` 被删除——设置页现在铺在白色内容列上，不再需要卡片外壳。
 *
 * 例外：未登录的登录/注册表单仍用 `Field`（标签在上）。居中的单列表单用竖排标签
 * 才读得顺，硬套行式反而别扭；它是唯一不套行式的地方。
 */

/** 分区：标题 + 细分隔线，无边框、无阴影、无白底（对齐参考图的「通用信息 / 团队信息」）。 */
export function Section({
  title,
  desc,
  right,
  children,
  className = "",
}: {
  title?: string;
  desc?: string;
  /** 分区标题右侧的操作（如「刷新」按钮） */
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`py-6 first:pt-0 last:pb-0 ${className}`}>
      {(title || right) && (
        <header className="flex items-start justify-between gap-3 border-b border-rule pb-2.5">
          <div className="min-w-0">
            {title && (
              <h3 className="text-body font-semibold text-ink">{title}</h3>
            )}
            {desc && (
              <p className="mt-0.5 text-small leading-relaxed text-ink-3">
                {desc}
              </p>
            )}
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </header>
      )}
      <div>{children}</div>
    </section>
  );
}

/**
 * 一行：标签左（定宽）/ 值右 / 操作按钮最右。
 *
 * 值的位置要么给 `value`，要么给 `children`（行内输入框等自定义内容），二者取一。
 * 行间用细分隔线分开，末行不留线——否则分区底部会多出一道悬空的横线。
 */
export function Row({
  label,
  value,
  action,
  children,
  align = "center",
}: {
  label: string;
  value?: ReactNode;
  /** 行尾的操作按钮（图 2 里 [变更] 的位置） */
  action?: ReactNode;
  /** 值位置的自定义内容（行内输入框、开关等） */
  children?: ReactNode;
  align?: "center" | "start";
}) {
  return (
    <div
      className={`flex gap-5 border-b border-rule py-3 last:border-0 ${
        align === "start" ? "items-start" : "items-center"
      }`}
    >
      <div className="w-28 shrink-0 text-small text-ink-3">{label}</div>
      <div className="min-w-0 flex-1 text-small text-ink-2">
        {children ?? value}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/** 值排版：空值统一显示灰色占位，避免每处各写一个「—」。 */
export function Value({
  children,
  empty,
}: {
  children?: ReactNode;
  /** 空值文案，默认「未设置」 */
  empty?: string;
}) {
  const isEmpty =
    children === null ||
    children === undefined ||
    children === "" ||
    children === false;
  if (isEmpty) return <span className="text-ink-3">{empty ?? "未设置"}</span>;
  return <>{children}</>;
}

/** 状态徽标：角色、账号来源、成员状态等。 */
export function Badge({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "brand" | "verified" | "attention" | "danger";
  children: ReactNode;
}) {
  const tones: Record<string, string> = {
    neutral: "bg-canvas text-ink-3",
    brand: "bg-brand-soft text-brand",
    verified: "bg-verified-soft text-verified",
    attention: "bg-attention-soft text-attention",
    danger: "bg-danger-soft text-danger",
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-control px-2 py-0.5 text-micro font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/** 分区底部的操作条（脏值时出现的「保存修改 / 撤销」）。 */
export function SectionFooter({ children }: { children: ReactNode }) {
  return <div className="flex items-center gap-2 pt-3.5">{children}</div>;
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-baseline gap-2">
        <span className="text-small font-medium text-ink-2">{label}</span>
        {hint && <span className="text-micro text-ink-3">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export const inputCls =
  "w-full rounded-control border border-rule bg-white px-3 py-2 text-small text-ink outline-none transition placeholder:text-ink-3 focus:border-brand focus:ring-2 focus:ring-brand disabled:bg-canvas disabled:text-ink-3";

/** 行内输入框：宽度收敛，避免把整行拉满（行式布局里值区通常只有一半宽度）。 */
export const rowInputCls = `${inputCls} max-w-sm`;

export function Button({
  children,
  onClick,
  type = "button",
  variant = "primary",
  disabled,
  loading,
  className = "",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: "primary" | "secondary" | "ghost" | "danger";
  disabled?: boolean;
  loading?: boolean;
  className?: string;
  title?: string;
}) {
  const variants: Record<string, string> = {
    primary:
      "bg-brand text-white hover:bg-brand-hover shadow-sm disabled:bg-brand-soft",
    secondary:
      "border border-rule bg-white text-ink-2 hover:border-rule-strong hover:bg-canvas",
    ghost: "text-ink-2 hover:bg-canvas",
    danger:
      "border border-danger bg-white text-danger hover:bg-danger-soft",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className={`inline-flex items-center justify-center gap-1.5 rounded-control px-3.5 py-2 text-small font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${variants[variant]} ${className}`}
    >
      {loading && <Loader2 className="h-4 w-4 animate-spin" />}
      {children}
    </button>
  );
}

export function Notice({
  kind,
  children,
}: {
  kind: "error" | "success" | "info";
  children: ReactNode;
}) {
  const styles = {
    error: "border-danger bg-danger-soft text-danger",
    success: "border-verified bg-verified-soft text-verified",
    info: "border-rule bg-canvas text-ink-2",
  }[kind];
  const Icon = kind === "success" ? CheckCircle2 : AlertCircle;
  return (
    <div
      role={kind === "error" ? "alert" : undefined}
      className={`flex items-start gap-2 rounded-control border px-3 py-2 text-small leading-relaxed ${styles}`}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" />
      <span className="min-w-0">{children}</span>
    </div>
  );
}

/**
 * 未登录时的分区占位。
 *
 * 只有**认证关闭**的部署会走到这里：那种情况下 `user` 恒为 null，但也不存在
 * 「登录」这个动作，所以说「请去登录」是误导——真正的状态是「账号体系没开」。
 * 认证开启但未登录时，整页已经被 SettingsView 拦在登录入口了，到不了面板。
 */
export function LoginPrompt({ what }: { what: string }) {
  return (
    <div className="flex flex-col items-center gap-2.5 py-12 text-center">
      <span className="grid h-11 w-11 place-items-center rounded-full bg-canvas text-ink-3">
        <KeyRound className="h-5 w-5" />
      </span>
      <p className="text-body font-semibold text-ink">本服务未启用账号体系</p>
      <p className="max-w-sm text-small leading-relaxed text-ink-3">
        {what}与登录账号绑定。当前部署没有开启用户登录，
        请让管理员开启后再来管理。
      </p>
    </div>
  );
}

/** 面板级加载占位（各面板取数时的统一形态）。 */
export function Loading({ what = "读取" }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-small text-ink-3">
      <Loader2 className="h-4 w-4 animate-spin" /> 正在{what}…
    </div>
  );
}

/** 空态占位（列表类分区的「暂无 XX」）。 */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-small text-ink-3">{children}</p>;
}

/** ISO 时间 → 北京时间的可读格式（本地时区，秒级）。 */
export function fmtTime(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours(),
  )}:${p(d.getMinutes())}`;
}

/** 相对时间（登录流水用，比绝对时间更易读）。 */
export function fmtAgo(ts: number): string {
  const diff = Date.now() - ts * 1000;
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h} 小时前`;
  return `${Math.floor(h / 24)} 天前`;
}
