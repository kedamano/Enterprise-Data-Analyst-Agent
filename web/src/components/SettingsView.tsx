import { useEffect, useState } from "react";
import {
  Fingerprint,
  Settings,
  ShieldCheck,
  Sliders,
  UserCog,
  Users,
} from "@/components/icons";
import { AccountPanel, AuthEntry } from "./settings/AccountPanel";
import { ExperiencePanel } from "./settings/ExperiencePanel";
import { PermissionsPanel } from "./settings/PermissionsPanel";
import { SecurityPanel } from "./settings/SecurityPanel";
import { TeamPanel } from "./settings/TeamPanel";
import { Loading } from "./settings/common";
import { useAuth } from "@/lib/user";

/**
 * 设置页（主区域页面，非弹窗）。
 *
 * 为什么从弹窗升级成页面：用户体系要做的事（注册登录 / 资料与头像 / 改密与设备 /
 * 权限矩阵 / 成员管理）在 `max-w-lg` 的弹窗里必然要横向滚动或折叠成多级菜单，
 * 与知识库、文件库同为"重操作"界面，形态应该一致。
 *
 * 2026-09-17 重构：顶部页签 + 双层卡片 → **左侧竖向导航 + 右侧扁平行式**。
 * 原来的形态是「一张白卡里再套两张小卡」，盒子套盒子；现在内容直接铺在白色列上，
 * 靠分区标题和细分隔线组织层级。骨架照抄本项目已有视图（FilesView 的「左 aside +
 * 右内容」、DataSourcesView 的 h-14 白色页头），不另创一套。
 *
 * 导航项按**用户角色可见性**划分：任何人都能看到账号、安全、权限、体验；
 * 只有管理员额外看到「成员管理」——权限不足时不是隐藏入口，而是给一张解释卡
 * （见 TeamPanel），否则用户会以为自己找错了地方。
 */
type TabKey = "account" | "security" | "permissions" | "team" | "experience";

const TABS: { key: TabKey; label: string; icon: typeof UserCog; adminOnly?: boolean }[] = [
  { key: "account", label: "账号", icon: Fingerprint },
  { key: "security", label: "安全", icon: ShieldCheck },
  { key: "permissions", label: "权限", icon: UserCog },
  { key: "team", label: "成员管理", icon: Users, adminOnly: true },
  { key: "experience", label: "体验", icon: Sliders },
];

/** 居中窄栏：未登录、加载中这类「整页只说一件事」的形态。 */
function CenteredPane({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-0 flex-1 justify-center overflow-y-auto bg-white">
      <div className="w-full max-w-md px-6 py-12">{children}</div>
    </div>
  );
}

export function SettingsView({
  onClearAll,
  initialTab = "account",
}: {
  onClearAll: () => void;
  initialTab?: TabKey;
}) {
  const { user, config, ready } = useAuth();
  const [tab, setTab] = useState<TabKey>(initialTab);
  const isAdmin = user?.role === "admin";

  // 非管理员误停在「成员管理」（如从管理员账号切过来）→ 退回账号页，
  // 否则会看到一个自己无权进入的导航项被选中。
  useEffect(() => {
    if (tab === "team" && user && !isAdmin) setTab("account");
  }, [tab, user, isAdmin]);

  // 成员管理只对管理员出现；其余项人人可见
  const visible = TABS.filter((t) => !t.adminOnly || isAdmin);

  // 登录态还没查回来：先给占位。否则会先闪一下"未登录"的登录页，再跳成设置页。
  if (!ready) {
    return (
      <div className="flex min-h-0 flex-1 flex-col bg-white">
        <CenteredPane>
          <Loading what="读取登录状态" />
        </CenteredPane>
      </div>
    );
  }

  /**
   * 需要登录但没登录：整页专注登录，**不出导航**。
   *
   * `user_auth_enabled === false` 是例外——那种部署里根本没有"登录"这个动作，
   * `user` 恒为 null。若也拦在登录页，用户就永远进不去「体验」（服务运行信息、
   * 鉴权开关状态都在那儿），那是实打实的功能回退。所以只对真正开了账号体系的
   * 部署做拦截，其余照常出导航，各面板自己给未登录占位。
   */
  if (!user && config?.user_auth_enabled !== false) {
    return (
      <div className="flex min-h-0 flex-1 flex-col bg-white">
        <CenteredPane>
          <AuthEntry />
        </CenteredPane>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas">
      {/* 页头：与数据源页同款 h-14 白条，标题 + 说明 + 角色徽标 */}
      <header className="flex h-14 shrink-0 items-center gap-2.5 border-b border-rule bg-white px-6">
        <Settings className="h-5 w-5 text-ink-3" />
        <h1 className="text-heading font-semibold text-ink">设置</h1>
        <span className="hidden text-small text-ink-3 sm:inline">
          账号、安全、权限与本地体验
        </span>
        <span
          className={`ml-auto hidden shrink-0 rounded-full border px-2.5 py-1 text-micro font-medium sm:inline-flex ${
            isAdmin
              ? "border-attention bg-attention-soft text-attention"
              : "border-rule bg-canvas text-ink-3"
          }`}
        >
          {isAdmin ? "管理员" : "成员"}
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左：竖向导航（选中态是图 2 的浅色圆角块） */}
        <nav
          className="flex w-56 shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-rule bg-white p-3"
          role="tablist"
          aria-orientation="vertical"
        >
          {visible.map(({ key, label, icon: Icon }) => {
            const active = tab === key;
            return (
              <button
                key={key}
                role="tab"
                aria-selected={active}
                onClick={() => setTab(key)}
                className={`flex w-full items-center gap-2.5 rounded-control px-3 py-2 text-left text-small font-medium transition-colors ${
                  active
                    ? "bg-brand-soft text-brand"
                    : "text-ink-2 hover:bg-canvas hover:text-ink"
                }`}
              >
                <Icon
                  className={`h-4 w-4 shrink-0 ${
                    active ? "text-brand" : "text-ink-3"
                  }`}
                />
                {label}
              </button>
            );
          })}
        </nav>

        {/* 右：内容列。白底是刻意的——扁平行式铺在灰 canvas 上会散。 */}
        <div className="min-h-0 flex-1 overflow-y-auto bg-white">
          <div className="mx-auto max-w-3xl px-8 py-6">
            {tab === "account" && <AccountPanel />}
            {tab === "security" && <SecurityPanel />}
            {tab === "permissions" && <PermissionsPanel />}
            {tab === "team" && <TeamPanel />}
            {tab === "experience" && <ExperiencePanel onClearAll={onClearAll} />}
          </div>
        </div>
      </div>
    </div>
  );
}
