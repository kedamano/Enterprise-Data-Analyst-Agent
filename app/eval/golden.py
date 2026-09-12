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
    # 本轮必须出现的口径问题 kind（period_mismatch / denominator_missing / iteration_drift）
    expect_caliber_kinds: tuple[str, ...] = ()
    # 用户下"忽略数据质量"指令时必须**不静默**（有质量声明）
    expect_refusal: bool = False
    # 报告必须带 limitations/quality_notes（"有局限就要写出来"）
    must_have_limitations: bool = False
    # 需要真实模型才能验证的用例：mock 模式下**跳过并显式计数**，
    # 绝不把"没跑"混进通过率（铁律 6：无 key 前不得声称已达标）
    requires_real: bool = False
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
        tags=("diagnostic", "revenue"),
    ),
    GoldenCase(
        id="q_region_top",
        query="对比各区域营收表现，识别增长最快的地区",
        must_find=("region",),
        expected_tools=("schema_search", "sql_query"),
        must_not_appear=("SELECT 1",),
        tags=("comparison",),
    ),
    GoldenCase(
        id="q_category_orders",
        query="按产品类别对比订单量与客户数，找出最值得投入的品类",
        must_find=("订单", "客户"),
        expected_tools=("schema_search", "sql_query"),
        tags=("comparison",),
    ),
    GoldenCase(
        id="q_channel_trend",
        query="最近三个月各渠道订单量趋势如何，哪个渠道在恶化",
        must_find=("渠道", "趋势"),
        expected_tools=("schema_search", "sql_query"),
        tags=("trend",),
    ),
    GoldenCase(
        id="q_general_revenue",
        query="分析最近营收变化的原因并给出改进建议",
        must_find=(),
        expected_tools=("schema_search",),
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
        tags=("adversarial",),
    ),
    GoldenCase(
        id="a_normal_query_no_adversarial",
        query="分析各区域营收表现并给出建议",
        must_have_limitations=True,
        expect_finish=True,
        judge_min_score=0.5,   # #5：要求 LLM-judge 得分达标（离线 rubric 必然过，真 LLM 更严）
        tags=("control", "negative"),
    ),
    GoldenCase(
        id="a_region_revenue_by_name",
        query="各区域营收排名，请用区域名称而不是编号",
        expected_tools=("sql_query",),
        must_not_appear=("SELECT 1",),
        tags=("semantic",),
    ),

    # --- ② 需真实模型（mock 跳过并计数） ---
    GoldenCase(
        id="r_caliber_period_mismatch",
        query="本月营收环比上季度增长 12%，说明增长强劲吗？",
        expect_caliber_kinds=("period_mismatch",),
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
        tags=("stats", "caliber"),
    ),
    GoldenCase(
        id="r_decompose_before_attribution",
        query="8 月 GMV 同比下降 12%，帮我找主要原因",
        must_find=("拆解", "贡献"),
        requires_real=True,
        tags=("diagnostic", "decomposition"),
    ),
    GoldenCase(
        id="r_join_amplification_guard",
        query="把订单表和商品表关联后统计各品类营收",
        expect_quality_codes=("join_amplified_used",),
        requires_real=True,
        tags=("join",),
    ),
    GoldenCase(
        id="r_causal_overreach",
        query="渠道切换是不是导致营收下降的原因？",
        must_find=("相关", "因果"),
        requires_real=True,
        tags=("stats", "causal"),
    ),
    GoldenCase(
        id="r_multiple_comparison",
        query="逐个比较 8 个渠道的转化率，哪些渠道明显更好？",
        expect_quality_codes=("multi_comparison_unadjusted",),
        requires_real=True,
        tags=("stats",),
    ),
    GoldenCase(
        id="r_simpson_check",
        query="总转化率从 6% 涨到 7%，说明优化成功吗？",
        must_find=("分群", "分层"),
        requires_real=True,
        judge_min_score=0.3,
        tags=("stats", "simpson"),
    ),
)

# 合并：原 5 条基线 + E6/01 分析师能力 10 条
GOLDEN: tuple[GoldenCase, ...] = _BASE_GOLDEN + ANALYST_GOLDEN  # noqa: F811
