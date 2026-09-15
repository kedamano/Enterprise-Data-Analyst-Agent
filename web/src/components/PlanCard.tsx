import { Bot, Check, Circle } from "lucide-react";
import type { AgentEvent, ExecutionPlan } from "@/lib/api";

const TASK_LABEL: Record<string, string> = {
  business_analysis: "Business Analysis",
  sql: "SQL Generation",
  sql_optimization: "SQL Optimization",
  python: "Python Code",
  markdown_report: "Markdown Report",
  data_exploration: "Data Exploration",
  metric_definition: "Metric Definition",
  data_modeling: "Data Modeling",
  visualization: "Visualization",
  data_interpretation: "Data Interpretation",
  quick_answer: "Quick Answer",
  full_analysis: "Full Analysis",
};

const STAGE_ORDER = [
  "INIT",
  "UNDERSTAND",
  "PLAN",
  "EXECUTE",
  "ANALYZE",
  "REFLECT",
  "REPORT",
  "FINISH",
];

function stageRank(status: string): number {
  const i = STAGE_ORDER.indexOf(status);
  if (i >= 0) return i;
  if (status === "REPLAN") return 4.5;
  return 0;
}

function progressOf(
  events: AgentEvent[] | undefined,
  n: number,
): { done: number; finished: boolean } {
  if (!events || events.length === 0 || n === 0) return { done: 0, finished: false };
  const finished = events.some((e) => e.status === "FINISH");
  if (finished) return { done: n, finished: true };
  for (let i = events.length - 1; i >= 0; i--) {
    const p = events[i].workflow_progress;
    if (p && p.total === n) return { done: Math.min(p.done, n), finished: false };
  }
  const maxRank = Math.max(...events.map((e) => stageRank(e.status)));
  const ratio = Math.min(1, (maxRank + 1) / STAGE_ORDER.length);
  const done = Math.max(0, Math.min(n - 1, Math.floor(ratio * n)));
  return { done, finished };
}

function StepIcon({ state }: { state: "done" | "active" | "todo" }) {
  if (state === "done") {
    return <Check className="h-3.5 w-3.5 shrink-0 text-emerald-500" />;
  }
  if (state === "active") {
    return (
      <span className="h-3 w-3 shrink-0 animate-pulse rounded-full border-2 border-indigo-400" />
    );
  }
  return <Circle className="h-3.5 w-3.5 shrink-0 text-slate-300" />;
}

export function PlanCard({
  plan,
  events,
}: {
  plan?: ExecutionPlan | null;
  events?: AgentEvent[];
}) {
  if (!plan) return null;
  const label = TASK_LABEL[plan.task_type] ?? plan.task_type;
  const required = [
    plan.requires_sql && "SQL",
    plan.requires_python && "Python",
    plan.requires_data && "数据",
    plan.requires_report && "报告",
  ].filter(Boolean);

  const { done, finished } = progressOf(events, plan.workflow.length);

  return (
    <div className="rounded-panel border border-slate-200 bg-slate-50/80 px-3 py-2.5 text-small">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1 text-indigo-600">
          <Bot className="h-3.5 w-3.5" /> 任务识别
        </span>
        <span className="rounded-control border border-indigo-200 bg-indigo-50 px-2 py-0.5 font-medium text-indigo-700">
          {label}
        </span>
        <span className="ml-auto flex flex-wrap gap-1">
          {plan.deliverable.map((d) => (
            <span
              key={d}
              className="rounded-control border border-slate-200 bg-white px-1.5 py-0.5 text-micro text-slate-500"
            >
              {d}
            </span>
          ))}
        </span>
      </div>

      <div className="mb-1 text-micro font-medium uppercase tracking-wider text-slate-400">
        执行计划
      </div>
      <ol className="grid gap-0.5">
        {plan.workflow.map((step, i) => {
          const st: "done" | "active" | "todo" =
            i < done ? "done" : i === done && !finished ? "active" : "todo";
          return (
            <li
              key={step}
              className={`flex items-center gap-1.5 ${
                st === "todo" ? "text-slate-400" : "text-slate-700"
              }`}
            >
              <StepIcon state={st} />
              <span className={st === "done" ? "line-through decoration-slate-300" : ""}>
                {step}
              </span>
            </li>
          );
        })}
      </ol>

      {required.length > 0 && (
        <div className="mt-1.5 text-micro text-slate-400">
          需要：{required.join(" / ")}
        </div>
      )}
    </div>
  );
}
