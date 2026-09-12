// 与后端 SSE 流式接口对接的客户端与类型定义

import { AuthError, authHeaders, maybeAuthError } from "@/lib/auth";

export interface StepInfo {
  step_id?: string;
  tool?: string;
  status?: string;
  execution_time_ms?: number;
  attempts?: number;
  error?: string | null;
  error_class?: string | null;
  artifacts?: number;
  digest?: string;
}

export interface ExecutionPlan {
  task_type: string;
  deliverable: string[];
  requires_data?: boolean;
  requires_sql?: boolean;
  requires_python?: boolean;
  requires_report?: boolean;
  workflow: string[];
}

export interface WorkflowProgress {
  done: number;
  total: number;
}

export interface Clarification {
  questions: string[];
  assumptions?: string[];
  objective?: string | null;
}

/** E3/02：增量轮信息（后端 FINISH 帧下发；全链轮为 null）。 */
export interface IterationInfo {
  /** date_change | granularity | drilldown | filter | generic */
  kind: string;
  stages?: string[];
  skips?: string[];
  attempts?: number;
  guard?: Record<string, unknown> | null;
}

/** E4/04 + E5：质量门禁违规项（severity=BLOCK 表示结论必然错）。 */
export interface QualityIssue {
  code: string;
  /** ANNOTATE（披露）| REPLAN（可补查）| BLOCK（结论必然错） */
  severity: string;
  detail: string;
  metric?: string | null;
  fixable?: boolean;
}

export interface AgentEvent {
  status: string;
  message: string;
  /** CLARIFY/01：需要用户回答的问题（status=CLARIFY 时非空） */
  clarification?: Clarification | null;
  objective?: string | null;
  intent?: ExecutionPlan | null;
  mode?: string | null;
  /** E3/02：本轮是否为增量执行（下钻/改期/换粒度/筛选） */
  iteration?: IterationInfo | null;
  /** E4/04 + E5：本轮触发的质量门禁项 */
  quality_issues?: QualityIssue[] | null;
  /** DEGRADE/01：本轮是否发生 LLM 降级（mock 兜底） */
  degraded?: boolean | null;
  workflow_progress?: WorkflowProgress | null;
  tool?: string | null;
  last_result_ok?: boolean | null;
  step?: StepInfo | null;
  report?: string | null;
}

/** E3/02：增量类型的用户可读文案（新增一类时只改这里）。 */
export function iterationLabel(kind: string | null | undefined): string {
  return (
    {
      date_change: "改期",
      granularity: "换粒度",
      drilldown: "下钻",
      filter: "筛选",
      generic: "增量",
    }[kind ?? ""] ?? "增量"
  );
}

export type StreamHandler = (ev: AgentEvent) => void;

const STAGE_ORDER = [
  "INIT",
  "UNDERSTAND",
  "PLAN",
  "EXECUTE",
  "ANALYZE",
  "REFLECT",
  "REPLAN",
  "REPORT",
  "CLARIFY",
  "FINISH",
];

export function stageLabel(status: string): string {
  return (
    {
      UPLOADING: "解析上传的附件",
      INIT: "初始化",
      UNDERSTAND: "解析业务意图",
      PLAN: "制定分析计划",
      EXECUTE: "执行工具获取数据",
      ANALYZE: "分析证据",
      REFLECT: "质检与反思",
      REPLAN: "证据不足，重新规划",
      REPORT: "生成业务报告",
      CLARIFY: "需要澄清",
      FINISH: "完成",
      ERROR: "出错",
      FAILED: "失败",
    }[status] ?? status
  );
}

export function isTerminal(status: string): boolean {
  // CLARIFY 也是终止态：本轮结束等用户回答。漏掉它界面会永久转圈（无后续事件）。
  return (
    status === "FINISH" ||
    status === "ERROR" ||
    status === "FAILED" ||
    status === "CLARIFY"
  );
}

export async function streamAnalyze(
  query: string,
  sessionId: string | null,
  history: { role: string; content: string }[],
  onEvent: StreamHandler,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api/v1/chat/analyze/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({
      query,
      session_id: sessionId,
      stream: true,
      history: history.slice(-12),
    }),
    signal,
  });
  if (!res.ok || !res.body) {
    const authErr = maybeAuthError(res, `后端返回 HTTP ${res.status}`);
    if (authErr) throw authErr;
    throw new Error(`后端返回 HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      try {
        onEvent(JSON.parse(payload) as AgentEvent);
      } catch {
        /* 忽略无法解析的帧 */
      }
    }
  }
}

export { STAGE_ORDER };

// ---------------------------------------------------------------- 附件上传

export interface UploadedAttachment {
  name: string;
  kind: string;
  rows?: number | null;
  columns?: string[] | null;
  sample?: Record<string, unknown>[] | null;
  excerpt?: string | null;
  bytes?: number;
}

export interface UploadResult {
  ok: boolean;
  session_id: string;
  attachment: UploadedAttachment;
  hint?: string | null;
}

/**
 * 把单个文件 POST 到后端 /attachments/upload，绑定到 session。
 * 后端会按类型解析（表格 → 列名/行数/样例；文本 → 摘录），
 * 之后 /chat/analyze 会自动带上这些信息作为上下文。
 */
export async function uploadAttachment(
  file: File,
  sessionId: string,
  signal?: AbortSignal,
): Promise<UploadResult> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  fd.append("session_id", sessionId);
  const res = await fetch("/api/v1/attachments/upload", {
    method: "POST",
    body: fd,
    headers: { ...authHeaders() },
    signal,
  });
  if (!res.ok) {
    const authErr = maybeAuthError(res, `上传失败 HTTP ${res.status}`);
    if (authErr) throw authErr;
    let detail = `上传失败 HTTP ${res.status}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j?.detail) detail = j.detail;
    } catch {
      /* 保持默认文案 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as UploadResult;
}

/** 上传多个文件，返回每个的结果 + 各自的错误（不因单个失败中断整批）。 */
export async function uploadAttachments(
  files: File[],
  sessionId: string,
  signal?: AbortSignal,
): Promise<{ ok: UploadResult[]; failed: { name: string; reason: string }[] }> {
  const ok: UploadResult[] = [];
  const failed: { name: string; reason: string }[] = [];
  for (const f of files) {
    try {
      ok.push(await uploadAttachment(f, sessionId, signal));
    } catch (e) {
      failed.push({ name: f.name, reason: e instanceof Error ? e.message : "未知错误" });
    }
  }
  return { ok, failed };
}

export { AuthError };
