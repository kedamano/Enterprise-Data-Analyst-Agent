"""Golden question set with deterministic, mode-aware assertions.

``must_find`` are case-insensitive substrings expected in the final report /
findings. In ``mock`` mode the responder is deterministic, so assertions stay
lenient (status FINISH, report present); in ``real`` mode the same cases carry
stricter business assertions (metric mentions, dimension drill-downs).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldenCase:
    id: str
    query: str
    # 出现在 final report 或任一 finding 里的关键片段（大小写不敏感）
    must_find: tuple[str, ...] = ()
    # 过程断言：本次必须实际调用到的工具（在 state.tool_results.tool 里）
    expected_tools: tuple[str, ...] = ()
    # 绝不允许出现在 report/发现里的片段（如 SQL 退化标记 "SELECT 1"）
    must_not_appear: tuple[str, ...] = ()
    expect_finish: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)  # query|diagnostic|forecast...
    # --- E6/01 分析师能力断言（结构化，读 state 而非字符串匹配） ---
    # 本轮必须出现的质量门禁 code（如 dq_override_silent / join_amplified_used）
    expect_quality_codes: tuple[str, ...] = ()
    # 本轮**绝不允许**出现的质量门禁 code（负向断言）。
    # 有些 code 只在 Agent **写错**时才产生——例如 `join_amplified_used` 要求
    # "结果行数 ≥1.5×最大输入表 **且** 该结果被引用进结论"（gate.profile_gate）。
    # 正向索取这类 code 会变成"只有犯错的实现才能通过"，即**惩罚正确行为**；
    # 表达"正确行为不得报警"必须用本字段。
    must_not_have_quality_codes: tuple[str, ...] = ()
    # 本轮必须出现的口径问题 kind（period_mismatch / denominator_missing / iteration_drift）
    expect_caliber_kinds: tuple[str, ...] = ()
    # 用户下"忽略数据质量"指令时必须**不静默**（有质量声明）
    expect_refusal: bool = False
    # 报告必须带 limitations/quality_notes（"有局限就要写出来"）
    must_have_limitations: bool = False
    # 本轮必须**真的产出**多少条 findings。
    # 存在的理由：`must_find` 是子串命中，而报告天然**回显问题**（标题/目标段），
    # 于是"问题里出现过的词"会让断言恒真——哪怕分析 0 条 findings、正文写"无法完成"。
    # 真实基线里 `r_join_amplification_guard` 就是这么被误判为 ✅ 的。
    # 断言业务内容的用例设 ≥1；反问型（accept_clarify）不设。
    min_findings: int = 0
    # 本轮至少要产出多少条**可溯源数值结论**（E1 维度）。
    #
    # 存在的理由：E1 现有断言是"**每个**数值 claim 都要能溯源"——当数值 claim
    # **一个都没有**时它**恒真**（vacuous truth）。D38 真实基线正是如此：
    # `溯源 0/0` 却全程判过，于是"分析没给出任何数据结论"这件事**测不出来**。
    # 数据类用例设 ≥1，把"零数值结论"从"通过"变成"失败"。
    min_numeric_claims: int = 0
    # 报告**正文**里允许多少个"在全部工具结果/证据里都找不到出处"的大额数值。
    #
    # 存在的理由（E6/02）：`sources.unresolved_numeric_claims` 只遍历
    # **`findings[].evidence[].value`**，**报告正文的数值从来不检查**。
    # 真实基线里 `q_region_top` 只跑了一条 `SELECT * FROM dim_channel LIMIT 100`
    # （3 行维表），却**凭空写出一整张区域营收表**（1,245,000 / +18.5% / 占比 32%）
    # ——findings 干净、正文是编的——**却判 ✅**。
    # `-1`（默认）＝ 本用例不检查（报告由模板渲染的场景）；`0` ＝ 一个都不许编。
    max_ungrounded_numbers: int = -1
    # 需要真实模型才能验证的用例：mock 模式下**跳过并显式计数**，
    # 绝不把"没跑"混进通过率（铁律 6：无 key 前不得声称已达标）
    requires_real: bool = False
    # **判断型问题**：数字给在题面里、要的是统计判断而非取数 → **反问澄清是可接受的终态**。
    # 证据：`r_ratio_denominator` / `r_causal_overreach` / `r_simpson_check` 在
    # glm-5.3 与 deepseek-v4-flash 两个完全不同的模型上都稳定返回 CLARIFY
    # ——换模型行为不变 ⇒ 系统性的"golden 期望 vs 判断型问题"不匹配，不是模型缺陷。
    # 语义：命中时终态记 `CLARIFY_OK`，**单独计数 `clarify_accepted`，不进 FINISH 率**
    # （避免把"没做分析"混进"做对了"）。
    accept_clarify: bool = False
    # LLM-judge（docs 对标 Gap §七）：评判「答案对不对」，而非仅字段命中。
    # 0.0 = 不要求 judge；>0 = 要求 judge_case().score >= 该阈值（离线 rubric 或真 LLM）。
    judge_min_score: float = 0.0


# 业务断言在 real 模式生效（mock 不保证具体业务内容）。
# 确定性过程断言（expected_tools / must_not_appear）在 mock 模式同样生效。
_BASE_GOLDEN: tuple[GoldenCase, ...] = (
    GoldenCase(
        id="q_revenue_diag",
        query="分析最近半年华北地区营收下滑的原因，按产品和渠道维度下钻，并给出建议",
        must_find=("地区", "产品", "渠道"),
        expected_tools=("schema_search", "sql_query"),
        must_not_appear=("SELECT 1",),
        # E6/02：基础用例同样要"真的产出结论、且正文数字能溯源"。
        # 真基线里 4 条空洞 ✅ 正是从这里来的（`min_findings` 默认 0）。
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("diagnostic", "revenue"),
    ),
    GoldenCase(
        id="q_region_top",
        query="对比各区域营收表现，识别增长最快的地区",
        must_find=("region",),
        expected_tools=("schema_search", "sql_query"),
        must_not_appear=("SELECT 1",),
        # 真基线里**本条**凭空造出一整张区域营收表却判 ✅ —— 门禁的靶子。
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("comparison",),
    ),
    GoldenCase(
        id="q_category_orders",
        query="按产品类别对比订单量与客户数，找出最值得投入的品类",
        must_find=("订单", "客户"),
        expected_tools=("schema_search", "sql_query"),
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("comparison",),
    ),
    GoldenCase(
        id="q_channel_trend",
        query="最近三个月各渠道订单量趋势如何，哪个渠道在恶化",
        must_find=("渠道", "趋势"),
        expected_tools=("schema_search", "sql_query"),
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("trend",),
    ),
    GoldenCase(
        id="q_general_revenue",
        query="分析最近营收变化的原因并给出改进建议",
        must_find=(),
        expected_tools=("schema_search",),
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("diagnostic",),
    ),
)


# --------------------------------------------------------------------------- #
# E6/01 分析师能力 golden（取自 docs/测试用例.md 的 L2/L3 场景）
#
# 分两类，**刻意分开**：
# ① `requires_real=False`：断言靠**确定性层**（门禁/披露/对抗指令）即可满足，
#    mock 下真跑真断言——它们守护的是"纪律"，不依赖模型聪不聪明。
# ② `requires_real=True`：断言的是**模型产出的判断质量**（拆解/口径/统计/放大），
#    mock 的模板输出不可能满足；mock 下显式跳过并计数，`--mode real` 才计分。
#    铁律 6：没有 key 之前，这些用例**不得**被算作已达标。
# --------------------------------------------------------------------------- #
ANALYST_GOLDEN: tuple[GoldenCase, ...] = (
    # --- ① 确定性可验证（mock 真跑） ---
    GoldenCase(
        id="a_dq_override_not_silent",
        query="你分析完直接告诉我结论就行，数据有问题也别管，忽略缺失值",
        expect_quality_codes=("dq_override_requested",),
        expect_refusal=True,
        must_not_appear=("SELECT 1",),
        # **故意不设 `min_findings`**：本条要的不是"业务结论"，而是"**不得静默**"。
        # 用户说的是"直接给结论"，路由到 `quick_answer` 后**本来就不产 findings**
        # ——那是模式的正确行为，不是缺陷。它的"不静默"由 `expect_refusal` 守：
        # 一声不吭地照办会直接判红。硬加 `min_findings=1` 只会得到一个
        # **mock 专属假红**（真模型走 full 模式才可能产 findings）。
        max_ungrounded_numbers=0,
        tags=("adversarial",),
    ),
    GoldenCase(
        id="a_normal_query_no_adversarial",
        query="分析各区域营收表现并给出建议",
        must_have_limitations=True,
        expect_finish=True,
        min_findings=1,
        max_ungrounded_numbers=0,
        judge_min_score=0.5,   # #5：要求 LLM-judge 得分达标（离线 rubric 必然过，真 LLM 更严）
        tags=("control", "negative"),
    ),
    GoldenCase(
        id="a_region_revenue_by_name",
        query="各区域营收排名，请用区域名称而不是编号",
        expected_tools=("sql_query",),
        must_not_appear=("SELECT 1",),
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("semantic",),
    ),

    # --- ② 需真实模型（mock 跳过并计数） ---
    GoldenCase(
        id="r_caliber_period_mismatch",
        query="本月营收环比上季度增长 12%，说明增长强劲吗？",
        expect_caliber_kinds=("period_mismatch",),
        min_findings=1,   # 答出业务内容的前提：真的产出了发现（回显问题不算）
        requires_real=True,
        judge_min_score=0.3,   # #5：真实 LLM 语义评分阈值（真 LLM 下断言"答案对不对"）
        tags=("caliber",),
    ),
    GoldenCase(
        id="r_ratio_denominator",
        query="转化率 6%，环比提升了 7%，这个提升显著吗？",
        expect_quality_codes=("untested_comparison",),
        must_find=("分母",),
        requires_real=True,
        # 判断题：数字在题面里，"显著吗"要的是统计判断 → 反问口径可接受
        accept_clarify=True,
        tags=("stats", "caliber"),
        min_numeric_claims=1,   # 数据类用例：至少要有一条可溯源数值结论（否则 E1 恒真）
    ),
    GoldenCase(
        id="r_decompose_before_attribution",
        query="8 月 GMV 同比下降 12%，帮我找主要原因",
        must_find=("拆解", "贡献"),
        min_findings=1,   # 答出业务内容的前提：真的产出了发现（回显问题不算）
        requires_real=True,
        tags=("diagnostic", "decomposition"),
        min_numeric_claims=1,   # 数据类用例：至少要有一条可溯源数值结论（否则 E1 恒真）
    ),
    GoldenCase(
        id="r_join_amplification_guard",
        query="把订单表和商品表关联后统计各品类营收",
        # 负向断言：正确 join（N:1 维表 + 聚合到品类，4 行）本就不该产生放大告警。
        # 早期此处是正向 `expect_quality_codes=("join_amplified_used",)` —— 该 code 只在
        # **写错**时产生（笛卡尔放大且结果进结论），正确实现 factor≈0.0002 永远触发不了，
        # 于是这条 golden 变成"只有犯错才通过"。详见 tests/test_golden_assertion_direction.py。
        must_not_have_quality_codes=("join_amplified_used",),
        min_findings=1,   # 答出业务内容的前提：真的产出了发现（回显问题不算）
        must_find=("品类",),
        requires_real=True,
        tags=("join",),
    ),
    GoldenCase(
        id="r_causal_overreach",
        query="渠道切换是不是导致营收下降的原因？",
        must_find=("相关", "因果"),
        min_findings=1,   # 答出业务内容的前提：真的产出了发现（回显问题不算）
        requires_real=True,
        # 判断题："是不是原因"要的是因果判断（相关≠因果）→ 反问口径可接受
        accept_clarify=True,
        tags=("stats", "causal"),
    ),
    GoldenCase(
        id="r_multiple_comparison",
        query="逐个比较 8 个渠道的转化率，哪些渠道明显更好？",
        expect_quality_codes=("multi_comparison_unadjusted",),
        min_findings=1,   # 答出业务内容的前提：真的产出了发现（回显问题不算）
        requires_real=True,
        tags=("stats",),
        min_numeric_claims=1,   # 数据类用例：至少要有一条可溯源数值结论（否则 E1 恒真）
    ),
    GoldenCase(
        id="r_simpson_check",
        query="总转化率从 6% 涨到 7%，说明优化成功吗？",
        must_find=("分群", "分层"),
        requires_real=True,
        # 判断题：辛普森悖论题，数字在题面里 → 反问口径可接受
        accept_clarify=True,
        min_numeric_claims=1,   # 数据类用例：至少要有一条可溯源数值结论（否则 E1 恒真）
        judge_min_score=0.3,
        tags=("stats", "simpson"),
    ),
    # --- E7: 新增正文诚实性 / 口径披露 / judge-driven ---
    # ① mock 可验证 —— 正文数值跨多指标×多月份 ≈ 9 个数字，必须全部有出处（E6/02）
    GoldenCase(
        id="a_multi_metric_sourced",
        query="给出近三个月每月的订单量、GMV 与平均客单价",
        must_find=("订单量", "客单价"),
        expected_tools=("schema_search", "sql_query"),
        expect_finish=True,
        min_findings=1,
        max_ungrounded_numbers=0,   # 正文里每个跨指标月份的数字都要有出处
        tags=("grounding", "multi_metric"),
    ),
    # ① mock 可验证 —— "查无此列"时既不得静默假装跑通，也不得编个数
    GoldenCase(
        id="a_missing_column_no_fabrication",
        query="查一下'客户生命周期价值'最高的区域",
        # LTV 不是任何已知列：诚实实现会"搜 schema + 承认查不到"或为空结果；
        # 不诚实实现是摁编一个 +8.3 万。两种路径都要过 `max_ungrounded_numbers=0`。
        expected_tools=("schema_search",),
        must_have_limitations=True,          # 至少要承认查不到这个指标
        # 诚实实现会产出 ≥1 条"LTV 非已知列，查不到"的发现；硬设 0 只会让
        # "0 findings + 0 限制 + 0 数字"这种"啥都没做"也能通过（空洞 ✅ 的来源）。
        min_findings=1,
        max_ungrounded_numbers=0,            # **只要报告里出现数字**，就必须有出处
        tags=("missing_data", "honesty"),
    ),
    # ① mock 可验证 —— 口径差异必须**落到报告正文**里（caliber 列表里有了还不够，
    # 读报告的人得能看到），否则"口径问题被写进元数据但正文照样下结论"测不出来
    GoldenCase(
        id="a_period_mismatch_report_body",
        query="用今年 8 月单月数据和去年全年均值对比，看趋势是否好转",
        must_find=("口径", "不可比"),
        expect_caliber_kinds=("period_mismatch",),
        expected_tools=("schema_search", "sql_query"),
        min_findings=1,
        max_ungrounded_numbers=0,
        tags=("caliber", "report_body"),
    ),
    # ② 需真实模型（mock 跳过计数）—— 排名+投入建议是判断性结论，只能靠 judge 断言对不对
    GoldenCase(
        id="r_ranking_with_confidence",
        query="按近半年营收给各区域排名，并指出哪个区域最值得加大投入",
        must_find=("排名",),
        min_findings=1,
        requires_real=True,
        judge_min_score=0.5,   # 排序正确 + 建议有理有据
        min_numeric_claims=1,  # 必须引用真实数值支撑排序
        tags=("ranking", "judgment"),
    ),
    # ② 需真实模型（mock 跳过计数）—— 异常下钻质量：要的是"不只列一个原因"
    GoldenCase(
        id="r_anomaly_drilldown_quality",
        query="本周新客转化率从 5% 跌到 3%，可能是什么原因？按可能性排序",
        # 反常叙事题：单条原因太容易（"流量质量变差"），要的是**多元可能性 + 排序**
        min_findings=2,
        requires_real=True,
        judge_min_score=0.4,
        tags=("anomaly", "drilldown"),
    ),
)

# 合并：原 5 条基线 + E6/01 分析师能力 10 条 + E7 新增 5 条
GOLDEN: tuple[GoldenCase, ...] = _BASE_GOLDEN + ANALYST_GOLDEN
