# E4/03 口径可比性检查（Reflection）— 规格 v1.0（D19 定稿，D22 实现）

> 痛点：报告里最常见、也最难自查的错误是**口径不可比**——
> 「Q1 营收 1.2 亿」和「Q2 营收 1.5 亿」用的其实是两种统计口径（含/不含退货、含/不含税、不同区域范围），
> 结论"环比增长 25%"因此是假的。Reflection 现在只查证据/逻辑/完整性，**不查口径**。

## 1. 检查项（新增到 Reflection 维度）
`caliber_comparability`：同一次分析内出现的**同一指标多次取值**，其口径是否可比。

判定来源（确定性优先）：
- **引号/条件不一致**：报告里同一指标名配了不同的限定词（"含退货/不含退货"、"华东/全部地区"、"订单口径/营收口径"）。
- **期间不一致**：同一对比组里的期间长度不等（如"1 月"vs"Q1"、"近 30 天"vs"上月"）→ 必须显式标注或拒绝。
- **与上一轮口径漂移**（与 E3 联动）：迭代轮改了时间切片/粒度/度量后，报告若把新旧口径直接并列比较 → 判不可比。
- **分母未声明**：比率类指标（转化率、占比）未给出分母口径。

## 2. 输出契约（`ReflectionResult` 新增）
```jsonc
{
  "caliber_comparability": {
    "comparable": true,
    "issues": [ {"kind": "period_mismatch|filter_mismatch|denominator_missing|iteration_drift",
                 "detail": "1 月与 Q1 期间长度不同", "metric": "营收"} ],
    "checked_metrics": ["营收", "订单量"]
  }
}
```
- `comparable == false` 且存在 `issues` → Reflection 决策 **REPLAN**（要求补口径）或（无法补齐时）**PASS 但报告强制标注**；
  判定策略：`issues` 含 `denominator_missing`/`period_mismatch` → 至少**标注**；
  含 `iteration_drift`（新旧口径并列） → **REPLAN**，因为这不是措辞问题而是结论错误。
- 报告渲染：`## Limitations` 增「口径说明」小节，列出 `issues[].detail`。

## 3. 与 E3 的接口
迭代轮产出 `state.iteration = {kind, stages, ...}`（E3/02）；当 `kind in (date_change, granularity)`
时，Reflection 必须把 `iteration_drift` 检查置为**必需**（口径已变），且报告的"数字来源"需标明基于哪个基线（`derived_from`）。

## 4. 边界
- 只有一个指标的孤立陈述（无对比）→ `comparable=true`，不做强制。
- 用户**显式要求**跨口径对比（"用两种口径各算一遍给我看"）→ 不算 issue，但报告需逐条标口径。
- 依赖真实模型的语义判断部分（如"含退货"这类限定词识别）标 `[待真实验证]`；
  **结构性判定**（期间长度、粒度漂移、分母缺失）必须是确定性规则。

## 5. TDD（D22）
- 正：同口径的两期对比 → `comparable=true`。
- 负：期间长度不等的对比样例 → `comparable=false`，`issues[].kind == "period_mismatch"`，报告含"口径说明"。
- 负：E3 迭代改期后直接与旧口径并列 → `kind == "iteration_drift"` 且 Reflection **REPLAN**。
- 正：用户显式要求跨口径 → 不算 issue，报告逐条标注。


---

## 6. 实现说明（v1.1 · S2 落地）

- 模块：`app/core/agents/data_analyst/caliber.py`（纯函数，与 `gate.py` 同范式）。
- 状态：`CaliberIssue{kind, detail, metric}` + `CaliberCheck{comparable, checked_metrics, issues}`
  挂在 `ReflectionResult.caliber_comparability`。**刻意不复用 `ReflectionDimension`**——
  后者的 `_coerce` 会把 issues 列表压成字符串，结构化断言（eval 要用）就失效了。
- 期间解析：`parse_period_days()` 覆盖 `近N天/周/月/季/年`、`2024年3月`、`本季度/上月/去年同期`；
  **解析不出返回 None 且不判定**（宁缺勿滥）。模式表**顺序即优先级**——实现时踩过：
  `2024年3月` 被泛化的 `月` 先命中判成 30 天（应为 31），已把具体写法前置。
- 差异容差 20%（`_PERIOD_TOLERANCE`）：期间长度差 ≤20% 视为可比。
- 决策：`iteration_drift` → REPLAN（跨口径比较是**结论错误**）；其余只披露，不改决策。
- 披露：报告新增 `## 口径说明` 段（有问题才出现），由 `report_tool` 从
  `reflection.caliber_comparability.issues` 渲染。
- **迭代轮耦合已解决**：`deliver_iteration` 绕过 Reflection→Reporter，
  故在增量终态处直接调 `caliber_check(..., iteration=state.iteration)`，
  命中 `iteration_drift` 时把 `> ⚠ **口径提示**：…` 追加进增量报告。
- 提示词（**只追加**，`tests/test_prompt_negative.py` 钉死了 planner.md 既有句子）：
  - `system.md` 新增 `# Analytical Methodology: Decomposition, Denominator, Comparability`
    （量价拆解 / Mix 结构效应 / 贡献度 / 分层分群 / 分母纪律 / 可比性四项对齐 / 归因纪律）
  - `analyst.md` 新增 `# Decomposition & Caliber Requirements`（变化类结论必须带拆解或说明为何不能拆；
    比率必须声明分子分母；对比必须声明基线期间/过滤/定义/粒度）
  - `reflection.md` 新增第 7 维 `caliber_comparability` 的 JSON 契约与 REPLAN 规则
- `unit_mismatch`（同一报告里"万元/亿元"混用）**枚举值保留但未实现**——语义判读标 `[待真实验证]`。
- `filter_mismatch` 同理：结构性判据不足，留给 LLM 维度，标 `[待真实验证]`。
