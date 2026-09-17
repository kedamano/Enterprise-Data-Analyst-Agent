import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  KeyRound,
  LogOut,
  Monitor,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import {
  Button,
  Empty,
  Loading,
  LoginPrompt,
  Notice,
  Row,
  Section,
  SectionFooter,
  fmtAgo,
  fmtTime,
  rowInputCls,
} from "./common";
import {
  changePassword,
  fetchMyLogins,
  fetchMySessions,
  logout as apiLogout,
  useAuth,
} from "@/lib/user";
import type { LoginEvent, SessionInfo } from "@/lib/user";

/** 密码强度自检：只做提示，不拦截——真正的规则由后端 `password_policy_error` 判定。 */
function strengthOf(pwd: string): { label: string; cls: string; pct: number } {
  let score = 0;
  if (pwd.length >= 8) score += 1;
  if (pwd.length >= 12) score += 1;
  if (/[a-z]/.test(pwd) && /[A-Z]/.test(pwd)) score += 1;
  if (/\d/.test(pwd)) score += 1;
  if (/[^A-Za-z0-9]/.test(pwd)) score += 1;
  if (score <= 2) return { label: "弱", cls: "bg-danger", pct: 33 };
  if (score === 3) return { label: "中", cls: "bg-attention", pct: 66 };
  return { label: "强", cls: "bg-verified", pct: 100 };
}

