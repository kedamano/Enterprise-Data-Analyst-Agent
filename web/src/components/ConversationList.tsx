import { motion, AnimatePresence } from "motion/react";
import { Plus, MessageSquare, Trash2, PanelLeftClose } from "lucide-react";
import type { Conversation } from "@/lib/types";

function timeAgo(ts: number): string {
  const d = Math.floor((Date.now() - ts) / 1000);
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

/**
 * 会话侧栏。
 *
 * 2026-09-15 紧凑化：面板 300→272px、会话项改为单行（标题 + 右侧时间，
 * 时间与删除按钮共用固定宽度，hover 不跳动）、移除底部纯装饰的
 * 「由 DeepSeek 驱动」卡片（占 76px 且与顶部标题重复）。
 * 目的是把垂直方向的空间还给会话内容本身，而不是留白。
 */
export function ConversationList({
  conversations,
  activeId,
  open,
  onSelect,
  onNew,
  onDelete,
  onToggle,
}: {
  conversations: Conversation[];
  activeId: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onToggle: () => void;
}) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.aside
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 272, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.25 }}
          className="hidden shrink-0 overflow-hidden border-r border-slate-200 bg-white md:block"
        >
          <div className="flex h-full w-[272px] flex-col">
            {/* 标题 + 收起（Logo 已由左侧细导航栏展示，这里不再重复） */}
            <div className="flex h-[52px] shrink-0 items-center justify-between border-b border-slate-200/70 px-3">
              <div className="flex min-w-0 flex-col leading-tight">
                <span className="truncate text-small font-semibold text-slate-900">
                  数据分析工作台
                </span>
                <span className="truncate text-micro text-slate-500">
                  Enterprise DA Agent
                </span>
              </div>
              <button
                onClick={onToggle}
                aria-label="收起会话列表"
                title="收起会话列表"
                className="grid h-7 w-7 shrink-0 place-items-center rounded-control text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
              >
                <PanelLeftClose className="h-[17px] w-[17px]" />
              </button>
            </div>

            <div className="shrink-0 px-2.5 pt-2.5">
              <button
                onClick={onNew}
                className="flex w-full items-center justify-center gap-1.5 rounded-control border border-indigo-200 bg-gradient-to-br from-indigo-500 to-violet-600 px-3 py-2 text-small font-medium text-white shadow-sm shadow-indigo-500/30 transition hover:from-indigo-600 hover:to-violet-700"
              >
                <Plus className="h-4 w-4" /> 新建对话
              </button>
            </div>

            <div className="flex shrink-0 items-center justify-between px-3.5 pt-3">
              <span className="text-micro font-medium uppercase tracking-wider text-slate-400">
                会话历史
              </span>
              <span className="text-micro tabular-nums text-slate-300">
                {conversations.length}
              </span>
            </div>

            <div className="mt-1.5 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
              {conversations.length === 0 && (
                <div className="mx-1.5 rounded-control border border-dashed border-slate-200 px-3 py-6 text-center text-small leading-relaxed text-slate-400">
                  还没有会话
                  <br />
                  在右侧直接提问即可开始
                </div>
              )}
              {conversations.map((c) => (
                <div
                  key={c.id}
                  onClick={() => onSelect(c.id)}
                  className={`group flex cursor-pointer items-center gap-2 rounded-control px-2 py-2 transition ${
                    c.id === activeId
                      ? "border border-indigo-200 bg-indigo-50/80 text-indigo-900 shadow-sm"
                      : "border border-transparent text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`}
                >
                  <MessageSquare
                    className={`h-4 w-4 shrink-0 ${
                      c.id === activeId ? "text-indigo-500" : "text-slate-400"
                    }`}
                  />
                  <span className="min-w-0 flex-1 truncate text-small font-medium leading-snug">
                    {c.title || "新对话"}
                  </span>
                  {/* 时间与删除按钮共用同一固定宽度（48px，容得下「9分钟前」），hover 切换不产生位移 */}
                  <span className="relative grid h-6 w-12 shrink-0 place-items-center">
                    <span className="absolute right-0 text-micro tabular-nums text-slate-400 transition group-hover:opacity-0">
                      {timeAgo(c.updatedAt).replace(" ", "")}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(c.id);
                      }}
                      aria-label="删除会话"
                      title="删除会话"
                      className="absolute right-0 grid h-6 w-6 place-items-center rounded-control text-slate-400 opacity-0 transition hover:bg-rose-50 hover:text-rose-600 group-hover:opacity-100"
                    >
                      <Trash2 className="h-[15px] w-[15px]" />
                    </button>
                  </span>
                </div>
              ))}
            </div>

            <div className="flex shrink-0 items-center gap-1.5 border-t border-slate-200/70 px-3.5 py-2 text-micro text-slate-400">
              <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
              由 DeepSeek 驱动 · 六阶段编排
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
