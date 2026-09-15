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
  Sparkles,
  History,
  Database,
  FolderOpen,
} from "lucide-react";
import type { ReactNode } from "react";

/**
 * 主区域视图标识。左侧细导航点击后**切换主区域内容**（而不是弹小窗）——
 * 知识库/文件库/数据源都是需要大面积操作的重功能，弹窗装不下。
 */
export type RailView = "chat" | "knowledge" | "files" | "datasources";

function Logo() {
  return (
    <div className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-sm font-bold text-white shadow-md shadow-indigo-500/30">
      <Sparkles className="h-4 w-4" />
    </div>
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

export function SideRail({
  view,
  onNavigate,
  onNew,
  onToggleList,
  showList,
  onOpenDocs,
  onOpenHistory,
  onOpenSettings,
}: {
  view: RailView;
  onNavigate: (v: RailView) => void;
  onNew: () => void;
  onToggleList: () => void;
  showList: boolean;
  onOpenDocs: () => void;
  onOpenHistory: () => void;
  onOpenSettings: () => void;
}) {
  return (
    <Sidebar open={false} setOpen={() => undefined} animate={false}>
      <SidebarBody
        className="w-[64px] !w-[64px] shrink-0 border-r border-slate-200 bg-white px-2 py-4"
        style={{ width: 64 }}
      >
        <div className="flex flex-col items-center gap-3">
          <Logo />
          <div className="mt-2 flex flex-col items-center gap-1">
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
          </div>
        </div>

        <div className="mt-auto flex flex-col items-center gap-1.5">
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
              onOpenSettings();
            }}
            title="设置"
            accent="slate"
          >
            <Settings className="h-5 w-5" strokeWidth={2.25} />
          </RailLink>
        </div>
      </SidebarBody>
    </Sidebar>
  );
}
