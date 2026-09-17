// AUTH/02 前端登录态。
//
// 设计取舍
// --------
// - **不引状态库**：整个应用只有一个跨组件可变状态（当前用户），用一个
//   20 行的订阅器 + `useSyncExternalStore` 就够了，不值得为此加依赖。
// - **令牌存 localStorage**：与既有 `da_api_key` 一致（同源、发不到第三方）。
//   代价是 XSS 可读；换来的是刷新不掉登录态。这也是本控制台的既有基线，
//   不在这里单独升级为 httpOnly Cookie——那需要后端加 Cookie 校验与 CSRF 防护，
//   属于独立的一次改动，混进来会让本次 diff 无法审查。
// - **模块不依赖 api.ts**：`api.ts → auth.ts → user.ts` 单向依赖，避免循环导入。
//   （所以这里自己拼了一个 20 行的请求包装，而不是复用 requestJson。）

import { useSyncExternalStore } from "react";

// --------------------------------------------------------------------------- 类型

export interface AuthUser {
  id: string;
  username: string;
  email: string;
  phone: string;
  display_name: string;
  bio: string;
  role: string;
  status: string;
  tenant: string;
  avatar: string;
  avatar_version: number;
  source: string; // 账号来源。后端下发的字段，前端不再按来源分支处理
  created_at: string;
  updated_at: string;
  last_login_at: string;
}

export interface AuthConfig {
  user_auth_enabled: boolean;
  enforcement: boolean;
  registration_open: boolean;
  password_min_length: number;
  has_users: boolean;
}

export interface RoleInfo {
  role: string;
  label: string;
  summary: string;
  rank: number;
  permissions: string[];
  permission_labels: string[];
}

export interface SessionInfo {
  created_at: string;
  expires_at: string;
  user_agent: string;
  ip: string;
}

export interface LoginEvent {
  username: string;
  ip: string;
  ts: number;
  ok: number | boolean;
  reason: string;
}

// --------------------------------------------------------------------------- 令牌

const TOKEN_KEY = "da_user_token";

export function getUserToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

function setUserToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* 隐私模式下存储不可用：本次会话仍可用，只是刷新后掉线 */
  }
}

/** 供 auth.ts 合并进所有 API 请求（让已登录身份自动带上）。 */
export function userAuthHeader(): Record<string, string> {
  const t = getUserToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

// --------------------------------------------------------------------------- 请求

/** 后端 `detail` 可能是字符串，也可能是 `{code, message}`（auth 路由统一后者）。 */
async function extractDetail(res: Response): Promise<string> {
  try {
    const j = (await res.json()) as { detail?: unknown };
    const d = j?.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "message" in d) {
      return String((d as { message: unknown }).message);
    }
  } catch {
    /* 保留默认信息 */
  }
  return `请求失败 HTTP ${res.status}`;
}

export class UserApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code = "") {
    super(message);
    this.name = "UserApiError";
    this.status = status;
    this.code = code;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...userAuthHeader(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let code = "";
    try {
      const j = (await res.json()) as { detail?: { code?: string } };
      if (j?.detail && typeof j.detail === "object") code = j.detail.code ?? "";
    } catch {
      /* ignore */
    }
    throw new UserApiError(await extractDetail(res), res.status, code);
  }
  return (await res.json()) as T;
}

// --------------------------------------------------------------------------- 状态

interface AuthState {
  /** null = 未登录（或还没查过） */
  user: AuthUser | null;
  config: AuthConfig | null;
  /** 首次 `refresh()` 是否已完成——避免首屏闪现"未登录"再跳成已登录 */
  ready: boolean;
  busy: boolean;
  error: string;
}

let state: AuthState = { user: null, config: null, ready: false, busy: false, error: "" };

const listeners = new Set<() => void>();

