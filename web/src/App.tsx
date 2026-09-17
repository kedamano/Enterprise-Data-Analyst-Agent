import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SidebarProvider } from "@/components/ui/sidebar";
import {
  ModalProvider,
  Modal,
  ModalBody,
  ModalContent,
} from "@/components/ui/animated-modal";
import { History, Settings2 } from "lucide-react";
import { SideRail } from "@/components/SideRail";
import { ConversationList } from "@/components/ConversationList";
import { Welcome } from "@/components/Welcome";
import { Composer } from "@/components/Composer";
import { ChatMessage } from "@/components/ChatMessage";
import { useLocalStorage, uid } from "@/lib/storage";
import { streamAnalyze, isTerminal, uploadAttachments, fetchHealth } from "@/lib/api";
import type { AgentEvent, HealthInfo } from "@/lib/api";
import { AuthError } from "@/lib/api";
import { AuthCentre } from "@/components/AuthCentre";
import { HistoryModal } from "@/components/RailPanels";
import { KnowledgeView } from "@/components/KnowledgeView";
import { FilesView } from "@/components/FilesView";
import { DataSourcesView } from "@/components/DataSourcesView";
import { SettingsView } from "@/components/SettingsView";
import { refreshAuth } from "@/lib/user";
import type { RailView } from "@/components/SideRail";
import type { Conversation, Message, Attachment } from "@/lib/types";

const STORE_KEY = "da_conversations_v1";

function DocsModal({ open, onClose }: { open: boolean; onClose?: () => void }) {
  return (
    <Modal open={open} onClose={onClose}>
      <ModalBody className="max-w-xl">
        <ModalContent>
          <h2 className="text-title font-semibold text-ink">使用指南</h2>
          <p className="mt-3 text-body leading-relaxed text-ink-3">
            这是一个企业级数据分析智能体的可视化控制台。你可以用自然语言提出业务问题，
            智能体会自动完成以下六阶段编排：
          </p>
          <ul className="mt-4 space-y-2 text-body text-ink-2">
            {[
              "意图理解 — 解析你的业务目标与约束",
              "制定计划 — 规划需要调用的工具与步骤",
              "执行取数 — 运行 SQL / Python / 知识检索等工具",
              "证据分析 — 对返回数据做统计与洞察",
              "质检反思 — 校验证据充分性与偏见",
              "生成报告 — 输出结构化、可读的分析结论",
            ].map((s) => (
              <li key={s} className="flex gap-2">
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
                {s}
              </li>
            ))}
          </ul>
          <p className="mt-4 text-small text-ink-3">
            所有工具均为只读 / 计算，不会对数据源产生写操作。
          </p>
        </ModalContent>
      </ModalBody>
    </Modal>
  );
}

/**
 * 页头运行状态徽章。
 *
 * 这里刻意**不写死**「已就绪」——模型降级或离线时那句话就是在骗人，
 * 而用户会据此误判结论的可信度。文案只陈述可观测事实：
 * 探活失败就说「状态未知」，降级就说「降级中」，不发明好听的措辞。
 */
/**
 * 把 /health 的探活结果翻成一句用户读得懂的状态。
 *
 * 抽成函数是因为同一个状态要在两处呈现（页头徽章 + 会话侧栏底部状态条），
 * 两处各写一遍就会对不上——页头说"降级中"、侧栏说"正常"是最糟的界面。
 */
function runtimeState(
  health: HealthInfo | null,
  failed: boolean,
): { label: string; tone: "neutral" | "ok" | "warn"; detail: string } {
  if (failed) {
    return {
      label: "模型状态未知",
      tone: "neutral",
      detail: "无法连接后端服务，页面功能可能不可用",
    };
  }
  if (health?.llm_degraded) {
    return {
      label: "模型降级中",
      tone: "warn",
      detail: "模型调用失败，已回退到降级通道，结论可信度可能下降",
    };
  }
  if (health?.mock_llm) {
    return {
      label: "模拟模式",
      tone: "warn",
      detail: "当前使用模拟模型，输出仅用于演示，不代表真实分析",
    };
  }
  if (health) {
    return {
      label: "模型正常",
      tone: "ok",
      detail: "模型响应正常，数据源已连接",
    };
  }
  return { label: "正在连接模型", tone: "neutral", detail: "" };
}

