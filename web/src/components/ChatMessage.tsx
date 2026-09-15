import { useState } from "react";
import { motion } from "motion/react";
import { Copy, Check, AlertTriangle, Bot, Download, FileText, FileCode2, Image as ImageIcon } from "lucide-react";
import { Report } from "./Report";
import { PlanCard } from "./PlanCard";
import { ClarifyCard } from "./ClarifyCard";
import { StageTimeline } from "./StageTimeline";
import { stageLabel } from "@/lib/api";
import { ShareBar } from "./ShareBar";
import { RunBadges } from "./RunBadges";
import { MetricCards, latestMetrics } from "./MetricCards";
import { ExportPreview } from "./ExportPreview";
import type { Message, Attachment } from "@/lib/types";

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function extIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["csv", "tsv", "xlsx", "xls"].includes(ext)) return <FileText className="h-3 w-3" />;
  if (["json", "py", "sql", "md", "log"].includes(ext)) return <FileCode2 className="h-3 w-3" />;
  return <FileText className="h-3 w-3" />;
}

function AttachmentStrip({ attachments }: { attachments: Attachment[] }) {
  if (attachments.length === 0) return null;
  return (
    <div className="mb-1.5 flex flex-wrap gap-1.5">
      {attachments.map((a) => (
        <div
          key={a.id}
          className="inline-flex max-w-[220px] items-center gap-1 rounded-md bg-white/15 px-1.5 py-1 text-[11.5px] text-white"
          title={`${a.name} · ${fmtSize(a.size)}`}
        >
          {a.kind === "image" ? (
            <ImageIcon className="h-3 w-3 shrink-0" />
          ) : (
            <span className="shrink-0">{extIcon(a.name)}</span>
          )}
          <span className="truncate font-medium">{a.name}</span>
          <span className="shrink-0 text-white/60">{fmtSize(a.size)}</span>
        </div>
      ))}
    </div>
  );
}

function StatusPill({ status, done }: { status?: string; done?: boolean }) {
  if (done) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11.5px] font-medium text-emerald-700">
        <Check className="h-3 w-3" /> 已完成
      </span>
    );
  }
  if (!status) return null;
  const busy = status !== "FINISH" && status !== "ERROR" && status !== "FAILED";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11.5px] font-medium ${
        busy
          ? "border border-indigo-200 bg-indigo-50 text-indigo-700"
          : "border border-rose-200 bg-rose-50 text-rose-700"
      }`}
    >
      {busy && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />
      )}
      {stageLabel(status)}…
    </span>
  );
}

export function ChatMessage({ message, sessionId, onAnswer }: {
  message: Message;
  /** E5/03：会话 id（导出交付包用） */
  sessionId?: string;
  /** CLARIFY/01：提交澄清回答（App 负责发下一轮请求） */
  onAnswer?: (answer: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  if (message.role === "user") {
    const attachments = message.attachments ?? [];
    return (
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex justify-end"
      >
        <div className="max-w-[78%] rounded-2xl rounded-br-md bg-gradient-to-br from-indigo-500 to-violet-600 px-4 py-2.5 text-[14.5px] leading-relaxed text-white shadow-lg shadow-indigo-500/20">
          {attachments.length > 0 && <AttachmentStrip attachments={attachments} />}
          {message.text && <div className="whitespace-pre-wrap break-words">{message.text}</div>}
        </div>
      </motion.div>
    );
  }

  const copyReport = async () => {
    if (!message.text) return;
    try {
      await navigator.clipboard.writeText(message.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* ignore */
    }
  };

  const isDone = message.done;
  const hasReport = isDone && message.text;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex gap-3"
    >
      <div className="mt-1 grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-md shadow-indigo-500/20">
        <Bot className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-900">
            数据分析智能体
          </span>
          <StatusPill status={message.status} done={message.done} />
        </div>

        {message.error && (
          <div className="mb-3 flex items-start gap-2 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{message.error}</span>
          </div>
        )}

        {message.clarification && (
          <ClarifyCard
            clarification={message.clarification}
            answered={message.answered || message.status !== "CLARIFY"}
            onAnswer={onAnswer}
          />
        )}

        <PlanCard
          plan={message.events?.find((e) => e.intent)?.intent}
          events={message.events}
        />

        {/* E3/E4：增量徽标 + 质量门禁披露（后端早已下发，此前无人消费） */}
        <RunBadges events={message.events} />

        {/* D51：指标卡（后端归一化后经 FINISH 帧下发；无指标则整块不渲染） */}
        <MetricCards metrics={latestMetrics(message.events)} />

        {hasReport && (
          <div className="group mb-3 rounded-2xl border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-200/50">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wider text-slate-500">
                分析报告
              </span>
              <button
                onClick={copyReport}
                aria-label="复制报告"
                className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-500 transition hover:border-slate-300 hover:text-slate-800"
              >
                {copied ? (
                  <>
                    <Check className="h-3.5 w-3.5 text-emerald-500" /> 已复制
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" /> 复制
                  </>
                )}
              </button>
              {sessionId && (
                // E5/03：一次拿走交付包（报告 + SQL + 数据 + 溯源）
                <a
                  href={`/api/v1/chat/analyze/export/${sessionId}?format=zip`}
                  aria-label="导出交付包"
                  className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs text-slate-500 transition hover:border-slate-300 hover:text-slate-800"
                >
                  <Download className="h-3.5 w-3.5" /> 导出
                </a>
              )}
              {sessionId && (
                // #6 协作骨架：分享 / 评论 / 权限（后端端点待接入）
                <ShareBar sessionId={sessionId} />
              )}
            </div>
            <Report raw={message.text ?? ""} />
            {/* D51：#7 导出预览 —— 打包前先说清楚包里有什么（与真实 zip 同源） */}
            {sessionId && <ExportPreview sessionId={sessionId} />}
          </div>
        )}

        {message.events && message.events.length > 0 && (
          <div className="mb-3 rounded-xl border border-slate-200/80 bg-white px-3 py-3 shadow-sm shadow-slate-200/50">
            <StageTimeline events={message.events} defaultExpanded={!isDone} />
          </div>
        )}

        {!message.done && !message.events?.length && !message.error && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <span className="h-2 w-2 animate-pulse rounded-full bg-indigo-400" />
            正在连接智能体…
          </div>
        )}
      </div>
    </motion.div>
  );
}
