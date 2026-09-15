import { AlertTriangle, GitBranch, ShieldAlert } from "lucide-react";
import {
  iterationLabel,
  type AgentEvent,
  type IterationInfo,
  type QualityIssue,
} from "@/lib/api";

/**
 * E3/02 + E4/04 的 UI 呈现：把后端早已下发、前端此前没人消费的两个字段画出来。
 *
 * - `iteration`（仅 FINISH 帧）：本轮是「增量·下钻/改期/换粒度/筛选」还是全链重跑——
 *   分析师必须能一眼看出"这次只跑了目标阶段、没有重新取数"，否则会误信增量结果的覆盖面。
 * - `quality_issues`：门禁违规项。**BLOCK 必须最显眼**——它意味着"结论必然错"
 *   （如拿 join 放大后的结果求和），只是后端按既有设计没有把请求置为 FAILED。
 */

/** 后端只在 FINISH 帧下发 iteration；取最后一个非空者。 */
export function latestIteration(events?: AgentEvent[] | null): IterationInfo | null {
  if (!events) return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const it = events[i].iteration;
    if (it) return it;
  }
  return null;
}

/** 跨帧汇总质量门禁项，按 code 去重（同一条规则可能多帧重复下发）。 */
export function collectQualityIssues(events?: AgentEvent[] | null): QualityIssue[] {
  if (!events) return [];
  const byCode = new Map<string, QualityIssue>();
  for (const ev of events) {
    for (const q of ev.quality_issues ?? []) {
      if (q?.code && !byCode.has(q.code)) byCode.set(q.code, q);
    }
  }
  const rank: Record<string, number> = { BLOCK: 0, REPLAN: 1, ANNOTATE: 2 };
  return [...byCode.values()].sort(
    (a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9),
  );
}

function IssueRow({ issue }: { issue: QualityIssue }) {
  const tone =
    issue.severity === "BLOCK"
      ? "text-rose-700"
      : issue.severity === "REPLAN"
        ? "text-amber-700"
        : "text-slate-600";
  return (
    <li className={`flex items-start gap-1.5 ${tone}`}>
      <span className="mt-0.5 shrink-0 font-mono text-micro uppercase opacity-70">
        {issue.severity}
      </span>
      <span className="min-w-0">{issue.detail}</span>
    </li>
  );
}

export function RunBadges({ events }: { events?: AgentEvent[] | null }) {
  const iteration = latestIteration(events);
  const issues = collectQualityIssues(events);
  const degraded = Boolean(events?.some((e) => e.degraded));
  const hasBlock = issues.some((i) => i.severity === "BLOCK");

  if (!iteration && issues.length === 0 && !degraded) return null;

  return (
    <div className="mb-3 flex flex-col gap-2" data-testid="run-badges">
      <div className="flex flex-wrap items-center gap-2">
        {iteration && (
          <span
            aria-label={`增量执行：${iterationLabel(iteration.kind)}`}
            className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-micro font-medium text-indigo-700"
          >
            <GitBranch className="h-3 w-3" />
            增量 · {iterationLabel(iteration.kind)}
            {iteration.attempts && iteration.attempts > 1
              ? `（第 ${iteration.attempts} 次）`
              : ""}
          </span>
        )}
        {iteration && (
          // 增量轮不重新取数——把"基于上一结果"写明，避免误读为全量结论。
          <span className="text-micro text-slate-400">基于上一结果，未重新取数</span>
        )}
        {degraded && (
          <span
            aria-label="本轮为模板兜底"
            className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-micro font-medium text-amber-700"
          >
            <AlertTriangle className="h-3 w-3" />
            模板兜底（LLM 降级）
          </span>
        )}
      </div>

      {issues.length > 0 && (
        <div
          aria-label="数据质量提示"
          className={`rounded-panel border p-3 text-small ${
            hasBlock
              ? "border-rose-200 bg-rose-50"
              : "border-amber-200 bg-amber-50"
          }`}
        >
          <div
            className={`mb-1.5 flex items-center gap-1.5 font-medium ${
              hasBlock ? "text-rose-800" : "text-amber-800"
            }`}
          >
            <ShieldAlert className="h-3.5 w-3.5" />
            {hasBlock ? "数据质量：存在结论级问题" : "数据质量提示"}
          </div>
          <ul className="space-y-1">
            {issues.map((i) => (
              <IssueRow key={i.code} issue={i} />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
