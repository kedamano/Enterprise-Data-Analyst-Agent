import { useState } from "react";
import type { ReactNode } from "react";
import { Timeline } from "@/components/ui/timeline";
import type { TimelineTone } from "@/components/ui/timeline";
import {
  CheckCircle2,
  Loader2,
  XCircle,
  Wrench,
  Sparkles,
  ChevronDown,
  ChevronUp,
  ListChecks,
} from "lucide-react";
import type { AgentEvent, StepInfo } from "@/lib/api";
import { stageLabel } from "@/lib/api";

function StepCard({ step }: { step: StepInfo }) {
  const ok = step.status === "SUCCESS";
  const fail = step.status === "FAILED" || step.status === "ERROR";
  return (
    <div className="rounded-control border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-small leading-snug">
      <div className="flex items-center gap-2">
        <Wrench className="h-4 w-4 text-indigo-500" />
        <span className="font-medium text-slate-800">{step.tool}</span>
        {ok && <CheckCircle2 className="h-4 w-4 text-emerald-500" />}
        {fail && <XCircle className="h-4 w-4 text-rose-500" />}
        {!ok && !fail && (
          <Loader2 className="h-4 w-4 animate-spin text-indigo-500" />
        )}
        {typeof step.execution_time_ms === "number" && (
          <span className="ml-auto text-small text-slate-400 tabular-nums">
            {(step.execution_time_ms / 1000).toFixed(2)}s
          </span>
        )}
      </div>
      {step.digest && (
        <p className="mt-1.5 font-mono text-small leading-snug text-slate-500 line-clamp-3">
          {step.digest}
        </p>
      )}
      {step.error && (
        <p className="mt-1.5 text-small leading-snug text-rose-600">{step.error}</p>
      )}
    </div>
  );
}

export function StageTimeline({
  events,
  defaultExpanded,
}: {
  events: AgentEvent[];
  defaultExpanded?: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded ?? false);

  const execEvents = events.filter((e) => e.status === "EXECUTE" && e.step);
  const execTools = execEvents.map((e) => e.step?.tool).filter(Boolean) as string[];
  const successCount = execEvents.filter((e) => e.step?.status === "SUCCESS").length;
  const failed = events.some(
    (e) =>
      e.status === "ERROR" ||
      e.status === "FAILED" ||
      e.step?.status === "FAILED" ||
      e.step?.status === "ERROR",
  );
  const finished = events.some((e) => e.status === "FINISH");
  // CLARIFY/01：澄清是终止态（等用户回答），既不是失败也不该显示成"仍在运行"
  const clarified = events.some((e) => e.status === "CLARIFY");
  const running = !finished && !failed && !clarified && events.length > 0;

  // 折叠态
  if (!expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="flex w-full items-center gap-2.5 rounded-control border border-slate-200 bg-slate-50 px-3.5 py-2.5 text-left transition hover:bg-slate-100"
      >
        <ListChecks className="h-[18px] w-[18px] shrink-0 text-slate-400" />
        <span className="flex-1 truncate text-small text-slate-600">
          {running ? (
            <span className="inline-flex items-center gap-2">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-500" />
              执行中 ·{" "}
              {events[events.length - 1]?.status
                ? stageLabel(events[events.length - 1].status)
                : "..."}
            </span>
          ) : failed ? (
            <span className="text-rose-600">
              执行异常 · {successCount}/{execTools.length} 个工具成功
            </span>
          ) : (
            <span>
              执行完成 · {execTools.length} 个工具
              {execTools.length > 0 && (
                <span className="ml-1.5 text-slate-400">({execTools.join(" / ")})</span>
              )}
            </span>
          )}
        </span>
        <ChevronDown className="h-[18px] w-[18px] shrink-0 text-slate-400" />
      </button>
    );
  }

  const data = events.map((ev, i) => {
    const isExec = ev.status === "EXECUTE" && ev.step;
    const terminal =
      ev.status === "FINISH" || ev.status === "ERROR" || ev.status === "FAILED";
    const isRunning = !terminal && i === events.length - 1;

    let content: ReactNode;
    if (isExec && ev.step) {
      content = <StepCard step={ev.step} />;
    } else if (ev.status === "UNDERSTAND" && ev.objective) {
      content = (
        <p className="rounded-control border border-slate-200 bg-slate-50 px-3.5 py-2 text-small leading-snug text-slate-600">
          <span className="text-slate-400">业务目标 · </span>
          {ev.objective}
        </p>
      );
    } else if (ev.status === "PLAN") {
      content = (
        <p className="rounded-control border border-slate-200 bg-slate-50 px-3 py-1.5 text-small leading-snug text-slate-500">
          <Sparkles className="mr-1 inline h-3 w-3 -translate-y-px text-indigo-500" />
          已生成分析计划，将依次调用工具取证。
        </p>
      );
    } else if (ev.status === "REPORT") {
      content = (
        <p className="rounded-control border border-indigo-200 bg-indigo-50 px-3.5 py-2 text-small leading-snug text-indigo-700">
          正在撰写业务报告…
        </p>
      );
    } else if (terminal) {
      content = (
        <p className="rounded-control border border-slate-200 bg-slate-50 px-3.5 py-2 text-small leading-snug text-slate-500">
          {ev.status === "FINISH" ? "分析完成，报告已生成。" : "流程异常终止。"}
        </p>
      );
    } else {
      content = (
        <p className="text-small leading-snug text-slate-400">
          {stageLabel(ev.status)}…
        </p>
      );
    }

    const title =
      ev.status === "EXECUTE" && ev.step?.tool
        ? `执行 · ${ev.step.tool}`
        : stageLabel(ev.status);

    let tone: TimelineTone = "idle";
    if (ev.status === "ERROR" || ev.status === "FAILED") {
      tone = "error";
    } else if (ev.step?.status === "FAILED" || ev.step?.status === "ERROR") {
      tone = "error";
    } else if (isRunning) {
      tone = "active";
    } else if (ev.status === "REPORT") {
      tone = "warn";
    } else if (ev.status === "CLARIFY") {
      tone = "warn";
    } else if (ev.status === "FINISH") {
      tone = "success";
    } else if (
      ev.status === "EXECUTE" &&
      ev.step?.status === "SUCCESS"
    ) {
      tone = "success";
    } else if (["INIT", "UNDERSTAND", "PLAN", "ANALYZE", "REFLECT"].includes(ev.status)) {
      tone = "success";
    }

    return { title, content, tone };
  });

  return (
    <div>
      <div className="mb-2.5 flex items-center justify-between">
        <span className="text-small font-medium uppercase tracking-wider text-slate-400">
          执行过程
        </span>
        <button
          onClick={() => setExpanded(false)}
          className="inline-flex items-center gap-1 rounded-control border border-slate-200 bg-white px-2.5 py-1.5 text-small text-slate-500 transition hover:border-slate-300 hover:text-slate-800"
        >
          收起 <ChevronUp className="h-3.5 w-3.5" />
        </button>
      </div>
      <Timeline data={data} />
    </div>
  );
}
