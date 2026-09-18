import { Database, BookOpen, Upload, ShieldCheck } from "@/components/icons";

const EXAMPLES = [
  "对比各区域营收表现，识别增长最快的地区",
  "分析最近半年的月度销售趋势并预测下个月",
  "找出销售额异常波动的产品并解释原因",
  "统计各客户类别的消费占比与复购率",
  "诊断数据质量：缺失值、重复记录与异常分布",
  "查询 orders 表的总行数",
];

/**
 * 三张能力卡各自对应左侧导航里的一个真实资产，不是营销词：
 * 数据源 / 知识库 / 报告溯源。三者互不重复，且都可在本产品内验证。
 */
const HIGHLIGHTS = [
  {
    icon: Database,
    title: "连接真实数据",
    desc: "只读接入主数据库与命名数据源，也可直接上传 CSV / Excel 文件。",
  },
  {
    icon: BookOpen,
    title: "引用业务口径",
    desc: "指标定义、区域划分等口径文档入库后，回答会标出引用来源。",
  },
  {
    icon: ShieldCheck,
    title: "结论可追溯",
    desc: "每条结论附带工具调用与数据依据，可导出交付包供他人复核。",
  },
];

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
    <div className="relative flex h-full flex-col items-center overflow-y-auto bg-canvas px-6">
      {/* 首屏不铺装饰光晕：靛蓝→紫罗兰的径向光晕是生成式界面的公共默认手法，
          且与「用结构而非装饰传达信息」的原则冲突。留白本身也不该被填满。 */}

      {/* 用 CSS 动画而非 motion 编排：即便动画被中断，内容也不会停留在 opacity:0 */}
      {/* my-auto：内容比视口矮时垂直居中，消掉落地页下方的大片空白；比视口高时正常滚动 */}
      <div className="da-fade-up relative z-10 mx-auto my-auto w-full max-w-5xl pb-5 pt-7 text-center">
        {/* 此处原有一个眉标「Enterprise Data Analyst Agent」，与下方主标题同义重复，已移除。
            类型标签只在能补充主标题没说的信息时才有存在意义。 */}
        <h1 className="text-balance text-display font-semibold leading-tight tracking-tight text-ink">
          企业数据分析智能体
        </h1>

        <p className="mx-auto mt-3.5 max-w-2xl text-body leading-relaxed text-ink-3">
          描述一个业务问题，智能体会自行取数、计算并给出结论，每一步都留下可复核的依据。
        </p>

        {/* 能力说明：三张卡各自对应一个真实资产，不用彩色底衬做区分（它们不是状态） */}
        <div className="mt-7 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {HIGHLIGHTS.map(({ icon: Icon, title, desc }) => (
            <div
              key={title}
              className="rounded-panel border border-rule bg-white p-3.5 text-left"
            >
              <div className="mb-2.5 flex items-center gap-2">
                <Icon className="h-5 w-5 shrink-0 text-brand" />
                <h3 className="text-body font-semibold text-ink">{title}</h3>
              </div>
              <p className="text-small leading-relaxed text-ink-3">{desc}</p>
            </div>
          ))}
        </div>

        {/* 快速开始：三个真实资产入口（替代已移除的「工作台」）*/}
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
          {onUpload && (
            <button
              type="button"
              onClick={onUpload}
              className="flex items-center justify-center gap-2 rounded-panel border border-rule-strong bg-white px-4 py-2.5 text-small font-medium text-ink-2 transition hover:border-verified hover:text-verified"
            >
              <Upload className="h-4 w-4" /> 上传文件
            </button>
          )}
          {onAddKnowledge && (
            <button
              type="button"
              onClick={onAddKnowledge}
              className="flex items-center justify-center gap-2 rounded-panel border border-rule-strong bg-white px-4 py-2.5 text-small font-medium text-ink-2 transition hover:border-attention hover:text-attention"
            >
              <BookOpen className="h-4 w-4" /> 添加知识
            </button>
          )}
          {onDataSources && (
            <button
              type="button"
              onClick={onDataSources}
              className="flex items-center justify-center gap-2 rounded-panel border border-rule-strong bg-white px-4 py-2.5 text-small font-medium text-ink-2 transition hover:border-brand hover:text-brand"
            >
              <Database className="h-4 w-4" /> 查看数据源
            </button>
          )}
        </div>

        {/* 示例问题：这六条之间没有先后关系，此前用 1–6 编号是纯粹的装饰 */}
        <div className="mb-3 mt-7 text-left">
          <div className="mb-2.5 flex items-baseline justify-between">
            <span className="text-small font-medium text-ink-2">示例问题</span>
            <span className="hidden text-small text-ink-3 sm:block">
              点击即发送，或在下方输入自己的问题
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => onPick(ex)}
                className="flex items-center rounded-panel border border-rule bg-white px-3.5 py-2.5 text-left text-small text-ink-2 transition hover:border-brand hover:bg-brand-soft hover:text-brand"
              >
                <span className="truncate">{ex}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
