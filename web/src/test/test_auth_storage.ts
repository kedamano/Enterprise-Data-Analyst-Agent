// 本机 sandbox 受限，CI 可执行
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  getApiKey,
  setApiKey,
  hasApiKey,
  AuthError,
  authHeaders,
  maybeAuthError,
} from "@/lib/auth";
import { renderHook, act } from "@/test/helper";

// storage.ts 的 useLocalStorage 是 React hook，直接在非组件下测需要 React 渲染。
// 为了纯逻辑测到 JSON.parse fallback，直接模拟 localStorage 初始化读写的逻辑。

describe("AuthError 类", () => {
  it("实例带 name / status / message", () => {
    const e = new AuthError("需要鉴权", 401);
    expect(e).toBeInstanceOf(Error);
    expect(e).toBeInstanceOf(AuthError);
    expect(e.message).toBe("需要鉴权");
    expect(e.status).toBe(401);
    expect(e.name).toBe("AuthError");
  });
});

describe("maybeAuthError 分支", () => {
  it("401 → 返回 AuthError", () => {
    const err = maybeAuthError({ status: 401 } as Response, "未授权");
    expect(err).toBeInstanceOf(AuthError);
    expect((err as AuthError).status).toBe(401);
  });

  it("503 → 返回 AuthError", () => {
    const err = maybeAuthError({ status: 503 } as Response, "服务不可用");
    expect(err).toBeInstanceOf(AuthError);
  });

  it("403 → 不返回 AuthError（边界外）", () => {
    const err = maybeAuthError({ status: 403 } as Response, "禁止访问");
    expect(err).toBeNull();
  });

  it("500 → 不返回 AuthError", () => {
    const err = maybeAuthError({ status: 500 } as Response, "内部错误");
    expect(err).toBeNull();
  });

  it("200 → 不返回 AuthError", () => {
    const err = maybeAuthError({ status: 200 } as Response, "OK");
    expect(err).toBeNull();
  });
});

describe("authHeaders", () => {
  it("有 API Key 时返回 X-API-Key 头", () => {
    setApiKey("my-key");
    expect(authHeaders()).toEqual({ "X-API-Key": "my-key" });
  });

  it("API Key 为空时返回空对象", () => {
    setApiKey("");
    expect(authHeaders()).toEqual({});
  });
});

describe("localStorage 存读 API Key", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it("setApiKey 写入 / getApiKey 读到", () => {
    setApiKey("abc-123");
    expect(getApiKey()).toBe("abc-123");
    expect(localStorage.getItem("da_api_key")).toBe("abc-123");
  });

  it("setApiKey 传入空串 → removeItem", () => {
    setApiKey("abc-123");
    expect(getApiKey()).toBe("abc-123");
    setApiKey("");
    expect(localStorage.getItem("da_api_key")).toBeNull();
    expect(getApiKey()).toBe("");
  });

  it("hasApiKey: 有 key → true", () => {
    setApiKey("any-key");
    expect(hasApiKey()).toBe(true);
  });

  it("hasApiKey: 空 → false", () => {
    setApiKey("");
    expect(hasApiKey()).toBe(false);
  });
});

describe("useLocalStorage hook (SSR + 首次读 + 写入触发 + JSON.parse fallback)", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("SSR 无 localStorage 时返回默认值", () => {
    // 模拟 SSR：localStorage 抛错
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("unavailable");
    });
    const { result } = renderHook(() =>
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      require("@/lib/storage").useLocalStorage("test-key", "default-val"),
    );
    expect(result.current[0]).toBe("default-val");
    spy.mockRestore();
  });

  it("首次读：有缓存就解析出来", () => {
    localStorage.setItem("cached-key", JSON.stringify({ count: 42 }));
    const { result } = renderHook(() =>
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      require("@/lib/storage").useLocalStorage("cached-key", { count: 0 }),
    );
    expect(result.current[0]).toEqual({ count: 42 });
  });

  it("首次读：JSON.parse 恶意内容 → 回退 default", () => {
    localStorage.setItem("bad-key", "not json at all {{{");
    const spy = vi.spyOn(JSON, "parse").mockImplementation(() => {
      throw new SyntaxError("bad json");
    });
    const { result } = renderHook(() =>
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      require("@/lib/storage").useLocalStorage("bad-key", "fallback"),
    );
    expect(result.current[0]).toBe("fallback");
    spy.mockRestore();
  });

  it("setValue 写入后 localStorage 同步更新", () => {
    const { result } = renderHook(() =>
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      require("@/lib/storage").useLocalStorage<string>("write-key", "init"),
    );
    act(() => {
      result.current[1]("new-value");
    });
    expect(localStorage.getItem("write-key")).toBe(JSON.stringify("new-value"));
  });
});