function PasswordSection() {
  const { config } = useAuth();
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const minLen = config?.password_min_length ?? 8;
  const mismatch = confirm.length > 0 && newPwd !== confirm;
  const same = newPwd.length > 0 && newPwd === oldPwd;
  const strength = strengthOf(newPwd);
  const canSubmit =
    oldPwd.length > 0 && newPwd.length >= minLen && !mismatch && !same && !busy;

  const submit = async () => {
    setError("");
    setDone("");
    if (mismatch) {
      setError("两次输入的新密码不一致");
      return;
    }
    if (same) {
      setError("新密码不能与当前密码相同");
      return;
    }
    setBusy(true);
    try {
      const revoked = await changePassword(oldPwd, newPwd);
      setOldPwd("");
      setNewPwd("");
      setConfirm("");
      // 说清"其他设备已被踢"——用户会因此看到别的终端掉线，必须提前告知
      setDone(
        revoked > 0
          ? `密码已更新，其他 ${revoked} 台设备已退出登录。`
          : "密码已更新。",
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "修改失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section
      title="登录密码"
      desc="修改后其他设备的登录会立即失效，当前设备保持登录。"
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Row label="当前密码">
          <input
            className={rowInputCls}
            type="password"
            value={oldPwd}
            onChange={(e) => setOldPwd(e.target.value)}
            autoComplete="current-password"
          />
        </Row>
        <Row label="新密码">
          <div className="space-y-2">
            <input
              className={rowInputCls}
              type="password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              autoComplete="new-password"
            />
            <p className="text-micro text-ink-3">至少 {minLen} 位，需含字母与数字</p>
            {newPwd.length > 0 && (
              <div className="flex max-w-sm items-center gap-2">
                <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-canvas">
                  <div
                    className={`h-full rounded-full transition-all ${strength.cls}`}
                    style={{ width: `${strength.pct}%` }}
                  />
                </div>
                <span className="w-6 shrink-0 text-micro text-ink-3">
                  {strength.label}
                </span>
              </div>
            )}
          </div>
        </Row>
        <Row label="确认新密码">
          <input
            className={rowInputCls}
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
          />
        </Row>
        {mismatch && (
          <div className="pt-3">
            <Notice kind="error">两次输入的新密码不一致</Notice>
          </div>
        )}
        {same && (
          <div className="pt-3">
            <Notice kind="error">新密码不能与当前密码相同</Notice>
          </div>
        )}
        {error && (
          <div className="pt-3">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        {done && (
          <div className="pt-3">
            <Notice kind="success">{done}</Notice>
          </div>
        )}
        <SectionFooter>
          <Button type="submit" loading={busy} disabled={!canSubmit}>
            <KeyRound className="h-4 w-4" /> 更新密码
          </Button>
        </SectionFooter>
      </form>
    </Section>
  );
}

function SessionsSection() {
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const res = await fetchMySessions();
      setSessions(res.sessions);
    } catch {
      setSessions([]);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <Section
      title="登录设备"
      desc="当前账号的有效登录会话。发现陌生设备时请立即改密。"
      right={
        <Button variant="secondary" onClick={() => void load()} loading={busy}>
          <RefreshCw className="h-4 w-4" /> 刷新
        </Button>
      }
    >
      {sessions === null ? (
        <Loading what="读取会话" />
      ) : sessions.length === 0 ? (
        <Empty>暂无有效会话</Empty>
      ) : (
        <ul>
          {sessions.map((s, i) => (
            <li
              key={`${s.created_at}-${i}`}
              className="flex items-start gap-3 border-b border-rule py-3 last:border-0"
            >
              <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-control bg-canvas text-ink-3">
                <Monitor className="h-4 w-4" />
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-small text-ink-2" title={s.user_agent}>
                  {s.user_agent || "未知客户端"}
                </p>
                <p className="mt-0.5 text-micro text-ink-3">
                  来源 {s.ip || "—"} · 登录于 {fmtTime(s.created_at)}
                </p>
              </div>
              <span className="shrink-0 text-micro text-ink-3">
                {fmtTime(s.expires_at)} 到期
              </span>
            </li>
          ))}
        </ul>
      )}
      <SectionFooter>
        <Button
          variant="danger"
          onClick={async () => {
            await apiLogout();
          }}
        >
          <LogOut className="h-4 w-4" /> 退出当前设备
        </Button>
      </SectionFooter>
    </Section>
  );
}

function LoginLogSection() {
  const [events, setEvents] = useState<LoginEvent[] | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetchMyLogins(12);
        if (alive) setEvents(res.events);
      } catch {
        if (alive) setEvents([]);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <Section title="登录记录" desc="含失败的登录尝试，用于自查异常登录。">
      {events === null ? (
        <Loading what="读取登录记录" />
      ) : events.length === 0 ? (
        <Empty>暂无登录记录</Empty>
      ) : (
        <ul>
          {events.map((e, i) => {
            const ok = Boolean(e.ok);
            return (
              <li
                key={`${e.ts}-${i}`}
                className="flex items-center gap-3 border-b border-rule py-2.5 last:border-0"
              >
                <span
                  className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                    ok ? "bg-verified" : "bg-danger"
                  }`}
                />
                <span className="w-16 shrink-0 text-small text-ink-2">
                  {ok ? "成功" : "失败"}
                </span>
                <span className="min-w-0 flex-1 truncate text-small text-ink-3">
                  {e.ip || "—"}
                  {e.reason ? ` · ${e.reason}` : ""}
                </span>
                <span className="shrink-0 text-micro text-ink-3">
                  {fmtAgo(e.ts)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

export function SecurityPanel() {
  const { user } = useAuth();
  if (!user) return <LoginPrompt what="密码、登录设备与登录记录" />;

  return (
    <div className="space-y-2">
      <PasswordSection />

      <SessionsSection />
      <LoginLogSection />

      <Section title="安全建议">
        <ul className="pt-3.5">
          <li className="flex items-start gap-2 py-2 text-small leading-relaxed text-ink-2">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-attention" />
            {/* 原先这里直接写环境变量名 AUTH_ENABLED=true —— 那是写给部署者看的，
                不是写给使用者看的。产品界面只说「要做什么」和「找谁做」。 */}
            对外提供服务前，请让管理员开启登录校验；未开启时接口对未登录请求同样开放。
          </li>
          <li className="flex items-start gap-2 border-t border-rule py-2 text-small leading-relaxed text-ink-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-attention" />
            登录令牌保存在浏览器本地存储中，请勿在公共终端保持登录。
          </li>
        </ul>
      </Section>
    </div>
  );
}
