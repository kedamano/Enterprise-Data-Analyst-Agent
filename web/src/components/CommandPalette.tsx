import { useEffect, useState } from "react";
import { Command } from "cmdk";

export interface CommandPaletteProps {
  onSwitchView: (v: string) => void;
  onToggleTheme: () => void;
  onFeedbackOpen: () => void;
  onNewConversation: () => void;
}

export function CommandPalette({
  onSwitchView,
  onToggleTheme,
  onFeedbackOpen,
  onNewConversation,
}: CommandPaletteProps) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <Command.Dialog
      open={open}
      onOpenChange={setOpen}
      label="命令面板"
      className="fixed left-1/2 top-1/4 z-[100] w-full max-w-lg -translate-x-1/2 rounded-panel border border-rule bg-white/95 shadow-2xl backdrop-blur"
    >
      <div className="border-b border-rule px-4 py-3">
        <Command.Input
          placeholder="搜索命令…"
          className="w-full bg-transparent text-body text-ink outline-none placeholder:text-ink-3"
        />
      </div>
      <Command.List className="max-h-80 overflow-y-auto px-2 py-2">
        <Command.Empty className="px-2 py-3 text-small text-ink-3">
          未找到匹配的命令
        </Command.Empty>

        <Command.Group heading="对话">
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onNewConversation();
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            新对话
          </Command.Item>
          <Command.Item
            onSelect={() => {
              setOpen(false);
              // 从 localStorage 取最近 5 个会话
              try {
                const raw = localStorage.getItem("da_conversations_v1");
                if (raw) {
                  const conversations = JSON.parse(raw) as { id: string; title: string }[];
                  const recent = conversations.slice(0, 5);
                  if (recent.length > 0) {
                    const event = new CustomEvent("cmdk:open-history", {
                      detail: recent,
                    });
                    window.dispatchEvent(event);
                  }
                }
              } catch {
                // ignore
              }
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            打开最近会话
          </Command.Item>
        </Command.Group>

        <Command.Group heading="视图">
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onSwitchView("chat");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            切换到 对话
          </Command.Item>
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onSwitchView("analytics");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            切换到 统计
          </Command.Item>
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onSwitchView("knowledge");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            切换到 知识库
          </Command.Item>
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onSwitchView("files");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            切换到 文件库
          </Command.Item>
        </Command.Group>

        <Command.Group heading="Job">
          <Command.Item
            onSelect={() => {
              setOpen(false);
              const event = new CustomEvent("cmdk:new-job");
              window.dispatchEvent(event);
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            新建 Job
          </Command.Item>
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onSwitchView("skills");
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            查看 Job 列表
          </Command.Item>
        </Command.Group>

        <Command.Group heading="设置">
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onToggleTheme();
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            主题切换
          </Command.Item>
        </Command.Group>

        <Command.Group heading="反馈">
          <Command.Item
            onSelect={() => {
              setOpen(false);
              onFeedbackOpen();
            }}
            className="flex cursor-pointer items-center gap-2 rounded-control px-3 py-2 text-body text-ink hover:bg-canvas"
          >
            发送反馈
          </Command.Item>
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}

export default CommandPalette;
