import { useCallback, useEffect, useState } from "react";
import {
  Ban,
  CheckCircle2,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  Trash2,
  Users,
} from "lucide-react";
import { Avatar } from "@/components/Avatar";
import {
  Badge,
  Button,
  Loading,
  LoginPrompt,
  Notice,
  Section,
  fmtTime,
  inputCls,
} from "./common";
import {
  adminDeleteUser,
  adminUpdateUser,
  fetchUsers,
  roleLabel,
  useAuth,
} from "@/lib/user";
import type { AuthUser } from "@/lib/user";

const ROLES = ["viewer", "analyst", "analyst_lead", "admin"] as const;

const selectCls =
  "rounded-control border border-rule bg-white px-2.5 py-1.5 text-small text-ink-2 outline-none transition focus:border-brand";

function RoleSelect({
  user,
  me,
  onDone,
}: {
  user: AuthUser;
  me: AuthUser;
  onDone: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // 自降级由后端权威拦截（还要管"最后一名管理员"），这里只禁自己这一项，
  // 让交互诚实：明知不可为就不给点。
  const isSelf = user.id === me.id;

  return (
    <div className="flex items-center gap-2">
      <select
        value={user.role}
        disabled={busy || isSelf}
        title={isSelf ? "不能修改自己的角色" : undefined}
        onChange={async (e) => {
          setError("");
          setBusy(true);
          try {
            const updated = await adminUpdateUser(user.id, { role: e.target.value });
            onDone(`${updated.username} 的角色已改为 ${roleLabel(updated.role)}`);
          } catch (err) {
            setError(err instanceof Error ? err.message : "修改失败");
          } finally {
            setBusy(false);
          }
        }}
        className={`${selectCls} disabled:cursor-not-allowed disabled:bg-canvas disabled:text-ink-3`}
      >
        {ROLES.map((r) => (
          <option key={r} value={r}>
            {roleLabel(r)}
          </option>
        ))}
      </select>
      {busy && <Loader2 className="h-4 w-4 animate-spin text-ink-3" />}
      {error && (
        <span className="text-micro text-danger" title={error}>
          {error}
        </span>
      )}
    </div>
  );
}

export function TeamPanel() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [q, setQ] = useState("");
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const res = await fetchUsers();
      setUsers(res.users);
      setForbidden(false);
    } catch (err) {
      // 403 = 非管理员。这是正常状态，不是故障——给出解释而非报错。
      const status = (err as { status?: number }).status;
      if (status === 403) setForbidden(true);
      else setError(err instanceof Error ? err.message : "读取成员失败");
      setUsers([]);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(""), 2600);
    return () => window.clearTimeout(t);
  }, [flash]);

  if (!me) return <LoginPrompt what="团队成员与角色分配" />;

  if (forbidden) {
    return (
      <Section title="团队成员">
        <div className="flex flex-col items-center gap-3 py-12 text-center">
          <span className="grid h-12 w-12 place-items-center rounded-full bg-canvas text-ink-3">
            <ShieldAlert className="h-6 w-6" />
          </span>
          <p className="text-body font-semibold text-ink">仅管理员可管理成员</p>
          <p className="max-w-md text-small leading-relaxed text-ink-3">
            你当前的角色是 <strong>{roleLabel(me.role)}</strong>，没有查看成员列表的权限。
            如需调整，请联系管理员把你的角色提升为「管理员」。
          </p>
        </div>
      </Section>
    );
  }

  const filtered = (users ?? []).filter((u) => {
    if (role && u.role !== role) return false;
    if (status && u.status !== status) return false;
    if (!q.trim()) return true;
    const kw = q.trim().toLowerCase();
    return (
      u.username.toLowerCase().includes(kw) ||
      u.display_name.toLowerCase().includes(kw) ||
      u.email.toLowerCase().includes(kw)
    );
  });

  return (
    <div className="space-y-2">
      {flash && <Notice kind="success">{flash}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}

      <Section
        title="团队成员"
        desc={`共 ${users?.length ?? 0} 个账号。角色变更立即生效，无需对方重新登录。`}
        right={
          <Button variant="secondary" onClick={() => void load()} loading={busy}>
            <RefreshCw className="h-4 w-4" /> 刷新
          </Button>
        }
      >
        {/* 工具条 */}
        <div className="flex flex-wrap items-center gap-2 pt-3.5">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-3" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索用户名 / 昵称 / 邮箱"
              aria-label="搜索成员"
              className={`${inputCls} w-60 py-1.5 pl-8`}
            />
          </div>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className={selectCls}
          >
            <option value="">角色 全部</option>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {roleLabel(r)}
              </option>
            ))}
          </select>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className={selectCls}
          >
            <option value="">状态 全部</option>
            <option value="active">正常</option>
            <option value="disabled">已停用</option>
          </select>
          {(q || role || status) && (
            <button
              onClick={() => {
                setQ("");
                setRole("");
                setStatus("");
              }}
              className="text-small text-ink-3 hover:text-ink"
            >
              清除筛选
            </button>
          )}
          <span className="ml-auto text-small text-ink-3">
            显示 {filtered.length} / {users?.length ?? 0}
          </span>
        </div>

        {users === null ? (
          <Loading what="读取成员" />
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Users className="h-6 w-6 text-ink-3" />
            <p className="text-small text-ink-3">
              {users.length === 0 ? "暂无成员" : "没有匹配的成员"}
            </p>
          </div>
        ) : (
          <div className="mt-1 overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-rule text-micro text-ink-3">
                  <th className="py-2 pr-3 font-medium">成员</th>
                  <th className="w-32 px-3 py-2 font-medium">角色</th>
                  <th className="w-24 px-3 py-2 font-medium">状态</th>
                  <th className="w-32 px-3 py-2 font-medium">最近登录</th>
                  <th className="w-24 px-3 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((u) => {
                  const isSelf = u.id === me.id;
                  const disabled = u.status !== "active";
                  return (
                    <tr
                      key={u.id}
                      className="border-b border-rule last:border-0 hover:bg-canvas"
                    >
                      <td className="py-2.5 pr-3">
                        <div className="flex items-center gap-2.5">
                          <Avatar user={u} size="sm" />
                          <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate text-small font-medium text-ink">
                                {u.display_name || u.username}
                              </span>
                              {isSelf && <Badge tone="neutral">我</Badge>}
                            </div>
                            <p className="truncate text-micro text-ink-3">
                              @{u.username}
                              {u.email ? ` · ${u.email}` : ""}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <RoleSelect user={u} me={me} onDone={setFlash} />
                      </td>
                      <td className="px-3 py-2.5">
                        <Badge tone={disabled ? "danger" : "verified"}>
                          {disabled ? (
                            <Ban className="h-4 w-4" />
                          ) : (
                            <CheckCircle2 className="h-4 w-4" />
                          )}
                          {disabled ? "已停用" : "正常"}
                        </Badge>
                      </td>
                      <td className="px-3 py-2.5 text-micro text-ink-3">
                        {u.last_login_at ? fmtTime(u.last_login_at) : "从未登录"}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            disabled={isSelf}
                            title={isSelf ? "不能停用自己的账号" : undefined}
                            onClick={async () => {
                              setError("");
                              try {
                                await adminUpdateUser(u.id, {
                                  status: disabled ? "active" : "disabled",
                                });
                                setFlash(
                                  `${u.username} 已${disabled ? "启用" : "停用"}`,
                                );
                                await load();
                              } catch (err) {
                                setError(err instanceof Error ? err.message : "操作失败");
                              }
                            }}
                          >
                            {disabled ? "启用" : "停用"}
                          </Button>
                          <Button
                            variant="ghost"
                            disabled={isSelf}
                            title={isSelf ? "不能删除自己的账号" : undefined}
                            onClick={async () => {
                              if (
                                !window.confirm(
                                  `确定删除成员「${u.display_name || u.username}」？该操作不可撤销，其登录会话将立即失效。`,
                                )
                              )
                                return;
                              setError("");
                              try {
                                await adminDeleteUser(u.id);
                                setFlash(`${u.username} 已删除`);
                                await load();
                              } catch (err) {
                                setError(err instanceof Error ? err.message : "删除失败");
                              }
                            }}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="管理员须知">
        <ul className="pt-3.5">
          {[
            "角色变更与停用立即生效，对方无需重新登录。",
            "为避免把系统锁死，服务端禁止：修改自己的角色、停用/删除自己、移除最后一名管理员。",
            "停用账号会同时吊销其全部登录会话，但保留历史数据。",
          ].map((line, i) => (
            <li
              key={line}
              className={`py-2 text-small leading-relaxed text-ink-2 ${
                i > 0 ? "border-t border-rule" : ""
              }`}
            >
              {line}
            </li>
          ))}
        </ul>
      </Section>
    </div>
  );
}
