import { useState } from "react";
import {
  Modal,
  ModalBody,
  ModalContent,
} from "@/components/ui/animated-modal";
import { History, MessageSquare, Search, Trash2 } from "@/components/icons";
import type { Conversation } from "@/lib/types";

/**
 * 左侧导航的**弹窗**类面板。
 *
 * 注意分工：知识库 / 文件库 / 数据源 / 设置已经升级为**主区域页面**
 * （RailView 切换）——它们是有目录结构、需要大面积操作的重功能；
 * 这里只保留「轻量、看完即走」的一类：历史记录（本地会话）。
 * 弹窗与页面混用时，弹窗只承载不需要空间展开的内容。
 */

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function HistoryModal({
  open,
  onClose,
  conversations,
  onSelect,
  onDelete,
}: {
  open: boolean;
  onClose: () => void;
  conversations: Conversation[];
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const [q, setQ] = useState("");

  const list = [...conversations]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .filter((c) => {
      if (!q.trim()) return true;
      const t = q.trim().toLowerCase();
      return (
        c.title.toLowerCase().includes(t) ||
        c.messages.some((m) => m.text?.toLowerCase().includes(t))
      );
    });

  return (
    <Modal open={open} onClose={onClose}>
      <ModalBody className="max-w-lg">
        <ModalContent>
          <h2 className="flex items-center gap-2 text-title font-semibold text-ink">
            <History className="h-5 w-5 text-brand" /> 历史记录
          </h2>
          <p className="mt-2 text-body text-ink-3">
            共 {conversations.length} 个会话，本地保存（localStorage）。点击可打开，右侧可删除。
          </p>

          <div className="relative mt-4">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索会话标题或内容…"
              aria-label="搜索历史会话"
              className="w-full rounded-control border border-rule bg-white py-2 pl-9 pr-3 text-body text-ink outline-none focus:border-brand"
            />
          </div>

          {list.length === 0 ? (
            <p className="mt-6 text-center text-body text-ink-3">
              {conversations.length === 0 ? "还没有任何会话" : "没有匹配的会话"}
            </p>
          ) : (
            <ul className="mt-3 space-y-2">
              {list.map((c) => (
                <li
                  key={c.id}
                  className="group flex items-center gap-3 rounded-control border border-rule bg-white px-3 py-2.5 transition hover:border-brand hover:bg-brand-soft"
                >
                  <MessageSquare className="h-4 w-4 shrink-0 text-ink-3" />
                  <button
                    type="button"
                    onClick={() => {
                      onSelect(c.id);
                      onClose();
                    }}
                    className="min-w-0 flex-1 text-left"
                  >
                    <p className="truncate text-body font-medium text-ink">{c.title}</p>
                    <p className="truncate text-micro text-ink-3">
                      {c.messages.length} 条消息 · {formatTime(c.updatedAt)}
                    </p>
                  </button>
                  <button
                    type="button"
                    onClick={() => onDelete(c.id)}
                    aria-label={`删除会话 ${c.title}`}
                    className="grid h-8 w-8 shrink-0 place-items-center rounded-control text-ink-3 opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}
