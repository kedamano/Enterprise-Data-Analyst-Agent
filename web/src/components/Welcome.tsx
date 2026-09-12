import { Sparkles, BarChart3, Bot, Zap, ArrowUpRight } from "lucide-react";

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

export function Welcome({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="relative flex h-full flex-col items-center overflow-y-auto bg-slate-50 px-6">
      {/* 顶部柔和光晕 */}
      <div
        className="pointer-events-none absolute left-1/2 top-12 h-72 w-[640px] -translate-x-1/2 rounded-full opacity-40 blur-3xl"
        style={{
          background:
            "radial-gradient(circle, rgba(99,102,241,0.18) 0%, rgba(139,92,246,0.10) 35%, rgba(255,255,255,0) 70%)",
        }}
      />

      {/* 用 CSS 动画而非 motion 编排：即便动画被中断，内容也不会停留在 opacity:0 */}
      <div className="da-fade-up relative z-10 mx-auto w-full max-w-3xl pt-12 pb-4 text-center">
        <div className="mx-auto mb-5 inline-flex items-center gap-2 rounded-full border border-indigo-200 bg-white px-3 py-1 text-xs font-medium text-indigo-700 shadow-sm">
          <Sparkles className="h-3.5 w-3.5 text-indigo-500" />
          Enterprise Data Analyst Agent
        </div>

        <h1 className="text-balance bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-700 bg-clip-text text-3xl font-semibold leading-tight tracking-tight text-transparent sm:text-4xl">
          企业数据分析智能体
        </h1>

        <p className="mx-auto mt-3 max-w-xl text-[14.5px] leading-relaxed text-slate-500">
          用自然语言驱动「意图理解 → 计划 → 取数 → 分析 → 质检 → 报告」六阶段编排。
          描述你的业务问题，智能体自动调用工具、取证并产出可读报告。
        </p>

        {/* 能力高亮卡片 */}
        <div className="mt-7 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {HIGHLIGHTS.map(({ icon: Icon, title, desc, color }) => {
            const c = COLOR_MAP[color] ?? COLOR_MAP.indigo;
            return (
              <div
                key={title}
                className="rounded-2xl border border-slate-200 bg-white p-4 text-left shadow-sm shadow-slate-200/50"
              >
                <div
                  className={`mb-2.5 grid h-9 w-9 place-items-center rounded-xl ${c.bg} ring-1 ${c.ring}`}
                >
                  <Icon className={`h-5 w-5 ${c.fg}`} />
                </div>
                <h3 className="text-[13.5px] font-semibold text-slate-900">{title}</h3>
                <p className="mt-1 text-[12.5px] leading-relaxed text-slate-500">
                  {desc}
                </p>
              </div>
            );
          })}
        </div>

        {/* 示例问题 */}
        <div className="mt-8 mb-4 text-left">
          <div className="mb-3 flex items-center justify-between">
            <span className="text-[12px] font-medium uppercase tracking-wider text-slate-500">
              示例问题 · 点击直接发送给智能体
            </span>
            <span className="text-[11px] text-slate-400">
              或在下方直接提问 / 上传 CSV 文件
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            {EXAMPLES.map((ex, i) => (
              <button
                key={ex}
                style={{ animationDelay: `${i * 45}ms` }}
                onClick={() => onPick(ex)}
                className="group flex items-center justify-between rounded-xl border border-slate-200 bg-white px-4 py-3 text-left text-[13px] text-slate-600 shadow-sm shadow-slate-200/50 transition hover:border-indigo-300 hover:bg-indigo-50/50 hover:text-indigo-700"
              >
                <span className="flex items-center gap-2">
                  <span className="grid h-5 w-5 place-items-center rounded-md bg-slate-100 text-[11px] font-mono text-slate-500 group-hover:bg-indigo-100 group-hover:text-indigo-600">
                    {i + 1}
                  </span>
                  <span className="truncate">{ex}</span>
                </span>
                <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-slate-300 transition group-hover:text-indigo-500" />
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