function RuntimeBadge({
  health,
  failed,
}: {
  health: HealthInfo | null;
  failed: boolean;
}) {
  const { label, tone, detail } = runtimeState(health, failed);

  const cls =
    tone === "ok"
      ? "border-verified bg-verified-soft text-verified"
      : tone === "warn"
        ? "border-attention bg-attention-soft text-attention"
        : "border-rule bg-canvas text-ink-3";
  const dot =
    tone === "ok" ? "bg-verified" : tone === "warn" ? "bg-attention" : "bg-ink-3";

  return (
    <span
      role="status"
      title={detail}
      className={`hidden items-center gap-1.5 rounded-full border px-2.5 py-1 text-micro font-medium sm:inline-flex ${cls}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
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
  // #1：鉴权开启时，401/503 触发全屏登录落地页；pendingRef 暂存待重试的发送参数。
  // 落地页和原来的 AuthGate 弹窗走同一套 pendingRef 重试链路，
  // 差异只在形态：弹窗 → 整页（双滑块）—— 见 AuthCentre。
  const [needAuth, setNeedAuth] = useState(false);
  // 主区域视图：chat（对话）/ knowledge / files / datasources
  // 知识库与文件库是有目录结构、需要大面积操作的重功能，用页面承载而非弹窗。
  const [view, setView] = useState<RailView>("chat");
  const [historyOpen, setHistoryOpen] = useState(false);
  // 页头的运行状态徽章要反映**真实**后端状态，不能写死一句「已就绪」——
  // 模型降级/离线时那句话就是在骗人。探活失败就退回中性表述，不谎报。
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthFailed, setHealthFailed] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    fetchHealth(ac.signal)
      .then((h) => {
        setHealth(h);
        setHealthFailed(false);
      })
      .catch(() => setHealthFailed(true));
    return () => ac.abort();
  }, []);

  // AUTH/02：启动时探一次"我是谁"。
  // 放在 App 而非设置页——令牌可能在别处失效，导航栏的用户头像要能反映真实登录态。
  useEffect(() => {
    void refreshAuth();
  }, []);

  const clearAllConversations = useCallback(() => {
    setConversations([]);
    setActiveId(null);
  }, [setConversations]);
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
          // #1：鉴权失败 → 存待重试参数，切到全屏登录落地页；用户填 key 后重试本轮
          pendingRef.current = { text, attachments };
          setNeedAuth(true);
          patchMessage(convId, botMsg.id, (m) => ({
            ...m,
            done: true,
            error: "需要 API Key 才能继续（请在登录页填写）",
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

  // #1：落地页登录成功（已写入 localStorage）→ 用暂存参数重试刚才被 401 拦截的分析
  const handleAuthed = useCallback(() => {
    setNeedAuth(false);
    const p = pendingRef.current;
    pendingRef.current = null;
    if (p) void send(p.text, p.attachments);
  }, [send]);

  return (
    <ModalProvider>
      <SidebarProvider>
        <div className="flex h-full w-full overflow-hidden bg-canvas text-ink">
          <SideRail
            view={view}
            onNavigate={setView}
            onNew={newConversation}
            onToggleList={() => setShowList((s) => !s)}
            showList={showList}
            onOpenDocs={() => setDocsOpen(true)}
            onOpenHistory={() => setHistoryOpen(true)}
          />

          <ConversationList
            conversations={conversations}
            activeId={activeId}
            open={showList}
            onSelect={(id) => {
              setActiveId(id);
              setView("chat");
            }}
            onNew={() => {
              newConversation();
              setView("chat");
            }}
            onDelete={deleteConversation}
            onToggle={() => setShowList((s) => !s)}
            onPick={(q) => {
              setView("chat");
              void send(q);
            }}
            runtime={runtimeState(health, healthFailed)}
          />

          <main className="relative flex min-w-0 flex-1 flex-col bg-canvas">
            {view !== "chat" ? (
              <div className="min-h-0 flex-1 overflow-hidden">
                {view === "knowledge" && <KnowledgeView />}
                {view === "files" && <FilesView />}
                {view === "datasources" && <DataSourcesView />}
                {view === "settings" && (
                  <SettingsView onClearAll={clearAllConversations} />
                )}
              </div>
            ) : (
              <>
                {/* 顶部 header：白色 + 细线分隔（h-52px 与会话侧栏头部齐平） */}
            <header className="flex h-[52px] shrink-0 items-center justify-between border-b border-rule bg-white/80 px-4 backdrop-blur sm:px-5">
              <div className="flex min-w-0 items-center gap-2.5">
                <img
                  src="/logo.png"
                  alt=""
                  width={28}
                  height={28}
                  draggable={false}
                  className="h-7 w-7 shrink-0 select-none rounded-control object-contain shadow-sm ring-1 ring-rule"
                />
                <div className="flex min-w-0 flex-col leading-tight">
                  <span className="truncate text-body font-semibold text-ink">
                    {active?.title || "企业数据分析智能体"}
                  </span>
                  <span className="truncate text-micro text-ink-3">
                    自然语言驱动的数据分析
                  </span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <RuntimeBadge health={health} failed={healthFailed} />
                <button
                  onClick={newConversation}
                  className="inline-flex items-center gap-1.5 rounded-control border border-rule bg-white px-3 py-1.5 text-small font-medium text-ink-2 shadow-sm transition hover:border-rule-strong hover:bg-canvas"
                >
                  <History className="h-4 w-4" /> 新对话
                </button>
                <button
                  onClick={() => setDocsOpen(true)}
                  className="grid h-8 w-8 place-items-center rounded-control border border-rule bg-white text-ink-3 shadow-sm transition hover:text-ink"
                  title="使用文档"
                  aria-label="使用文档"
                >
                  <Settings2 className="h-4 w-4" />
                </button>
              </div>
            </header>

            <div ref={scrollRef} className="flex-1 overflow-y-auto">
              {!active || active.messages.length === 0 ? (
                <Welcome
                  onPick={send}
                  onUpload={() => setView("files")}
                  onAddKnowledge={() => setView("knowledge")}
                  onDataSources={() => setView("datasources")}
                />
              ) : (
                <div className="mx-auto flex max-w-5xl flex-col gap-4 px-5 py-6">
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

            {/* 底部 composer —— 白底 + 阴影 + 顶部细线（宽度与消息流对齐，避免上下错位） */}
            <div className="shrink-0 border-t border-rule bg-white/80 px-4 py-3 backdrop-blur sm:px-5">
              <div className="mx-auto max-w-5xl">
                <Composer onSubmit={send} onStop={stop} streaming={streaming} />
              </div>
            </div>
              </>
            )}
          </main>
        </div>
      </SidebarProvider>

      <DocsModal open={docsOpen} onClose={() => setDocsOpen(false)} />
      <HistoryModal
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        conversations={conversations}
        onSelect={setActiveId}
        onDelete={deleteConversation}
      />
      {needAuth && (
        <AuthCentre onAuthed={handleAuthed} />
      )}
    </ModalProvider>
  );
}
