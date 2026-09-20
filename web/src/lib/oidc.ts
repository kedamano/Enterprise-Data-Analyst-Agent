import { useEffect } from "react";

/**
 * 监听 OIDC callback 页面发回的 ``oidc:login`` 事件（callback 页签把 IdP 返回的
 * Bearer token 写到 sessionStorage 后派发）；收到后把 token 迁到 localStorage
 * 并调 onAuted() 让 AuthGate 完成跳转。
 *
 * 放在 App 层即可；callback 页通常是单独 tab，同域下 CustomEvent + sessionStorage
 * 能让 AuthCentre 感知到登录完成。
 */
export function useOidcCallbackListener(onAuthed: () => void) {
  useEffect(() => {
    const handler = (e: Event) => {
      const ev = e as CustomEvent<{ token?: string }>;
      const token = ev.detail?.token ?? sessionStorage.getItem("oidc_token");
      if (!token) return;
      try {
        localStorage.setItem("EDAA_API_KEY", token);
      } catch {
        /* 隐私模式下次步 */
      }
      sessionStorage.removeItem("oidc_token");
      onAuthed();
    };
    window.addEventListener("oidc:login", handler);
    return () => window.removeEventListener("oidc:login", handler);
  }, [onAuthed]);
}
