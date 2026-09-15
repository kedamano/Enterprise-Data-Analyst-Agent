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
} from "lucide-react";
import type { ReactNode } from "react";
import { Avatar } from "@/components/Avatar";
import { roleLabel, useAuth } from "@/lib/user";

/**
 * 主区域视图标识。左侧细导航点击后**切换主区域内容**（而不是弹小窗）——
 * 知识库/文件库/数据源/设置都是需要大面积操作的重功能，弹窗装不下。
 */
export type RailView = "chat" | "knowledge" | "files" | "datasources" | "settings";

/** 应用 Logo：品牌图标（web/public/logo.png），同时用于浏览器 favicon。 */
function Logo() {
  return (
    <img
      src="/logo.png"
      alt="企业数据分析智能体"
      width={32}
      height={32}
      draggable={false}
      className="h-8 w-8 select-none rounded-lg object-contain shadow-sm ring-1 ring-slate-200/80"
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
    indigo: "bg-indigo-50 text-indigo-600",
    sky: "bg-sky-50 text-sky-600",
    amber: "bg-amber-50 text-amber-600",
    rose: "bg-rose-50 text-rose-600",
    emerald: "bg-emerald-50 text-emerald-600",
    violet: "bg-violet-50 text-violet-600",
    slate: "bg-slate-100 text-slate-600",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-current={active ? "page" : undefined}
      className={`grid h-9 w-9 place-items-center rounded-xl transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 ${
        active
          ? colors[accent ?? "indigo"]
          : "text-slate-500 hover:bg-slate-100 hover:text-slate-800"
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
    return <span className="grid h-9 w-9 place-items-center rounded-full bg-slate-100" />;
  }

  if (!user) {
    return (
      <button
        type="button"
        onClick={onClick}
        title="登录 / 注册"
        aria-label="登录 / 注册"
        className="grid h-9 w-9 place-items-center rounded-xl text-slate-500 transition hover:bg-slate-100 hover:text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
      >
        <LogIn className="h-5 w-5" strokeWidth={2.25} />
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
      className={`grid h-9 w-9 place-items-center rounded-xl transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 ${
        active ? "bg-indigo-50 ring-1 ring-indigo-200" : "hover:bg-slate-100"
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
        className="w-[56px] !w-[56px] shrink-0 flex-col items-center border-r border-slate-200 bg-white px-1.5 py-3"
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
            <Plus className="h-5 w-5" strokeWidth={2.25} />
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
            <MessagesSquare className="h-5 w-5" strokeWidth={2.25} />
          </RailLink>
          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onOpenHistory();
            }}
            title="历史记录"
            accent="sky"
          >
            <History className="h-5 w-5" strokeWidth={2.25} />
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
            <Database className="h-5 w-5" strokeWidth={2.25} />
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
            <BookOpen className="h-5 w-5" strokeWidth={2.25} />
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
            <FolderOpen className="h-5 w-5" strokeWidth={2.25} />
          </RailLink>

          <span aria-hidden className="my-1.5 h-px w-6 shrink-0 bg-slate-200" />

          <RailLink
            onClick={(e) => {
              e.preventDefault();
              onOpenDocs();
            }}
            title="使用文档"
            accent="indigo"
          >
            <FileText className="h-5 w-5" strokeWidth={2.25} />
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
            <Settings className="h-5 w-5" strokeWidth={2.25} />
          </RailLink>

          <span aria-hidden className="my-1.5 h-px w-6 shrink-0 bg-slate-200" />

          {/* 账号：未登录=登录入口，已登录=头像（点击进设置） */}
          <UserEntry active={view === "settings"} onClick={() => onNavigate("settings")} />
        </div>
      </SidebarBody>
    </Sidebar>
  );
}
