import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SidebarProvider } from "@/components/ui/sidebar";
import {
  ModalProvider,
  Modal,
  ModalBody,
  ModalContent,
  useModal,
} from "@/components/ui/animated-modal";
import { Sparkles, History, Settings2 } from "lucide-react";
import { SideRail } from "@/components/SideRail";
import { ConversationList } from "@/components/ConversationList";
import { Welcome } from "@/components/Welcome";
import { Composer } from "@/components/Composer";
import { ChatMessage } from "@/components/ChatMessage";
import { useLocalStorage, uid } from "@/lib/storage";
import { streamAnalyze, isTerminal, uploadAttachments } from "@/lib/api";
import type { AgentEvent } from "@/lib/api";
import { AuthError } from "@/lib/api";
import { AuthGate } from "@/components/AuthGate";
import type { Conversation, Message, Attachment } from "@/lib/types";

const STORE_KEY = "da_conversations_v1";

function DocsModal({ open }: { open: boolean }) {
  const { setOpen } = useModal();
  useEffect(() => setOpen(open), [open, setOpen]);
  return (
    <Modal>
      <ModalBody className="max-w-xl">
        <ModalContent>
          <h2 className="text-xl font-semibold text-slate-900">使用指南</h2>
          <p className="mt-3 text-sm leading-relaxed text-slate-500">
            这是一个企业级数据分析智能体的可视化控制台。你可以用自然语言提出业务问题，
            智能体会自动完成以下六阶段编排：
          </p>
          <ul className="mt-4 space-y-2 text-sm text-slate-700">
            {[
              "意图理解 — 解析你的业务目标与约束",
              "制定计划 — 规划需要调用的工具与步骤",
              "执行取数 — 运行 SQL / Python / 知识检索等工具",
              "证据分析 — 对返回数据做统计与洞察",
              "质检反思 — 校验证据充分性与偏见",
              "生成报告 — 输出结构化、可读的分析结论",
            ].map((s) => (
              <li key={s} className="flex gap-2">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-indigo-400" />
                {s}
              </li>
            ))}
          </ul>
          <p className="mt-4 text-xs text-slate-400">
            所有工具均为只读 / 计算，不会对数据源产生写操作。
          </p>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

export default function App() {
  const [conversations, setConversations] = useLocalStorage<Conversation[]>(
    STORE_KEY,
    [],
  );
  const [activeId, setActiveId] = useState<string | null>(null);
  const [showList, setShowList] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [docsOpen, setDocsOpen] = useState(false);
  // #1：鉴权开启时，401/503 触发登录弹窗；pendingRef 暂存待重试的发送参数
  const [authOpen, setAuthOpen] = useState(false);
  const pendingRef = useRef<{ text: string; attachments: Attachment[] } | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const m = window.location.hash.match(/#conv=([\w-]+)/);
    if (m && conversations.some((c) => c.id === m[1])) {
      setActiveId(m[1]);
    }
  }, [conversations]);

  const active = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? null,
    [conversations, activeId],
  );

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [active?.messages]);

  const patchConversation = useCallback(
    (id: string, fn: (c: Conversation) => Conversation) => {
      setConversations((prev) => prev.map((c) => (c.id === id ? fn(c) : c)));
    },
    [setConversations],
  );

  const patchMessage = useCallback(
    (convId: string, msgId: string, fn: (m: Message) => Message) => {
      patchConversation(convId, (c) => ({
        ...c,
        updatedAt: Date.now(),
        messages: c.messages.map((m) => (m.id === msgId ? fn(m) : m)),
      }));
    },
    [patchConversation],
  );

  const newConversation = useCallback(() => {
    const id = uid();
    const conv: Conversation = {
      id,
      title: "新对话",
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    setConversations((prev) => [conv, ...prev]);
    setActiveId(id);
  }, [setConversations]);

  const deleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (activeId === id) setActiveId(null);
    },
    [activeId, setConversations],
  );

  const ensureActive = useCallback((): string => {
    if (activeId && conversations.some((c) => c.id === activeId))
      return activeId;
    const id = uid();
    const conv: Conversation = {
      id,
      title: "新对话",
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    setConversations((prev) => [conv, ...prev]);
    setActiveId(id);
    return id;
  }, [activeId, conversations, setConversations]);

  const send = useCallback(
    async (text: string, attachments: Attachment[] = []) => {
      const convId = ensureActive();

      // 清理 previewUrl —— 那是 blob URL，关掉页面就失效，存进 localStorage 也无意义
      const cleanedAttachments = attachments.map(({ previewUrl: _u, ...rest }) => rest);

      const userMsg: Message = {
        id: uid("m"),
        role: "user",
        text,
        attachments: cleanedAttachments.length > 0 ? cleanedAttachments : undefined,
      };
      const botMsg: Message = {
        id: uid("m"),
        role: "assistant",
        events: [],
        status: "INIT",
        done: false,
      };

      patchConversation(convId, (c) => ({
        ...c,
        title: c.messages.length === 0 ? text.slice(0, 24) || "附件分析" : c.title,
        updatedAt: Date.now(),
        messages: [...c.messages, userMsg, botMsg],
      }));

      // 先把文件真正传到后端（后端解析表格列名/行数/样例并绑定 session），
      // 再发起分析；这样 Planner 能拿到附件结构而不是只有一个文件名。
      let uploadHint = "";
      if (cleanedAttachments.length > 0) {
        const rawFiles = attachments
          .map((a) => a.file)
          .filter((f): f is File => Boolean(f));
        if (rawFiles.length > 0) {
          patchMessage(convId, botMsg.id, (m) => ({ ...m, status: "UPLOADING" }));
          const { ok, failed } = await uploadAttachments(rawFiles, convId);
          const parts: string[] = [];
          if (ok.length > 0) {
            parts.push(
              `已解析：${ok
                .map((r) =>
                  r.attachment.kind === "table"
                    ? `${r.attachment.name}(${r.attachment.rows} 行)`
                    : r.attachment.name,
                )
                .join("、")}`,
            );
          }
          if (failed.length > 0) {
            parts.push(`上传失败：${failed.map((f) => `${f.name}（${f.reason}）`).join("、")}`);
          }
          uploadHint = parts.join("；");
        }
      }

      // 兜底线索：即便文件没传成功，也把文件名写进 query，避免模型完全失去上下文
      const hint = uploadHint || null;
      const enrichedQuery =
        cleanedAttachments.length > 0
          ? `${text}\n\n[附件：${cleanedAttachments.map((a) => a.name).join("、")}]${hint ? `\n（${hint}）` : ""}`
          : text;

      const history = (
        conversations.find((c) => c.id === convId)?.messages ?? []
      )
        .filter((m) => m.text)
        .map((m) => ({ role: m.role, content: m.text! }));

      const controller = new AbortController();
      abortRef.current = controller;
      setStreaming(true);

      const appendEvent = (ev: AgentEvent) => {
        patchMessage(convId, botMsg.id, (m) => ({
          ...m,
          status: ev.status,
          objective: ev.objective ?? m.objective,
          events: [...(m.events ?? []), ev],
          done: isTerminal(ev.status),
          error:
            ev.status === "ERROR" || ev.status === "FAILED"
              ? ev.message
              : m.error,
          // CLARIFY/01：澄清不是错误，单独存结构化问题（卡片据此渲染）
          clarification:
            ev.status === "CLARIFY" ? ev.clarification ?? null : m.clarification,
          text: ev.status === "FINISH" ? ev.report ?? m.text : m.text,
        }));
      };

      try {
        await streamAnalyze(
          enrichedQuery,
          convId,
          history,
          appendEvent,
          controller.signal,
        );
      } catch (err) {
        if (err instanceof AuthError) {
          // #1：鉴权失败 → 存待重试参数，弹登录框；用户填 key 后重试本轮
          pendingRef.current = { text, attachments };
          setAuthOpen(true);
          patchMessage(convId, botMsg.id, (m) => ({
            ...m,
            done: true,
            error: "需要 API Key 才能继续（请在弹窗中填写）",
          }));
          return;
        }
        patchMessage(convId, botMsg.id, (m) => ({
          ...m,
          done: true,
          error:
            err instanceof DOMException && err.name === "AbortError"
              ? "已停止"
              : `连接失败：${(err as Error).message}`,
        }));
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [conversations, ensureActive, patchConversation, patchMessage],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // #1：登录成功（已写入 localStorage）→ 用暂存参数重试刚才被 401 拦截的分析
  const handleAuthed = useCallback(() => {
    setAuthOpen(false);
    const p = pendingRef.current;
    pendingRef.current = null;
    if (p) void send(p.text, p.attachments);
  }, [send]);

  return (
    <ModalProvider>
      <SidebarProvider>
        <div className="flex h-full w-full overflow-hidden bg-slate-50 text-slate-800">
          <SideRail
            onNew={newConversation}
            onToggleList={() => setShowList((s) => !s)}
            showList={showList}
            onOpenDocs={() => setDocsOpen(true)}
          />

          <ConversationList
            conversations={conversations}
            activeId={activeId}
            open={showList}
            onSelect={setActiveId}
            onNew={newConversation}
            onDelete={deleteConversation}
            onToggle={() => setShowList((s) => !s)}
          />

          <main className="relative flex min-w-0 flex-1 flex-col bg-slate-50">
            {/* 顶部 header：白色 + 细线分隔 */}
            <header className="flex items-center justify-between border-b border-slate-200/80 bg-white/80 px-5 py-3 backdrop-blur">
              <div className="flex items-center gap-2.5">
                <span className="grid h-7 w-7 place-items-center rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 shadow-sm shadow-indigo-500/30">
                  <Sparkles className="h-4 w-4 text-white" />
                </span>
                <div className="flex flex-col">
                  <span className="text-sm font-semibold leading-tight text-slate-900">
                    {active?.title || "企业数据分析智能体"}
                  </span>
                  <span className="text-[11px] text-slate-500">
                    数据分析 · 自然语言驱动六阶段编排
                  </span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="hidden items-center gap-1.5 rounded-full border border-emerald-200/80 bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 sm:inline-flex">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                  deepseek-chat 已就绪
                </span>
                <button
                  onClick={newConversation}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:border-slate-300 hover:bg-slate-50"
                >
                  <History className="h-3.5 w-3.5" /> 新对话
                </button>
                <button
                  onClick={() => setDocsOpen(true)}
                  className="grid h-8 w-8 place-items-center rounded-lg border border-slate-200 bg-white text-slate-500 shadow-sm transition hover:text-slate-800"
                  title="使用文档"
                  aria-label="使用文档"
                >
                  <Settings2 className="h-4 w-4" />
                </button>
              </div>
            </header>

            <div ref={scrollRef} className="flex-1 overflow-y-auto">
              {!active || active.messages.length === 0 ? (
                <Welcome onPick={send} />
              ) : (
                <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6 sm:px-6">
                  {active.messages.map((m) => (
                    <ChatMessage
                      key={m.id}
                      message={m}
                      sessionId={active.id}
                      onAnswer={(answer) => {
                        // CLARIFY/01：回答即发下一轮，后端会带着 pending 澄清继续跑
                        patchMessage(active.id, m.id, (msg) => ({
                          ...msg,
                          answered: true,
                        }));
                        void send(answer);
                      }}
                    />
                  ))}
                </div>
              )}
            </div>

            {/* 底部 composer —— 白底 + 阴影 + 顶部细线 */}
            <div className="border-t border-slate-200/80 bg-white/80 px-4 py-4 sm:px-6 backdrop-blur">
              <div className="mx-auto max-w-3xl">
                <Composer onSubmit={send} onStop={stop} streaming={streaming} />
              </div>
            </div>
          </main>
        </div>
      </SidebarProvider>

      <DocsModal open={docsOpen} />
      <AuthGate
        open={authOpen}
        onAuthed={handleAuthed}
        onCancel={() => setAuthOpen(false)}
      />
    </ModalProvider>
  );
}
