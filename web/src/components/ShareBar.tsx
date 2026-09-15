import { useState } from "react";
import { Share2, MessageSquare, ShieldCheck } from "lucide-react";

/**
 * #6 协作骨架 v1（占位 + 最小可用）：
 * - 分享：生成只读分享链接（后端 /api/v1/share 待建）
 * - 评论：按会话的评论线程（后端待建）
 * - 权限：查看当前会话的数据权限范围（接 #1 的 Principal 范围）
 *
 * 当前为前端骨架：交互埋点已就绪，后端端点接好后即可点亮。
 */
export function ShareBar({ sessionId }: { sessionId: string | null }) {
  const [note, setNote] = useState<string | null>(null);

  const share = () => {
    const link = `${location.origin}/ui#conv=${sessionId}&share=1`;
    try {
      void navigator.clipboard?.writeText(link);
      setNote("分享链接已复制到剪贴板（后端持久化待接入）");
    } catch {
      setNote(link);
    }
  };
  const comment = () => setNote("评论线程：后端 /api/v1/comment 待接入");
  const permission = () => setNote("数据权限：接 #1 Principal 范围（allowed_tables / row_filters）");

  const btn =
    "inline-flex items-center gap-1 rounded-control border border-slate-200 bg-white px-2 py-1 text-small text-slate-500 transition hover:border-slate-300 hover:text-slate-800";

  return (
    <div className="flex items-center gap-1.5">
      <button onClick={share} className={btn} aria-label="分享">
        <Share2 className="h-3.5 w-3.5" /> 分享
      </button>
      <button onClick={comment} className={btn} aria-label="评论">
        <MessageSquare className="h-3.5 w-3.5" /> 评论
      </button>
      <button onClick={permission} className={btn} aria-label="权限">
        <ShieldCheck className="h-3.5 w-3.5" /> 权限
      </button>
      {note && (
        <span className="ml-1 text-micro text-slate-400" role="status">
          {note}
        </span>
      )}
    </div>
  );
}
