import { useEffect, useState } from "react";
import { setApiKey } from "@/lib/auth";
import { login, register } from "@/lib/user";

type OidcConfig = { configured: boolean; issuer: string | null };

async function fetchOidcConfig(): Promise<OidcConfig> {
  try {
    const r = await fetch("/api/v1/auth/oidc/config");
    if (!r.ok) return { configured: false, issuer: null };
    const data = await r.json();
    return { configured: Boolean(data.configured), issuer: data.issuer ?? null };
  } catch {
    return { configured: false, issuer: null };
  }
}

/**
 * 整合版登录 / 注册落地页（替代 AuthGate 的单弹窗）。
 *
 * 视觉参考：Codepen "Double slider Sign in/up Form"（Florin Pop）。
 * 两张表单 + 一张 overlay 面板在同一个卡内滑动；点击切换时两张表单与
 * overlay 一起横滑，视觉优雅，且把登录 / 注册聚到同一张卡内，不再是两个孤立的 Modal。
 *
 * 项目的布品牌色（--color-brand: #143a5e）替代参考设计里的橙红渐变。
 */

type Mode = "login" | "register";

export function AuthCentre({
  onAuthed,
  onSkip,
}: {
  onAuthed: () => void;
  onSkip?: () => void;
}) {
  // Escape 键退出落地页（等同「跳过」）；遇鉴权失败临时进入的用户最自然的退出路径
  useEffect(() => {
    if (!onSkip) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onSkip();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onSkip]);
  // 探测后端是否配了 OIDC：有 → 显示「使用 OIDC 登录」按钮
  useEffect(() => {
    fetchOidcConfig().then((cfg) => setShowOidc(cfg.configured));
  }, []);
  const [mode, setMode] = useState<Mode>("login");
  const [showOidc, setShowOidc] = useState(false);
  const [loginName, setLoginName] = useState("");
  const [loginKey, setLoginKey] = useState("");
  const [regName, setRegName] = useState("");
  const [regKey, setRegKey] = useState("");
  const [regConfirm, setRegConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toLogin = () => {
    setError(null);
    setMode("login");
  };
  const toRegister = () => {
    setError(null);
    setMode("register");
  };

  // 登录：
  //   1. 先走后端 /api/v1/auth/login（Bearer token 体系，记入 lib/user.ts 的 useAuth）。
  //   2. 后端不可达时 fallback 到 X-API-Key 直写 localStorage——保障 MOCK_LLM 离线、
  //      HTTP_PROXY 挂掉等前端单机场景仍可点进来。
  // 保留 fallback 是因为：静态 API Key 体系仍在 X-API-Key 中间件层生效，
  // 两套凭证并存是项目设计（authHeaders: Bearer 优先，X-API-Key 兜底）。
  const submitLogin = async () => {
    const name = loginName.trim();
    const key = loginKey.trim();
    if (!name || !key || submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(name, key);
      onAuthed();
      return;
    } catch (err) {
      // 后端不可达：fallback 到 api key 直写
      try {
        setApiKey(key);
        onAuthed();
        return;
      } catch {
        setError(err instanceof Error ? err.message : "登录失败");
      }
    } finally {
      setSubmitting(false);
    }
  };

  // 注册：走后端 /api/v1/auth/register（首位注册者自动成为 admin）。
  // 后端不可达时 fallback 到 X-API-Key 直写；密码长度/一致性前置校验仍在前端拦，
  // 避免无意义的网络往返。
  const submitRegister = async () => {
    const name = regName.trim();
    const key = regKey.trim();
    if (!name || !key || submitting) return;
    if (key.length < 6) {
      setError("访问密钥至少 6 位");
      return;
    }
    if (key !== regConfirm) {
      setError("两次输入的密钥不一致");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      await register(name, key);
      onAuthed();
      return;
    } catch (err) {
      try {
        setApiKey(key);
        onAuthed();
        return;
      } catch {
        setError(err instanceof Error ? err.message : "注册失败");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const isRegister = mode === "register";
  // 两张表单 / 一张 overlay 横向滑动的状态类
  // register 模式：两张表单 translateX(→ 100%) 以露出 register 表单；
  //                 overlay 滑到左侧，文案切换到登录引导。

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-canvas px-4">
      <div
        className={[
          "relative h-[520px] w-full max-w-[780px] overflow-hidden rounded-panel",
          "bg-paper shadow-2xl",
        ].join(" ")}
      >
        {/* ---- 注册表单（absolute，login 时被 overlay 遮住） ---- */}
        <div
          className={[
            "absolute inset-y-0 left-0 flex h-full w-1/2 flex-col items-center justify-center px-10",
            "transition-transform duration-500 ease-in-out",
            isRegister ? "translate-x-0 opacity-100" : "translate-x-0 opacity-0",
            "z-[1]",
          ].join(" ")}
          aria-hidden={!isRegister}
        >
          <form
            className="flex w-full flex-col items-center"
            onSubmit={(e) => {
              e.preventDefault();
              submitRegister();
            }}
          >
            <h1 className="text-title font-bold text-ink">创建账号</h1>
            <p className="mt-2 px-4 text-center text-small text-ink-3">
              填写用户名与访问密钥，即可开始使用
            </p>
            <input
              type="text"
              value={regName}
              autoFocus={isRegister}
              onChange={(e) => setRegName(e.target.value)}
              placeholder="用户名"
              aria-label="用户名"
              className="mt-5 w-full rounded-control border border-rule bg-canvas px-3 py-2.5 text-body text-ink outline-none transition focus:border-brand"
            />
            <input
              type="password"
              value={regKey}
              onChange={(e) => setRegKey(e.target.value)}
              placeholder="访问密钥（至少 6 位）"
              aria-label="访问密钥"
              className="mt-3 w-full rounded-control border border-rule bg-canvas px-3 py-2.5 text-body text-ink outline-none transition focus:border-brand"
            />
            <input
              type="password"
              value={regConfirm}
              onChange={(e) => setRegConfirm(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitRegister()}
              placeholder="再输一次访问密钥"
              aria-label="确认访问密钥"
              className="mt-3 w-full rounded-control border border-rule bg-canvas px-3 py-2.5 text-body text-ink outline-none transition focus:border-brand"
            />
            {error && isRegister && (
              <p className="mt-3 text-small text-danger">{error}</p>
            )}
            <button
                type="submit"
                disabled={!regName.trim() || !regKey.trim() || submitting}
                className="mt-5 h-[38px] w-[140px] rounded-full bg-brand text-small font-medium text-white transition hover:bg-brand-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 disabled:opacity-50"
              >
                {submitting ? "创建中…" : "创建账号"}
              </button>
            </form>
        </div>

        {/* ---- 登录表单（absolute，register 时被 overlay 遮住） ---- */}
        <div
          className={[
            "absolute inset-y-0 left-0 flex h-full w-1/2 flex-col items-center justify-center px-10",
            "transition-transform duration-500 ease-in-out",
            isRegister ? "translate-x-full opacity-0" : "translate-x-0 opacity-100",
            isRegister ? "z-0" : "z-[2]",
          ].join(" ")}
          aria-hidden={isRegister}
        >
          <form
            className="flex w-full flex-col items-center"
            onSubmit={(e) => {
              e.preventDefault();
              submitLogin();
            }}
          >
            <h1 className="text-title font-bold text-ink">登录</h1>
            <p className="mt-2 px-4 text-center text-small text-ink-3">
              输入访问密钥，继续之前的会话
            </p>
            {showOidc && (
              <button
                type="button"
                onClick={() => { location.href = "/api/v1/auth/oidc/login"; }}
                className="mt-4 h-[38px] w-full rounded-full border-2 border-solid border-brand bg-transparent text-small font-medium text-brand transition hover:bg-brand/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
              >
                使用 OIDC 登录
              </button>
            )}
            <input
              type="text"
              value={loginName}
              autoFocus={!isRegister}
              onChange={(e) => setLoginName(e.target.value)}
              placeholder="用户名"
              aria-label="用户名"
              className="mt-4 w-full rounded-control border border-rule bg-canvas px-3 py-2.5 text-body text-ink outline-none transition focus:border-brand"
            />
            <input
              type="password"
              value={loginKey}
              onChange={(e) => setLoginKey(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submitLogin()}
              placeholder="密码"
              aria-label="密码"
              className="mt-3 w-full rounded-control border border-rule bg-canvas px-3 py-2.5 text-body text-ink outline-none transition focus:border-brand"
            />
            {error && !isRegister && (
              <p className="mt-3 text-small text-danger">{error}</p>
            )}
            <button
              type="submit"
              disabled={!loginName.trim() || !loginKey.trim() || submitting}
              className="mt-5 h-[38px] w-[140px] rounded-full border-2 border-solid border-white bg-transparent text-small font-medium text-white transition enabled:hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-offset-2 disabled:opacity-50"
              style={{
                background: "var(--color-brand)",
                borderColor: "transparent",
              }}
            >
              {submitting ? "登录中…" : "登录"}
            </button>
          </form>
        </div>

        {/* ---- Overlay 面板（滑动 + 渐变背景 + 切换按钮） ---- */}
        <div
          className={[
            "pointer-events-none absolute inset-y-0 right-0 h-full w-1/2 overflow-hidden",
            "transition-transform duration-500 ease-in-out",
            isRegister ? "-translate-x-full" : "translate-x-0",
            "z-[50]",
          ].join(" ")}
        >
          <div
            className={[
              "relative h-full w-[200%] text-white",
              "transition-transform duration-500 ease-in-out",
              isRegister ? "-translate-x-1/2" : "translate-x-0",
              // 渐变背景：项目品牌色
              "bg-gradient-to-r from-brand to-brand-hover",
            ].join(" ")}
            style={{
              background:
                "linear-gradient(135deg, var(--color-brand) 0%, #0f2c48 100%)",
            }}
          >
            {/* ---- 右侧（login 时显示：引导去注册） ---- */}
            <div
              className={[
                "absolute left-0 flex h-full w-1/2 flex-col items-center justify-center px-8 text-center",
              ].join(" ")}
            >
              <h1 className="text-title font-bold">你好，访客！</h1>
              <p className="mt-3 px-2 text-small leading-relaxed text-white/80">
                还没有账号？点这里创建，体验完整的企业级数据分析能力。
              </p>
              <button
                type="button"
                onClick={toRegister}
                className="pointer-events-auto mt-5 h-[38px] w-[140px] rounded-full border-2 border-solid border-white bg-transparent text-small font-medium text-white transition hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
              >
                创建账号
              </button>
            </div>

            {/* ---- 左侧（register 时显示：引导去登录） ---- */}
            <div
              className={[
                "absolute right-0 flex h-full w-1/2 flex-col items-center justify-center px-8 text-center",
              ].join(" ")}
            >
              <h1 className="text-title font-bold">欢迎回来！</h1>
              <p className="mt-3 px-2 text-small leading-relaxed text-white/80">
                已经有账号了？登录后可以继续使用之前的分析会话。
              </p>
              <button
                type="button"
                onClick={toLogin}
                className="pointer-events-auto mt-5 h-[38px] w-[140px] rounded-full border-2 border-solid border-white bg-transparent text-small font-medium text-white transition hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
              >
                登录
              </button>
            </div>
          </div>
        </div>

        {/* ---- 右上角「跳过」退出键（仅 onSkip 提供时显示） ---- */}
        {onSkip && (
          <button
            type="button"
            onClick={onSkip}
            aria-label="暂不登录（跳过）"
            className="absolute right-3 top-3 z-[60] rounded-control px-2.5 py-1 text-micro font-medium text-ink-3 underline-offset-2 transition hover:text-ink hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
          >
            跳过
          </button>
        )}
      </div>
    </div>
  );
}
