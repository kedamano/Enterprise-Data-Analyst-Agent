// #1 前端鉴权桥接：当后端 AUTH_ENABLED=true 时，前端需携带 X-API-Key。
// key 存 localStorage（同源、非敏感落盘），不发到任何第三方。

const API_KEY_STORAGE = "da_api_key";

export class AuthError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "AuthError";
    this.status = status;
  }
}

export function getApiKey(): string {
  try {
    return localStorage.getItem(API_KEY_STORAGE) ?? "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(API_KEY_STORAGE, key);
    else localStorage.removeItem(API_KEY_STORAGE);
  } catch {
    /* 忽略隐私模式等存储不可用 */
  }
}

export function hasApiKey(): boolean {
  return getApiKey().length > 0;
}

/** 注入鉴权头；无 key 时返回空对象（后端 AUTH 关闭则忽略）。 */
export function authHeaders(): Record<string, string> {
  const k = getApiKey();
  return k ? { "X-API-Key": k } : {};
}

/** 把 401/503 转成 AuthError，供 UI 弹出登录框（不回显差异）。 */
export function maybeAuthError(res: Response, detail: string): Error | null {
  if (res.status === 401 || res.status === 503) {
    return new AuthError(detail || "需要有效的 API Key", res.status);
  }
  return null;
}
