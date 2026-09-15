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

/**
 * D51：指标卡。**后端已归一为 {name, value, comparison}**——
 * 前端不再自己判 name/text/metric 优先级（那等于把口径分叉成两份，
 * 前端这一份永远没人测）。两种原始形态的处理见 app/.../metric_cards.py。
 */
export interface MetricCard {
  name: string;
  value?: string;
  comparison?: string;
}

/** D51：导出预览清单（与真实 zip **同源**构造，故预览不会骗人）。 */
export interface ExportManifestFile {
  name: string;
  bytes: number;
  /** report | sql | trace | data | chart | readme | watermark | other */
  kind: string;
  note?: string;
}

export interface ExportManifest {
  session_id: string;
  /** 与该包的实际脱敏状态一致（前端据此拼下载链接，避免"看了 A 下到 B"）。 */
  masked: boolean;
  total_bytes: number;
  files: ExportManifestFile[];
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
  /** D51：指标卡（仅 FINISH 帧下发；无指标为 []，中途帧为 null） */
  metrics?: MetricCard[] | null;
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

// ---------------------------------------------------------------- D51 导出预览

/**
 * 拉取导出预览清单（`GET /analyze/export/{sid}/manifest`）。
 *
 * `masked` 传 `undefined` → **不传参**，由后端按 DLP 策略定缺省（与「导出」按钮
 * 走的是同一条缺省逻辑）。响应里的 `masked` 是**实际生效值**，下载链接据此拼接，
 * 保证"预览的是哪一份，下到的就是哪一份"。
 *
 * 预览**不需要两步授权**：它只暴露文件名与字节数，不暴露任何值。
 */
export async function fetchManifest(
  sessionId: string,
  masked?: boolean,
  signal?: AbortSignal,
): Promise<ExportManifest> {
  const qs = masked === undefined ? "" : `?masked=${masked ? 1 : 0}`;
  const res = await fetch(`/api/v1/chat/analyze/export/${sessionId}/manifest${qs}`, {
    headers: { ...authHeaders() },
    signal,
  });
  if (!res.ok) {
    const authErr = maybeAuthError(res, `导出预览失败 HTTP ${res.status}`);
    if (authErr) throw authErr;
    throw new Error(`导出预览失败 HTTP ${res.status}`);
  }
  return (await res.json()) as ExportManifest;
}

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

// ---------------------------------------------------------------- 文件库 / 数据源

/** GET /attachments/list 的响应（按 session 分桶）。 */
export interface AttachmentListPayload {
  session_id: string;
  attachments: UploadedAttachment[];
}

/** 拉取某个会话（或默认桶）已上传/已解析的文件清单。 */
export async function fetchAttachments(
  sessionId: string | null,
  signal?: AbortSignal,
): Promise<AttachmentListPayload> {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  const res = await fetch(`/api/v1/attachments/list${qs}`, {
    headers: { ...authHeaders() },
    signal,
  });
  if (!res.ok) {
    const authErr = maybeAuthError(res, `获取文件列表失败 HTTP ${res.status}`);
    if (authErr) throw authErr;
    throw new Error(`获取文件列表失败 HTTP ${res.status}`);
  }
  return (await res.json()) as AttachmentListPayload;
}

/** GET /health 的子集：数据源页只关心这几个字段。 */
export interface HealthInfo {
  status: string;
  data_source: string;
  data_sources?: string[];
  llm_mode?: string;
  mock_llm?: boolean;
  llm_degraded?: boolean;
  knowledge_enabled?: boolean;
}

/** 拉取服务健康信息（数据源弹窗用）。 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthInfo> {
  const res = await fetch("/api/v1/health", { signal });
  if (!res.ok) throw new Error(`获取数据源信息失败 HTTP ${res.status}`);
  return (await res.json()) as HealthInfo;
}

// ---------------------------------------------------------------- 数据源（数据库连接配置）

export interface DataSourceConn {
  name: string;
  dialect: string;
  url: string; // 已脱敏
  readonly: boolean;
}

export interface DataSourceListResponse {
  sources: DataSourceConn[];
}

/** GET /api/v1/datasources —— 已配置的数据库连接清单（与「文件库」是两类资产）。 */
export async function fetchDataSources(
  signal?: AbortSignal,
): Promise<DataSourceListResponse> {
  const res = await fetch("/api/v1/datasources", { signal });
  if (!res.ok) throw new Error(`获取数据源失败 HTTP ${res.status}`);
  return (await res.json()) as DataSourceListResponse;
}

// ---------------------------------------------------------------- 知识库（RAG 管理）

export interface KBSource {
  source: string;
  chunks: number;
}

export interface KBStatus {
  enabled: boolean;
  backend: string; // sqlite | milvus
  total_chunks: number;
  sources: number;
}

export interface KBListResponse {
  status: KBStatus;
  documents: KBSource[];
}

export interface KBHit {
  source: string;
  text: string;
  score: number;
}

/** GET /api/v1/documents —— 知识库来源清单 + 整体状态。 */
export async function fetchDocuments(signal?: AbortSignal): Promise<KBListResponse> {
  const res = await fetch("/api/v1/documents", { signal });
  if (!res.ok) throw new Error(`获取知识库失败 HTTP ${res.status}`);
  return (await res.json()) as KBListResponse;
}

/** POST /api/v1/documents/upload —— 浏览器上传文件或粘贴文本入库。 */
export async function uploadDocument(
  payload: { file?: File; text?: string; source?: string },
  signal?: AbortSignal,
): Promise<{ chunks: number; source: string }> {
  const fd = new FormData();
  if (payload.file) fd.append("file", payload.file, payload.file.name);
  if (payload.text) fd.append("text", payload.text);
  if (payload.source) fd.append("source", payload.source);
  const res = await fetch("/api/v1/documents/upload", {
    method: "POST",
    body: fd,
    headers: { ...authHeaders() },
    signal,
  });
  if (!res.ok) {
    const authErr = maybeAuthError(res, `入库失败 HTTP ${res.status}`);
    if (authErr) throw authErr;
    let detail = `入库失败 HTTP ${res.status}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j?.detail) detail = j.detail;
    } catch {
      /* keep */
    }
    throw new Error(detail);
  }
  return (await res.json()) as { chunks: number; source: string };
}

/** GET /api/v1/documents/search —— 检索预览。 */
export async function searchDocuments(
  q: string,
  topK = 5,
  signal?: AbortSignal,
): Promise<{ query: string; hits: KBHit[] }> {
  const qs = `?q=${encodeURIComponent(q)}&top_k=${topK}`;
  const res = await fetch(`/api/v1/documents/search${qs}`, { signal });
  if (!res.ok) throw new Error(`检索失败 HTTP ${res.status}`);
  return (await res.json()) as { query: string; hits: KBHit[] };
}

/** DELETE /api/v1/documents?source= —— 删除某个来源。 */
export async function deleteDocument(
  source: string,
  signal?: AbortSignal,
): Promise<{ source: string; deleted: number }> {
  const res = await fetch(
    `/api/v1/documents?source=${encodeURIComponent(source)}`,
    { method: "DELETE", headers: { ...authHeaders() }, signal },
  );
  if (!res.ok) throw new Error(`删除失败 HTTP ${res.status}`);
  return (await res.json()) as { source: string; deleted: number };
}

// ---------------------------------------------------------------- 多知识库（“我的知识库”）
//
// 与上面的 /documents 扁平接口的区别：这里以「知识库」为资产单位——一个库
// 有名字/类型/可见性，内部装若干文档，可单独检索、改名、删除。

export interface KbBase {
  id: string;
  name: string;
  description: string;
  /** general（通用知识库） | website（网站知识库） */
  kb_type: string;
  /** private | public */
  visibility: string;
  owner: string;
  created_at: string;
  updated_at: string;
  documents: number;
  chunks: number;
}

export interface KbBaseListResponse {
  bases: KbBase[];
  backend: string;
  total_chunks: number;
}

export interface KbDocument {
  id: string;
  kb_id: string;
  name: string;
  source: string;
  /** file | text | website */
  doc_type: string;
  mime: string;
  bytes: number;
  chunks: number;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface KbIngestResponse {
  ok: boolean;
  document: KbDocument;
  hint?: string | null;
}

/** 统一的请求包装：把后端 400 的 `detail` 抬成 Error message，便于直接展示。 */
async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, {
    ...init,
    headers: { ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...authHeaders(), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const authErr = maybeAuthError(res, `请求失败 HTTP ${res.status}`);
    if (authErr) throw authErr;
    let detail = `请求失败 HTTP ${res.status}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j?.detail) detail = j.detail;
    } catch {
      /* 保留默认信息 */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export function fetchKbBases(signal?: AbortSignal): Promise<KbBaseListResponse> {
  return requestJson<KbBaseListResponse>("/api/v1/knowledge-bases", { signal });
}

export function createKbBase(req: {
  name: string;
  description?: string;
  kb_type?: string;
  visibility?: string;
}): Promise<KbBase> {
  return requestJson<KbBase>("/api/v1/knowledge-bases", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export function updateKbBase(
  kbId: string,
  req: { name?: string; description?: string; kb_type?: string; visibility?: string },
): Promise<KbBase> {
  return requestJson<KbBase>(`/api/v1/knowledge-bases/${kbId}`, {
    method: "PATCH",
    body: JSON.stringify(req),
  });
}

export function deleteKbBase(kbId: string): Promise<{ ok: boolean; deleted_chunks: number }> {
  return requestJson(`/api/v1/knowledge-bases/${kbId}`, { method: "DELETE" });
}

export function fetchKbDocuments(
  kbId: string,
  signal?: AbortSignal,
): Promise<{ kb_id: string; documents: KbDocument[] }> {
  return requestJson(`/api/v1/knowledge-bases/${kbId}/documents`, { signal });
}

export function uploadKbDocument(kbId: string, file: File): Promise<KbIngestResponse> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  return requestJson<KbIngestResponse>(
    `/api/v1/knowledge-bases/${kbId}/documents/upload`,
    { method: "POST", body: fd },
  );
}

export function addKbText(
  kbId: string,
  text: string,
  name: string,
): Promise<KbIngestResponse> {
  return requestJson<KbIngestResponse>(
    `/api/v1/knowledge-bases/${kbId}/documents/text`,
    { method: "POST", body: JSON.stringify({ text, name }) },
  );
}

export function addKbWebsite(
  kbId: string,
  url: string,
  title?: string,
): Promise<KbIngestResponse> {
  return requestJson<KbIngestResponse>(
    `/api/v1/knowledge-bases/${kbId}/documents/website`,
    { method: "POST", body: JSON.stringify({ url, title }) },
  );
}

export function deleteKbDocument(
  kbId: string,
  docId: string,
): Promise<{ source: string; deleted: number }> {
  return requestJson(`/api/v1/knowledge-bases/${kbId}/documents/${docId}`, {
    method: "DELETE",
  });
}

export function searchKb(
  kbId: string,
  q: string,
  topK = 5,
  signal?: AbortSignal,
): Promise<{ query: string; hits: KBHit[] }> {
  const qs = `?q=${encodeURIComponent(q)}&top_k=${topK}`;
  return requestJson(`/api/v1/knowledge-bases/${kbId}/search${qs}`, { signal });
}

// ---------------------------------------------------------------- 文件库（企业文件管理）

export interface FsNode {
  id: string;
  parent_id: string;
  name: string;
  is_dir: boolean;
  bytes: number;
  mime: string;
  size_label: string;
  created_at: string;
  updated_at: string;
  /** 搜索结果携带完整路径，如 /数据集/2024/orders.csv */
  path?: string | null;
}

export interface FsTreeResponse {
  nodes: FsNode[];
  stats: { folders?: number; files?: number; bytes?: number };
}

export interface FsListResponse {
  parent_id: string;
  breadcrumb: FsNode[];
  nodes: FsNode[];
}

export function fetchFsTree(signal?: AbortSignal): Promise<FsTreeResponse> {
  return requestJson<FsTreeResponse>("/api/v1/files/tree", { signal });
}

export function fetchFsList(
  parentId = "",
  signal?: AbortSignal,
): Promise<FsListResponse> {
  const qs = parentId ? `?parent_id=${encodeURIComponent(parentId)}` : "";
  return requestJson<FsListResponse>(`/api/v1/files/list${qs}`, { signal });
}

export function searchFs(q: string, signal?: AbortSignal): Promise<{ query: string; results: FsNode[] }> {
  return requestJson(`/api/v1/files/search?q=${encodeURIComponent(q)}`, { signal });
}

export function createFsFolder(parentId: string, name: string): Promise<FsNode> {
  return requestJson<FsNode>("/api/v1/files/folder", {
    method: "POST",
    body: JSON.stringify({ parent_id: parentId, name }),
  });
}

export function uploadFsFile(parentId: string, file: File): Promise<FsNode> {
  const fd = new FormData();
  fd.append("parent_id", parentId);
  fd.append("file", file, file.name);
  return requestJson<FsNode>("/api/v1/files/upload", { method: "POST", body: fd });
}

export function renameFsNode(nodeId: string, name: string): Promise<FsNode> {
  return requestJson<FsNode>(`/api/v1/files/node/${nodeId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export function deleteFsNode(nodeId: string): Promise<{ ok: boolean; deleted: number }> {
  return requestJson(`/api/v1/files/node/${nodeId}`, { method: "DELETE" });
}

/** 下载直链（浏览器原生下载，不走 fetch，避免大文件占用内存）。 */
export function fsDownloadUrl(nodeId: string): string {
  return `/api/v1/files/download/${nodeId}`;
}
