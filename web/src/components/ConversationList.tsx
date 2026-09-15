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
          animate={{ width: 288, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.25 }}
          className="hidden shrink-0 overflow-hidden border-r border-slate-200 bg-white md:block"
        >
          <div className="flex h-full w-72 flex-col">
            {/* 标题 + 收起（Logo 已由左侧细导航栏展示，这里不再重复） */}
            <div className="flex items-center justify-between border-b border-slate-200/70 px-4 py-3">
              <div className="flex flex-col leading-tight">
                <span className="text-[13px] font-semibold text-slate-900">
                  数据分析工作台
                </span>
                <span className="text-[11px] text-slate-500">
                  Enterprise DA Agent
                </span>
              </div>
              <button
                onClick={onToggle}
                aria-label="收起会话列表"
                title="收起会话列表"
                className="grid h-7 w-7 place-items-center rounded-md text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
              >
                <PanelLeftClose className="h-4 w-4" />
              </button>
            </div>

            <div className="px-3 pt-3">
              <button
                onClick={onNew}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-indigo-200 bg-gradient-to-br from-indigo-500 to-violet-600 px-3 py-2 text-sm font-medium text-white shadow-sm shadow-indigo-500/30 transition hover:from-indigo-600 hover:to-violet-700"
              >
                <Plus className="h-4 w-4" /> 新建对话
              </button>
            </div>

            <div className="px-4 pt-4 text-[11px] font-medium uppercase tracking-wider text-slate-400">
              会话历史
            </div>

            <div className="mt-2 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
              {conversations.length === 0 && (
                <div className="mx-2 mt-2 rounded-xl border border-dashed border-slate-200 px-3 py-8 text-center text-xs text-slate-400">
                  还没有会话，开始提问吧
                </div>
              )}
              {conversations.map((c) => (
                <div
                  key={c.id}
                  onClick={() => onSelect(c.id)}
                  className={`group flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 transition ${
                    c.id === activeId
                      ? "border border-indigo-200 bg-indigo-50/80 text-indigo-900 shadow-sm"
                      : "border border-transparent text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`}
                >
                  <MessageSquare
                    className={`h-3.5 w-3.5 shrink-0 ${
                      c.id === activeId ? "text-indigo-500" : "text-slate-400"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] font-medium">
                      {c.title || "新对话"}
                    </p>
                    <p
                      className={`text-[10.5px] ${
                        c.id === activeId ? "text-indigo-400" : "text-slate-400"
                      }`}
                    >
                      {timeAgo(c.updatedAt)}
                    </p>
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(c.id);
                    }}
                    aria-label="删除会话"
                    title="删除会话"
                    className="grid h-6 w-6 place-items-center rounded text-slate-400 opacity-0 transition hover:bg-rose-50 hover:text-rose-600 group-hover:opacity-100"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>

            <div className="border-t border-slate-200/70 px-4 py-3">
              <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2">
                <span className="grid h-6 w-6 place-items-center rounded-md bg-gradient-to-br from-amber-300 to-orange-400 text-[11px] font-bold text-white">
                  AI
                </span>
                <div className="flex flex-col leading-tight">
                  <span className="text-[11.5px] font-medium text-slate-700">
                    由 DeepSeek 驱动
                  </span>
                  <span className="text-[10px] text-slate-400">
                    六阶段编排 · 全程可追溯
                  </span>
                </div>
              </div>
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
