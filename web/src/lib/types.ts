import type { AgentEvent, Clarification } from "./api";

export type Role = "user" | "assistant";

export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  kind: "image" | "file";
  /** image 类型的 blob: 预览 URL（仅本机，回话里不持久化） */
  previewUrl?: string;
  /** 原始 File 对象：仅存在于内存，用于真正上传到后端；不落 localStorage */
  file?: File;
}

export interface Message {
  id: string;
  role: Role;
  text?: string; // 用户问题 / 助手最终报告（Markdown）
  events?: AgentEvent[]; // 助手的流式编排事件
  status?: string;
  objective?: string | null;
  done?: boolean;
  error?: string | null;
  /** CLARIFY/01：待用户回答的澄清问题 */
  clarification?: Clarification | null;
  /** 该澄清是否已被回答（用于折叠卡片） */
  answered?: boolean;
  /** 用户上传的附件：发送时先 POST 到 /attachments/upload，再由后端注入分析上下文 */
  attachments?: Attachment[];
}

export interface Conversation {
  id: string;
  title: string;
  messages: Message[];
  createdAt: number;
  updatedAt: number;
}