function emit(patch: Partial<AuthState>): void {
  state = { ...state, ...patch };
  listeners.forEach((fn) => fn());
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 当前登录态（React）。 */
export function useAuth(): AuthState {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

/** 非 React 场景读取（如 `authHeaders` 组装）。 */
export function authSnapshot(): AuthState {
  return state;
}

export function clearAuthError(): void {
  if (state.error) emit({ error: "" });
}

// --------------------------------------------------------------------------- 动作

/** 拉取"我是谁" + 服务端能力开关。应用启动与每次登录态变化后调用。 */
export async function refreshAuth(): Promise<void> {
  try {
    const cfg = await req<AuthConfig>("/api/v1/auth/config");
    emit({ config: cfg });
  } catch {
    /* 能力探测失败不阻塞：按默认值渲染，登录表单仍可提交 */
  }
  try {
    const me = await req<{
      authenticated: boolean;
      user: AuthUser | null;
      enforcement: boolean;
      user_auth_enabled: boolean;
    }>("/api/v1/auth/me");
    if (me.authenticated && me.user) {
      emit({ user: me.user, ready: true, error: "" });
    } else {
      // 令牌失效（过期/被踢/停用）→ 清掉本地残留，否则会一直带着废令牌发请求
      setUserToken("");
      emit({ user: null, ready: true });
    }
  } catch {
    emit({ ready: true });
  }
}

async function withToken<T extends { token: string; user: AuthUser }>(
  fn: () => Promise<T>,
  fallbackMsg: string,
): Promise<AuthUser> {
  emit({ busy: true, error: "" });
  try {
    const res = await fn();
    setUserToken(res.token);
    emit({ user: res.user, busy: false, ready: true });
    return res.user;
  } catch (err) {
    const msg = err instanceof Error ? err.message : fallbackMsg;
    emit({ busy: false, error: msg });
    throw err;
  }
}

export async function login(username: string, password: string): Promise<AuthUser> {
  return withToken(
    () =>
      req<{ token: string; user: AuthUser }>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      }),
    "登录失败",
  );
}

export async function register(
  username: string,
  password: string,
  extra: { email?: string; display_name?: string } = {},
): Promise<AuthUser> {
  return withToken(
    () =>
      req<{ token: string; user: AuthUser }>("/api/v1/auth/register", {
        method: "POST",
        body: JSON.stringify({ username, password, ...extra }),
      }),
    "注册失败",
  );
}

export async function logout(): Promise<void> {
  emit({ busy: true });
  try {
    await req("/api/v1/auth/logout", { method: "POST" });
  } catch {
    /* 后端不可达也要能本地登出——否则用户被锁在"永远登录"的状态里 */
  } finally {
    setUserToken("");
    emit({ user: null, busy: false });
  }
}

export async function updateProfile(patch: {
  display_name?: string;
  email?: string;
  phone?: string;
  bio?: string;
}): Promise<AuthUser> {
  const user = await req<AuthUser>("/api/v1/auth/me", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
  emit({ user });
  return user;
}

export async function changePassword(
  oldPassword: string,
  newPassword: string,
): Promise<number> {
  const res = await req<{ revoked_sessions: number }>("/api/v1/auth/me/password", {
    method: "POST",
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  return res.revoked_sessions;
}

export async function uploadAvatar(file: File): Promise<AuthUser> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  const user = await req<AuthUser>("/api/v1/auth/me/avatar", {
    method: "POST",
    body: fd,
  });
  emit({ user });
  return user;
}

export async function removeAvatar(): Promise<AuthUser> {
  const user = await req<AuthUser>("/api/v1/auth/me/avatar", { method: "DELETE" });
  emit({ user });
  return user;
}

export function fetchMySessions(): Promise<{ sessions: SessionInfo[] }> {
  return req("/api/v1/auth/me/sessions");
}

export function fetchMyLogins(limit = 10): Promise<{ events: LoginEvent[] }> {
  return req(`/api/v1/auth/me/logins?limit=${limit}`);
}

// --------------------------------------------------------------------------- 角色 / 成员（管理）

export function fetchRoles(): Promise<{ roles: RoleInfo[] }> {
  return req("/api/v1/auth/roles");
}

export function fetchUsers(q = ""): Promise<{ users: AuthUser[]; total: number }> {
  const qs = q ? `?q=${encodeURIComponent(q)}` : "";
  return req(`/api/v1/auth/users${qs}`);
}

export function adminUpdateUser(
  userId: string,
  patch: { role?: string; status?: string },
): Promise<AuthUser> {
  return req<AuthUser>(`/api/v1/auth/users/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function adminDeleteUser(userId: string): Promise<{ ok: boolean }> {
  return req(`/api/v1/auth/users/${userId}`, { method: "DELETE" });
}

// --------------------------------------------------------------------------- 展示辅助

export const ROLE_LABEL: Record<string, string> = {
  viewer: "查看者",
  analyst: "分析师",
  analyst_lead: "分析主管",
  admin: "管理员",
};

export function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

/** 用户名首字（无头像时的占位）。 */
export function initials(user: AuthUser | null): string {
  if (!user) return "?";
  const name = user.display_name || user.username || "";
  return name.trim().slice(0, 1).toUpperCase() || "?";
}
