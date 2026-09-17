import { useEffect, useMemo, useState } from "react";
import { Check, Minus } from "lucide-react";
import {
  Badge,
  Loading,
  LoginPrompt,
  Notice,
  Row,
  Section,
} from "./common";
import { fetchRoles, roleLabel, useAuth } from "@/lib/user";
import type { RoleInfo } from "@/lib/user";

/**
 * 把「角色 → 权限」转成「权限 → 角色」矩阵。
 *
 * 后端给的是每个角色各自持有哪些权限；而用户真正要回答的问题是
 * 「这个操作谁能做」，那是行视角。前端转置一次比让后端多开一个接口划算。
 */
interface MatrixRow {
  key: string;
  label: string;
  byRole: Record<string, boolean>;
}

function buildMatrix(roles: RoleInfo[]): MatrixRow[] {
  const rows = new Map<string, MatrixRow>();
  for (const r of roles) {
    r.permissions.forEach((perm, i) => {
      const label = r.permission_labels[i] ?? perm;
      // 同一个权限在不同角色里的 label 应一致；以首次出现的为准
      const row = rows.get(perm) ?? { key: perm, label, byRole: {} };
      row.byRole[r.role] = true;
      rows.set(perm, row);
    });
  }
  return [...rows.values()].sort((a, b) => a.key.localeCompare(b.key));
}

export function PermissionsPanel() {
  const { user } = useAuth();
  const [roles, setRoles] = useState<RoleInfo[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetchRoles();
        if (alive) setRoles(res.roles);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : "读取角色失败");
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const matrix = useMemo(() => buildMatrix(roles ?? []), [roles]);
  const sorted = useMemo(
    () => [...(roles ?? [])].sort((a, b) => b.rank - a.rank),
    [roles],
  );

  if (roles === null) {
    return error ? (
      <Notice kind="error">{error}</Notice>
    ) : (
      <Loading what="读取权限矩阵" />
    );
  }

  const mine = roles.find((r) => r.role === user?.role);

  return (
    <div className="space-y-2">
      {/* 未登录只在认证关闭的部署出现（见 common.tsx 的 LoginPrompt） */}
      <Section
        title="我的角色"
        desc={
          user
            ? "角色决定你能调用哪些工具、访问哪些表与列。"
            : "未登录时展示本服务的完整角色划分。"
        }
      >
        {user ? (
          <>
            <Row
              label="当前角色"
              value={<Badge tone="brand">{roleLabel(user.role)}</Badge>}
            />
            <Row
              label="权限项"
              value={
                mine ? (
                  <span className="flex flex-wrap gap-1.5">
                    {mine.permission_labels.map((l) => (
                      <Badge key={l} tone="neutral">
                        {l}
                      </Badge>
                    ))}
                  </span>
                ) : (
                  <span className="text-ink-3">未取到该角色的权限清单</span>
                )
              }
            />
            <Row
              label="说明"
              value={mine?.summary ?? "角色权限由管理员分配。"}
            />
          </>
        ) : (
          <div className="pt-3.5">
            <LoginPrompt what="你所属的角色" />
          </div>
        )}
      </Section>

      {/* 角色一览：行式而非卡片——4 个角色信息量不大，行比卡片更好横向比较 */}
      <Section title="角色一览" desc="按权限从高到低排列。">
        <ul>
          {sorted.map((r) => {
            const isMine = user?.role === r.role;
            return (
              <li
                key={r.role}
                className="flex items-center gap-3 border-b border-rule py-3 last:border-0"
              >
                <span className="w-20 shrink-0 text-small font-semibold text-ink">
                  {r.label}
                </span>
                <span className="min-w-0 flex-1 text-small leading-relaxed text-ink-3">
                  {r.summary}
                </span>
                <span className="shrink-0 text-micro text-ink-3">
                  {r.permission_labels.length} 项权限
                </span>
                {isMine && <Badge tone="brand">我的</Badge>}
              </li>
            );
          })}
        </ul>
      </Section>

      {/* 权限 × 角色矩阵 */}
      <Section
        title="权限划分矩阵"
        desc="按操作维度展示每个角色是否具备该权限。"
      >
        <div className="overflow-x-auto pt-3.5">
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b border-rule">
                <th className="py-2 pr-4 text-small font-medium text-ink-3">
                  权限
                </th>
                {sorted.map((r) => (
                  <th
                    key={r.role}
                    className={`w-28 px-2 py-2 text-center text-small font-medium ${
                      user?.role === r.role ? "text-brand" : "text-ink-3"
                    }`}
                  >
                    {r.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.map((row) => (
                <tr
                  key={row.key}
                  className="border-b border-rule last:border-0 hover:bg-canvas"
                >
                  <td className="py-2 pr-4">
                    <span className="text-small text-ink-2">{row.label}</span>
                    <span className="ml-2 font-mono text-micro text-ink-3">
                      {row.key}
                    </span>
                  </td>
                  {sorted.map((r) => (
                    <td key={r.role} className="px-2 py-2 text-center">
                      {row.byRole[r.role] ? (
                        <Check
                          className="mx-auto h-4 w-4 text-verified"
                          aria-label="具备"
                        />
                      ) : (
                        <Minus
                          className="mx-auto h-4 w-4 text-ink-3"
                          aria-label="不具备"
                        />
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-micro leading-relaxed text-ink-3">
          权限在服务端强制执行（工具调用、表/列访问、行级过滤），前端仅作展示；
          修改角色请前往「成员管理」。
        </p>
      </Section>
    </div>
  );
}
