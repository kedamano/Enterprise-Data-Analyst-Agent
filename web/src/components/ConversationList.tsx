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
/**
 * 空态里给出的两条可直接点击的示例。
 * 空态如果只是描述现状（"还没有会话"），用户读完仍然不知道下一步该点哪里；
 * 给两个能立刻跑起来的问题，空态才是一个出口而不是一句话。
 * 只用两条、且挑最短的——侧栏只有 272px，堆多了会挤成两行。
 */
const EMPTY_STATE_EXAMPLES = [
  "查询 orders 表的总行数",
  "对比各区域营收表现",
];

export function ConversationList({
  conversations,
  activeId,
  open,
  onSelect,
  onNew,
  onDelete,
  onToggle,
  onPick,
  runtime,
}: {
  conversations: Conversation[];
  activeId: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onToggle: () => void;
  onPick?: (q: string) => void;
  runtime?: { label: string; tone: "neutral" | "ok" | "warn" };
}) {
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.aside
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 272, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.25 }}
          className="hidden shrink-0 overflow-hidden border-r border-rule bg-white md:block"
        >
          <div className="flex h-full w-[272px] flex-col">
            {/* 标题 + 收起（Logo 已由左侧细导航栏展示，这里不再重复） */}
            <div className="flex h-[52px] shrink-0 items-center justify-between border-b border-rule px-3">
              <div className="flex min-w-0 flex-col leading-tight">
                <span className="truncate text-small font-semibold text-ink">
                  数据分析工作台
                </span>
                <span className="truncate text-micro text-ink-3">
                  Enterprise DA Agent
                </span>
              </div>
              <button
                onClick={onToggle}
                aria-label="收起会话列表"
                title="收起会话列表"
                className="grid h-7 w-7 shrink-0 place-items-center rounded-control text-ink-3 transition hover:bg-canvas hover:text-ink-2"
              >
                <PanelLeftClose className="h-4 w-4" />
              </button>
            </div>

            <div className="shrink-0 px-2.5 pt-2.5">
              <button
                onClick={onNew}
                className="flex w-full items-center justify-center gap-1.5 rounded-control bg-brand px-3 py-2 text-small font-medium text-white transition hover:bg-brand-hover"
              >
                <Plus className="h-4 w-4" /> 新建对话
              </button>
            </div>

            <div className="flex shrink-0 items-center justify-between px-3.5 pt-3">
              <span className="text-micro font-medium text-ink-3">
                会话历史
              </span>
              <span className="text-micro tabular-nums text-ink-3">
                {conversations.length}
              </span>
            </div>

            <div className="mt-1.5 min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
              {conversations.length === 0 && (
                <div className="mx-1.5 rounded-control border border-dashed border-rule px-3 py-4">
                  <p className="text-center text-small text-ink-3">还没有会话</p>
                  {onPick && (
                    <>
                      <p className="mt-1 text-center text-micro text-ink-3">
                        点下面任意一条即可开始
                      </p>
                      <div className="mt-2.5 space-y-1.5">
                        {EMPTY_STATE_EXAMPLES.map((ex) => (
                          <button
                            key={ex}
                            type="button"
                            onClick={() => onPick(ex)}
                            className="w-full truncate rounded-control border border-rule bg-white px-2.5 py-2 text-left text-small text-ink-2 transition hover:border-brand hover:bg-brand-soft hover:text-brand"
                          >
                            {ex}
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              )}
              {conversations.map((c) => (
                <div
                  key={c.id}
                  onClick={() => onSelect(c.id)}
                  className={`group flex cursor-pointer items-center gap-2 rounded-control px-2 py-2 transition ${
                    c.id === activeId
                      ? "border border-rule-strong bg-brand-soft text-brand shadow-sm"
                      : "border border-transparent text-ink-2 hover:bg-canvas hover:text-ink"
                  }`}
                >
                  <MessageSquare
                    className={`h-4 w-4 shrink-0 ${
                      c.id === activeId ? "text-brand" : "text-ink-3"
                    }`}
                  />
                  <span className="min-w-0 flex-1 truncate text-small font-medium leading-snug">
                    {c.title || "新对话"}
                  </span>
                  {/* 时间与删除按钮共用同一固定宽度（48px，容得下「9分钟前」），hover 切换不产生位移 */}
                  <span className="relative grid h-6 w-12 shrink-0 place-items-center">
                    <span className="absolute right-0 text-micro tabular-nums text-ink-3 transition group-hover:opacity-0">
                      {timeAgo(c.updatedAt).replace(" ", "")}
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onDelete(c.id);
                      }}
                      aria-label="删除会话"
                      title="删除会话"
                      className="absolute right-0 grid h-6 w-6 place-items-center rounded-control text-ink-3 opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </span>
                </div>
              ))}
            </div>

            {/* 底部状态条只陈述一件真实的事：模型当前什么状态。
                原先写死「由 DeepSeek 驱动 · 六阶段编排」——模型名写死就会说谎
                （换模型后界面还在说 DeepSeek），「六阶段编排」是流程描述不是状态，
                用中圆点和状态串在一起只是模板化写法。 */}
            <div className="flex shrink-0 items-center gap-1.5 border-t border-rule px-3.5 py-2 text-micro text-ink-3">
              <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  runtime?.tone === "ok"
                    ? "bg-verified"
                    : runtime?.tone === "warn"
                      ? "bg-attention"
                      : "bg-ink-3"
                }`}
              />
              {runtime?.label ?? "正在连接模型"}
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
