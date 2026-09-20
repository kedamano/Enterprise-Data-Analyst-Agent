import {
  Sidebar,
  SidebarBody,
} from "@/components/ui/sidebar";
import {
  Plus,
  MessagesSquare,
  BookOpen,
  Settings,
  FileText,
  History,
  Database,
  FolderOpen,
  LogIn,
  Sparkles,
  ServerCog,
  BarChart3,
} from "@/components/icons";
import type { ReactNode } from "react";
import { Avatar } from "@/components/Avatar";
import { roleLabel, useAuth } from "@/lib/user";

/**
 * 主区域视图标识。左侧细导航点击后**切换主区域内容**（而不是弹小窗）——
 * 知识库/文件库/数据源/技能/MCP/设置都是需要大面积操作的重功能，弹窗装不下。
 */
export type RailView =
  | "chat"
  | "knowledge"
  | "files"
  | "datasources"
  | "skills"
  | "mcp"
  | "settings"
  | "analytics";

/** 应用 Logo：品牌图标（web/public/logo.png），同时用于浏览器 favicon。 */
function Logo() {
  return (
    <img
      src="/logo.png"
      alt="企业数据分析智能体"
      width={32}
      height={32}
      draggable={false}
      className="h-8 w-8 select-none rounded-control object-contain shadow-sm ring-1 ring-rule"
    />
  );
}

/**
 * 图标按钮（图标-only 模式），tooltip 用原生 title 属性显示。
 * 不依赖 useSidebar / hover-expand，固定 36px 方块，永远不撑开容器。
 * 配色：浅色 hover 背景 + 紫色高亮，匹配参考图的左侧栏风格。
 */
function RailLink({
  onClick,
  title,
  active,
  accent,
  children,
}: {
  onClick?: (e: React.MouseEvent) => void;
  title: string;
  active?: boolean;
  accent?: "indigo" | "sky" | "amber" | "rose" | "emerald" | "violet" | "slate";
  children: ReactNode;
}) {
  const colors: Record<string, string> = {
    indigo: "bg-brand-soft text-brand",
    sky: "bg-brand-soft text-brand",
    amber: "bg-attention-soft text-attention",
    rose: "bg-danger-soft text-danger",
    emerald: "bg-verified-soft text-verified",
    violet: "bg-brand-soft text-brand",
    slate: "bg-canvas text-ink-2",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-current={active ? "page" : undefined}
      className={`grid h-9 w-9 place-items-center rounded-panel transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand ${
        active
          ? colors[accent ?? "indigo"]
          : "text-ink-3 hover:bg-canvas hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

/**
 * 底部用户区。
 *
 * 刻意**不用 `mt-auto` 贴底**：那是上一版在 900px 高窗口里留下大段空白的原因。
 * 这里始终跟在导航图标下方（用细线分隔），账号状态与导航成为一个连续的视觉块——
 * 窗口变高时下方留白属于"面板的自然余量"，不会插在图标中间。
 */
function UserEntry({
  active,
  onClick,
}: {
  active: boolean;
  onClick: () => void;
}) {
  const { user, ready } = useAuth();

  if (!ready) {
    return <span className="grid h-9 w-9 place-items-center rounded-full bg-canvas" />;
  }

  if (!user) {
    return (
      <button
        type="button"
        onClick={onClick}
        title="登录 / 注册"
        aria-label="登录 / 注册"
        className="grid h-9 w-9 place-items-center rounded-panel text-ink-3 transition hover:bg-canvas hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
      >
        <LogIn className="h-5 w-5" />
      </button>
    );
  }

  const name = user.display_name || user.username;
  return (
    <button
      type="button"
      onClick={onClick}
      title={`${name} · ${roleLabel(user.role)}`}
      aria-label={`账号设置：${name}`}
      aria-current={active ? "page" : undefined}
      className={`grid h-9 w-9 place-items-center rounded-panel transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand ${
        active ? "bg-brand-soft ring-1 ring-brand" : "hover:bg-canvas"
      }`}
    >
      <Avatar user={user} size="xs" />
    </button>
  );
}

export function SideRail({
  view,
  onNavigate,
  onNew,
  onToggleList,
  showList,
  onOpenDocs,
  onOpenHistory,
}: {
  view: RailView;
  onNavigate: (v: RailView) => void;
  onNew: () => void;
  onToggleList: () => void;
  showList: boolean;
  onOpenDocs: () => void;
  onOpenHistory: () => void;
}) {
  return (
    <Sidebar open={false} setOpen={() => undefined} animate={false}>
      <SidebarBody
        className="w-[56px] !w-[56px] shrink-0 flex-col items-center border-r border-rule bg-white px-1.5 py-3"
        style={{ width: 56 }}
      >
        <Logo />

        {/*
          图标紧贴 Logo 连续排列（分 3 组，细线分隔）。
          此前工具组用 mt-auto 贴底，在 900px 高的窗口里会在图标之间留出
          ~500px 纯白空洞 —— 视觉上像「导航图标之间全是空白」。
          也不做垂直居中：居中会把空洞劈成「Logo 下方 + 图标下方」两段，同样扎眼。
        */}
        <div className="mt-2.5 flex flex-col items-center gap-0.5">
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNew();
              onNavigate("chat");
            }}
            title="新对话"
            accent="indigo"
            active={view === "chat"}
          >
            <Plus className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onToggleList();
            }}
            title={showList ? "隐藏会话列表" : "显示会话列表"}
            accent="sky"
            active={showList}
          >
            <MessagesSquare className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onOpenHistory();
            }}
            title="历史记录"
            accent="sky"
          >
            <History className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("datasources");
            }}
            title="数据源"
            accent="emerald"
            active={view === "datasources"}
          >
            <Database className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("knowledge");
            }}
            title="知识库"
            accent="amber"
            active={view === "knowledge"}
          >
            <BookOpen className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("files");
            }}
            title="文件库"
            accent="rose"
            active={view === "files"}
          >
            <FolderOpen className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("skills");
            }}
            title="技能"
            accent="violet"
            active={view === "skills"}
          >
            <Sparkles className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("mcp");
            }}
            title="MCP 服务器"
            accent="indigo"
            active={view === "mcp"}
          >
            <ServerCog className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("analytics");
            }}
            title="分析控制台"
            accent="amber"
            active={view === "analytics"}
          >
            <BarChart3 className="h-5 w-5" />
          </RailLink>

          <span aria-hidden className="my-1.5 h-px w-6 shrink-0 bg-rule" />

          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onOpenDocs();
            }}
            title="使用文档"
            accent="indigo"
          >
            <FileText className="h-5 w-5" />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onNavigate("settings");
            }}
            title="设置"
            accent="slate"
            active={view === "settings"}
          >
            <Settings className="h-5 w-5" />
          </RailLink>

          <span aria-hidden className="my-1.5 h-px w-6 shrink-0 bg-rule" />

          {/* 账号：未登录=登录入口，已登录=头像（点击进设置） */}
          <UserEntry active={view === "settings"} onClick={() => onNavigate("settings")} />
        </div>
      </SidebarBody>
    </Sidebar>
  );
}
