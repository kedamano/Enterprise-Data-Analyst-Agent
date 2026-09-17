import { initials } from "@/lib/user";
import type { AuthUser } from "@/lib/user";

/**
 * 用户头像。无头像时退化为「首字 + 稳定配色」的字母头像。
 *
 * 配色由 id 哈希决定——同一个人每次渲染都是同一个颜色，不会闪成随机色。
 * 也刻意不用 `index` 或随机数：那会让同一个人在列表里每次刷新换一种颜色。
 */
const PALETTE = [
  "bg-brand",
  "bg-brand",
  "bg-brand",
  "bg-verified",
  "bg-attention",
  "bg-danger",
  "bg-teal-500",
  "bg-fuchsia-500",
];

function paletteOf(seed: string): string {
  let h = 0;
  for (let i = 0; i < seed.length; i += 1) {
    h = (h * 31 + seed.charCodeAt(i)) | 0;
  }
  return PALETTE[Math.abs(h) % PALETTE.length];
}

const SIZES = {
  xs: "h-7 w-7 text-micro",
  sm: "h-8 w-8 text-small",
  md: "h-10 w-10 text-body",
  lg: "h-16 w-16 text-title",
  xl: "h-24 w-24 text-display",
} as const;

export type AvatarSize = keyof typeof SIZES;

export function Avatar({
  user,
  size = "md",
  className = "",
}: {
  user: AuthUser | null;
  size?: AvatarSize;
  className?: string;
}) {
  const base = `shrink-0 select-none overflow-hidden rounded-full ${SIZES[size]} ${className}`;
  const src = user?.avatar || "";
  if (src) {
    return (
      <img
        src={src}
        alt={user?.display_name || user?.username || "头像"}
        draggable={false}
        className={`${base} object-cover ring-1 ring-rule`}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className={`${base} grid place-items-center font-semibold text-white ${paletteOf(
        user?.id || "anonymous",
      )}`}
    >
      {initials(user)}
    </span>
  );
}
