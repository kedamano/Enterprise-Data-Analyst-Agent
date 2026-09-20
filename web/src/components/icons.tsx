/**
 * 全站图标统一出口。
 *
 * ## 为什么要有这个文件
 *
 * 图标原本散落在 30+ 个组件里直接 `from "lucide-react"`，导致换图标库要改遍全仓、
 * 而且同一个语义在不同文件里可能挑到不同的图标。这里把「语义名」与「具体库」解耦：
 *
 * - **调用方** 只 import 语义名（如 `Sparkles`、`ServerCog`），永远不关心底层是哪套库；
 * - **换库** 只需要改本文件左侧的映射，全站自动跟随，零改动业务代码。
 *
 * ## 当前实现：Tabler Icons（@tabler/icons-react v3）
 *
 * 6252 个图标、统一 24×24 画布与 2px 描边（round cap / round join），
 * 是目前一致性最好的现代线性图标集之一，与 Lucide 同为描边风格、可 1:1 替换。
 *
 * ## 组件 prop 约定（与 Lucide 的差异，务必注意）
 *
 * | 用途 | Tabler | Lucide（旧） |
 * |---|---|---|
 * | 线宽 | `stroke={2}` | `strokeWidth={2}` |
 * | 颜色 | `color="currentColor"` | 同样靠 `currentColor` |
 * | 尺寸 | `size={24}` | `size={24}` |
 *
 * 实际写代码时**尺寸与颜色一律走 Tailwind class**（`className="h-5 w-5 text-brand"`），
 * 只在需要非默认线宽时才传 `stroke`。`strokeWidth` 也能透传（库把它 spread 到 svg 上），
 * 但那是 Lucide 的写法，本仓统一用 `stroke`。
 *
 * ## 再换一套库怎么做
 *
 * 1. 安装新库（如 `npm i @phosphor-icons/react`）；
 * 2. 只改本文件的 re-export 目标名，**保持左侧语义名不变**；
 * 3. 跑 `npx tsc -b`；名字对不上会直接编译报错，不会静默漏图标。
 */

export {
  // ── 导航 / 全局 ────────────────────────────────────────────────
  IconPlus as Plus,
  IconMessages as MessagesSquare,
  IconMessage as MessageSquare,
  IconHistory as History,
  IconSettings as Settings,
  IconAdjustments as Settings2,
  IconAdjustmentsHorizontal as Sliders,
  IconLayoutSidebarLeftCollapse as PanelLeftClose,
  IconMenu2 as Menu2,
  IconX as X,
  IconSearch as Search,

  // ── 数据 / 分析 ────────────────────────────────────────────────
  IconDatabase as Database,
  IconChartBar as BarChart3,
  IconGauge as Gauge,
  IconTable as Table,
  IconCpu as Cpu,
  IconGitBranch as GitBranch,
  IconListCheck as ListChecks,
  IconBolt as Zap,
  IconRobot as Bot,

  // ── 技能 / 智能 ────────────────────────────────────────────────
  IconSparkles as Sparkles,
  IconServerCog as ServerCog,
  IconTool as Wrench,
  IconPackage as Package,
  IconPackageExport as PackageOpen,

  // ── 知识 / 文件 ────────────────────────────────────────────────
  IconBook as BookOpen,
  IconFile as File,
  IconFileText as FileText,
  IconFileCode as FileCode,
  IconFileCode2 as FileCode2,
  IconFileSpreadsheet as FileSpreadsheet,
  IconFileUpload as FileUp,
  IconFolder as Folder,
  IconFolderOpen as FolderOpen,
  IconFolderPlus as FolderPlus,
  IconServer2 as HardDrive,
  IconPhoto as Image,
  IconUpload as Upload,
  IconDownload as Download,
  IconPaperclip as Paperclip,
  IconPencil as Pencil,
  IconCopy as Copy,
  IconEye as Eye,
  IconWorld as Globe,

  // ── 动作 / 状态 ────────────────────────────────────────────────
  IconCheck as Check,
  IconCircleCheck as CheckCircle2,
  IconCircleX as XCircle,
  IconAlertCircle as AlertCircle,
  IconAlertTriangle as AlertTriangle,
  IconHelpCircle as HelpCircle,
  IconBan as Ban,
  IconCircle as Circle,
  IconMinus as Minus,
  IconSquare as Square,
  IconLoader2 as Loader2,
  IconRefresh as RefreshCw,
  IconSend as Send,
  IconTrash as Trash2,
  IconThumbUp as ThumbsUp,
  IconThumbDown as ThumbsDown,
  IconTrendingUp as TrendingUp,
  IconPlug as Plug,
  IconClock as Clock,
  IconLink as Link2,

  // ── 导航方向 ───────────────────────────────────────────────────
  IconChevronUp as ChevronUp,
  IconChevronDown as ChevronDown,
  IconChevronLeft as ChevronLeft,
  IconChevronRight as ChevronRight,
  IconGripVertical as GripVertical,

  // ── 账号 / 权限 / 安全 ─────────────────────────────────────────
  IconUsers as Users,
  IconUserCog as UserCog,
  IconUserPlus as UserPlus,
  IconLogin as LogIn,
  IconLogout as LogOut,
  IconLock as Lock,
  IconKey as KeyRound,
  IconFingerprint as Fingerprint,
  IconShieldCheck as ShieldCheck,
  IconShieldExclamation as ShieldAlert,
  IconShare as Share2,
  IconDeviceDesktop as Monitor,
  IconCamera as Camera,
  IconPalette as Palette,
} from "@tabler/icons-react";
