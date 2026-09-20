// 骨架屏原语：fetch pending 时模拟即将渲染的卡片/列表形态，避免闪白
// 用法：在 loading=true 分支渲染 <Skeleton /> 替代整片空白
import { cn } from "@/lib/utils";

export function SkeletonLine({
  className,
  style,
}: {
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      className={cn(
        "h-3 animate-pulse rounded-control bg-rule",
        className,
      )}
      style={style}
      aria-hidden
    />
  );
}

/** 通用卡片骨架：图像占位 + 2-3 行文本 */
export function SkeletonCard({ lines = 2 }: { lines?: number }) {
  return (
    <div className="rounded-panel border border-rule bg-white p-4">
      <div className="flex items-start gap-3">
        <div className="h-10 w-10 shrink-0 animate-pulse rounded-control bg-rule" aria-hidden />
        <div className="flex-1 space-y-2 pt-1">
          <SkeletonLine className="h-4 w-2/5" />
          {Array.from({ length: lines }).map((_, i) => (
            <SkeletonLine key={i} className={`w-${i % 2 ? "4/5" : "full"}`} style={{ width: i % 2 ? "80%" : "100%" }} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** 列表骨架：头像 + 主行 + 摘要行，重复 n 次 */
export function SkeletonList({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3" aria-hidden>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-panel border border-rule bg-white p-3">
          <div className="h-9 w-9 shrink-0 animate-pulse rounded-full bg-rule" />
          <div className="flex-1 space-y-2">
            <SkeletonLine className="h-3.5 w-1/3" />
            <SkeletonLine className="h-3 w-4/5" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** 表格骨架：表头行 + n 行数据 */
export function SkeletonTable({ rows = 5 }: { rows?: number }) {
  return (
    <div className="rounded-panel border border-rule bg-white" aria-hidden>
      <div className="flex gap-4 border-b border-rule bg-canvas/60 px-4 py-2.5">
        <SkeletonLine className="h-3 w-1/4" />
        <SkeletonLine className="h-3 w-1/4" />
        <SkeletonLine className="h-3 w-1/4" />
        <SkeletonLine className="h-3 w-1/4" />
      </div>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-4 border-b border-rule px-4 py-3 last:border-b-0">
          <SkeletonLine className="h-3 w-1/4" />
          <SkeletonLine className="h-3 w-1/4" />
          <SkeletonLine className="h-3 w-1/4" />
          <SkeletonLine className="h-3 w-1/4" />
        </div>
      ))}
    </div>
  );
}
