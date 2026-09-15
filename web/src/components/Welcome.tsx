import { Sparkles, BarChart3, Bot, Zap, ArrowUpRight, Database, BookOpen, Upload } from "lucide-react";

const EXAMPLES = [
  "查询 orders 表的总行数",
  "对比各区域营收表现，识别增长最快的地区",
  "分析最近半年的月度销售趋势并预测下个月",
  "找出销售额异常波动的产品并解释原因",
  "统计各客户类别的消费占比与复购率",
  "诊断数据质量：缺失值、重复记录与异常分布",
];

const HIGHLIGHTS = [
  {
    icon: Bot,
    title: "六阶段编排",
    desc: "意图理解 → 制定计划 → 执行工具 → 证据分析 → 质检反思 → 生成报告",
    color: "indigo",
  },
  {
    icon: BarChart3,
    title: "全程可追溯",
    desc: "每条结论都附带工具调用证据与置信度，支持溯源审阅",
    color: "sky",
  },
  {
    icon: Zap,
    title: "工具自动调度",
    desc: "SQL / Python / 统计 / 知识检索，按需选用，结果可复核",
    color: "amber",
  },
];

const COLOR_MAP: Record<string, { bg: string; fg: string; ring: string }> = {
  indigo: { bg: "bg-indigo-50", fg: "text-indigo-600", ring: "ring-indigo-100" },
  sky: { bg: "bg-sky-50", fg: "text-sky-600", ring: "ring-sky-100" },
  amber: { bg: "bg-amber-50", fg: "text-amber-600", ring: "ring-amber-100" },
};

/**
 * 落地首页（聊天区为空时展示）。
 * 2026-09-15：原「工作台」已被移除，其价值收口到这里——三个快速入口直接打开
 * 真实资产面板（上传文件 / 加知识 / 连数据源），而不是再放一个只读的 vanity 仪表盘。
 * 说明：这里刻意不放「新建分析」——落地页本身已是空白新会话，再放会堆积空会话。
 */
export function Welcome({
  onPick,
  onUpload,
  onAddKnowledge,
  onDataSources,
}: {
  onPick: (q: string) => void;
  onUpload?: () => void;
  onAddKnowledge?: () => void;
  onDataSources?: () => void;
}) {
  return (
    <div className="relative flex h-full flex-col items-center overflow-y-auto bg-slate-50 px-6">
      {/* 顶部柔和光晕 */}
      <div
        className="pointer-events-none absolute left-1/2 top-6 h-60 w-[560px] -translate-x-1/2 rounded-full opacity-40 blur-3xl"
        style={{
          background:
            "radial-gradient(circle, rgba(99,102,241,0.18) 0%, rgba(139,92,246,0.10) 35%, rgba(255,255,255,0) 70%)",
        }}
      />

      {/* 用 CSS 动画而非 motion 编排：即便动画被中断，内容也不会停留在 opacity:0 */}
      {/* my-auto：内容比视口矮时垂直居中，消掉落地页下方的大片空白；比视口高时正常滚动 */}
      <div className="da-fade-up relative z-10 mx-auto my-auto w-full max-w-5xl pb-5 pt-7 text-center">
        <div className="mx-auto mb-4 inline-flex items-center gap-2 rounded-full border border-indigo-200 bg-white px-3.5 py-1.5 text-[13px] font-medium text-indigo-700 shadow-sm">
          <Sparkles className="h-4 w-4 text-indigo-500" />
          Enterprise Data Analyst Agent
        </div>

        <h1 className="text-balance bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-700 bg-clip-text text-[28px] font-semibold leading-tight tracking-tight text-transparent sm:text-[34px]">
          企业数据分析智能体
        </h1>

        <p className="mx-auto mt-3 max-w-2xl text-[14.5px] leading-relaxed text-slate-500">
          用自然语言驱动「意图理解 → 计划 → 取数 → 分析 → 质检 → 报告」六阶段编排。
          描述你的业务问题，智能体自动调用工具、取证并产出可读报告。
        </p>

        {/* 能力高亮卡片 */}
        <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {HIGHLIGHTS.map(({ icon: Icon, title, desc, color }) => {
            const c = COLOR_MAP[color] ?? COLOR_MAP.indigo;
            return (
              <div
                key={title}
                className="rounded-xl border border-slate-200 bg-white p-3.5 text-left shadow-sm shadow-slate-200/50"
              >
                <div
                  className={`mb-2.5 grid h-9 w-9 place-items-center rounded-lg ${c.bg} ring-1 ${c.ring}`}
                >
                  <Icon className={`h-5 w-5 ${c.fg}`} />
                </div>
                <h3 className="text-[14px] font-semibold text-slate-900">{title}</h3>
                <p className="mt-1 text-[12.5px] leading-relaxed text-slate-500">
                  {desc}
                </p>
              </div>
            );
          })}
        </div>

        {/* 快速开始：三个真实资产入口（替代已移除的「工作台」）*/}
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {onUpload && (
            <button
              type="button"
              onClick={onUpload}
              className="flex items-center justify-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-[13.5px] font-medium text-emerald-700 transition hover:bg-emerald-100"
            >
              <Upload className="h-4 w-4" /> 上传文件
            </button>
          )}
          {onAddKnowledge && (
            <button
              type="button"
              onClick={onAddKnowledge}
              className="flex items-center justify-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-[13.5px] font-medium text-amber-700 transition hover:bg-amber-100"
            >
              <BookOpen className="h-4 w-4" /> 添加知识
            </button>
          )}
          {onDataSources && (
            <button
              type="button"
              onClick={onDataSources}
              className="flex items-center justify-center gap-2 rounded-xl border border-sky-200 bg-sky-50 px-4 py-2.5 text-[13.5px] font-medium text-sky-700 transition hover:bg-sky-100"
            >
              <Database className="h-4 w-4" /> 查看数据源
            </button>
          )}
        </div>

        {/* 示例问题 */}
        <div className="mb-3 mt-6 text-left">
          <div className="mb-2.5 flex items-center justify-between">
            <span className="text-[12px] font-medium uppercase tracking-wider text-slate-500">
              示例问题 · 点击直接发送给智能体
            </span>
            <span className="hidden text-[12px] text-slate-400 sm:block">
              或在下方直接提问 / 上传 CSV 文件
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
            {EXAMPLES.map((ex, i) => (
              <button
                key={ex}
                style={{ animationDelay: `${i * 45}ms` }}
                onClick={() => onPick(ex)}
                className="group flex items-center justify-between gap-2 rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-left text-[13px] text-slate-600 shadow-sm shadow-slate-200/50 transition hover:border-indigo-300 hover:bg-indigo-50/50 hover:text-indigo-700"
              >
                <span className="flex min-w-0 items-center gap-2.5">
                  <span className="grid h-5 w-5 shrink-0 place-items-center rounded-md bg-slate-100 text-[11px] font-mono text-slate-500 group-hover:bg-indigo-100 group-hover:text-indigo-600">
                    {i + 1}
                  </span>
                  <span className="truncate">{ex}</span>
                </span>
                <ArrowUpRight className="h-4 w-4 shrink-0 text-slate-300 transition group-hover:text-indigo-500" />
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
