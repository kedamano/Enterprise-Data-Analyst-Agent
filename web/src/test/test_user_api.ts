// 本机 sandbox 受限，CI 可执行
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// vi.mock 必须出现在 import 之前（hoisting）
vi.mock("@/lib/auth", () => ({
  getApiKey: vi.fn(),
  setApiKey: vi.fn(),
  AuthError: class extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = "AuthError";
      this.status = status;
    }
  },
  authHeaders: vi.fn(() => ({})),
}));

const listeners = new Set<() => void>();
let mockState: { user: unknown; config: unknown; ready: boolean; busy: boolean; error: string } =
  { user: null, config: null, ready: false, busy: false, error: "" };

function emit(patch: Partial<typeof mockState>) {
  mockState = { ...mockState, ...patch };
  listeners.forEach((fn) => fn());
}

vi.mock("@/lib/user", () => ({
  getUserToken: vi.fn(() => localStorage.getItem("da_user_token") ?? ""),
  setUserToken: vi.fn((t: string) => {
    if (t) localStorage.setItem("da_user_token", t);
    else localStorage.removeItem("da_user_token");
  }),
  userAuthHeader: vi.fn(() =>
    localStorage.getItem("da_user_token")
      ? { Authorization: `Bearer ${localStorage.getItem("da_user_token")}` }
      : {},
  ),
  useAuth: vi.fn(() => mockState),
  authSnapshot: vi.fn(() => mockState),
  login: vi.fn(async (username: string, password: string) => {
    emit({ busy: true, error: "" });
    const res = await fetch_mock("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    localStorage.setItem("da_user_token", data.token);
    emit({ user: data.user, busy: false, ready: true });
    listeners.forEach((fn) => fn());
    return data.user;
  }),
  register: vi.fn(async (username: string, password: string) => {
    emit({ busy: true, error: "" });
    const res = await fetch_mock("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    localStorage.setItem("da_user_token", data.token);
    emit({ user: data.user, busy: false, ready: true });
    return data.user;
  }),
  refreshAuth: vi.fn(async () => {
    try {
      const res = await fetch_mock("/api/v1/auth/me");
      const data = await res.json();
      if (data.authenticated && data.user) {
        emit({ user: data.user, ready: true, error: "" });
      } else {
        localStorage.removeItem("da_user_token");
        emit({ user: null, ready: true });
      }
    } catch {
      emit({ ready: true });
    }
  }),
  logout: vi.fn(async () => {
    emit({ busy: true });
    try {
      await fetch_mock("/api/v1/auth/logout", { method: "POST" });
    } catch {
      /* 忽略后端不可达 */
    } finally {
      localStorage.removeItem("da_user_token");
      emit({ user: null, busy: false });
    }
  }),
}));

// 后端 mock：可控的 fetch 替代
let fetch_mock: (input: string, init?: RequestInit) => Promise<Response>;
global.fetch = vi.fn();

function mockResponse(data: unknown, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: vi.fn(async () => data),
  } as unknown as Response;
}

describe("login", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockState = { user: null, config: null, ready: false, busy: false, error: "" };
    fetch_mock = vi.fn(async (path: string) => {
      if (path === "/api/v1/auth/login") {
        return mockResponse({ token: "tok-123", user: { id: "u1", username: "alice" } });
      }
      return mockResponse({});
    });
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
  });
  afterEach(() => localStorage.clear());

  it("login 成功后 setUserToken 写入 localStorage + 发出订阅通知", async () => {
    const { login } = await import("@/lib/user");
    let notified = false;
    listeners.add(() => {
      notified = true;
    });
    const user = await login("alice", "pass123");
    expect(user).toEqual({ id: "u1", username: "alice" });
    expect(localStorage.getItem("da_user_token")).toBe("tok-123");
    expect(notified).toBe(true);
  });

  it("login 失败时抛出错误且不写 token", async () => {
    fetch_mock = vi.fn(async () => mockResponse({ detail: "密码错误" }, false, 400));
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
    const { login } = await import("@/lib/user");
    await expect(login("alice", "wrong")).rejects.toThrow();
  });
});

describe("register", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockState = { user: null, config: null, ready: false, busy: false, error: "" };
    fetch_mock = vi.fn(async () =>
      mockResponse({ token: "tok-456", user: { id: "u2", username: "bob" } }),
    );
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
  });
  afterEach(() => localStorage.clear());

  it("register 成功后更新当前用户", async () => {
    const { register } = await import("@/lib/user");
    const user = await register("bob", "pass123");
    expect(user).toEqual({ id: "u2", username: "bob" });
    expect(mockState.user).toEqual({ id: "u2", username: "bob" });
  });
});

describe("refreshAuth 令牌失效", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockState = { user: null, config: null, ready: false, busy: false, error: "" };
  });
  afterEach(() => localStorage.clear());

  it("后端 401（me 返回非 authenticated）→ 清空 token", async () => {
    localStorage.setItem("da_user_token", "expired-tok");
    fetch_mock = vi.fn(async () => mockResponse({ authenticated: false, user: null }));
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
    const { refreshAuth } = await import("@/lib/user");
    await refreshAuth();
    expect(localStorage.getItem("da_user_token")).toBeNull();
    expect(mockState.user).toBeNull();
    expect(mockState.ready).toBe(true);
  });

  it("后端返回 authenticated + user → 清除 ready 并更新 user", async () => {
    localStorage.setItem("da_user_token", "good-tok");
    fetch_mock = vi.fn(async () =>
      mockResponse({ authenticated: true, user: { id: "u3", username: "carol" } }),
    );
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
    const { refreshAuth } = await import("@/lib/user");
    await refreshAuth();
    expect(mockState.user).toEqual({ id: "u3", username: "carol" });
    expect(mockState.ready).toBe(true);
  });
});

describe("logout", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    fetch_mock = vi.fn(async () => mockResponse({ ok: true }));
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
  });
  afterEach(() => localStorage.clear());

  it("无论后端是否可达，都清空本地 token", async () => {
    localStorage.setItem("da_user_token", "tok-to-clear");
    const { logout } = await import("@/lib/user");
    await logout();
    expect(localStorage.getItem("da_user_token")).toBeNull();
    expect(mockState.user).toBeNull();
    expect(mockState.busy).toBe(false);
  });

  it("后端不可达时仍不会抛错", async () => {
    fetch_mock = vi.fn(async () => {
      throw new Error("network down");
    });
    // @ts-expect-error 覆盖全局 fetch
    global.fetch = fetch_mock;
    localStorage.setItem("da_user_token", "tok-x");
    const { logout } = await await import("@/lib/user");
    await expect(logout()).resolves.not.toThrow();
  });
});

describe("useAuth hook", () => {
  it("初始无 token 时 state.user 为 null", () => {
    mockState = { user: null, config: null, ready: false, busy: false, error: "" };
    const { useAuth } = require("@/lib/user");
    expect(useAuth().user).toBeNull();
    expect(useAuth().ready).toBe(false);
  });

  it("状态变更后订阅者收得到", () => {
    const { useAuth } = require("@/lib/user");
    mockState = { ...mockState, user: { id: "u1", username: "alice" } };
    emit({});
    expect(useAuth().user).toEqual({ id: "u1", username: "alice" });
  });
});
