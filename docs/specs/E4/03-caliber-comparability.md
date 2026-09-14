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
- `unit_mismatch`（同一报告里"万元/亿元"混用）**已实现确定性规则**（v1.2，见 §7）——
  原标 `[待真实验证]`，本轮把"可结构化判定"的那一半落地，仍留语义判读的部分。
- `filter_mismatch`（含/不含退款等限定词）**已实现"极性冲突"这一确定性子集**（v1.2，见 §7）；
  更广的"单侧过滤"判读仍留给 LLM 维度，标 `[待真实验证]`。

## 7. v1.2 增量（2026-09-12）：`unit_mismatch` / `filter_mismatch` 确定性落地

> 动因：这两个枚举值此前只有名字没有判定，`pending-real.md` 与 §8.3 一直挂着"未实现"。
> 本轮把**可以结构化判定**的那部分做成确定性规则；判不动的部分**明确不判**（宁缺勿滥）。

### 7.1 `unit_mismatch` — 同一指标混用不同数量级单位

**判据（双条件）**
1. 文本中解析出**同一指标名**下的 ≥2 个金额；
2. 这些金额使用了**不同数量级单位**（亿元/万元/千元/百万元/万/亿）。

指标名取**金额紧邻前方的 2–8 个中文/字母**（`营收 1.2 亿元` → 指标 `营收`）。
两个金额必须**指向同一指标名**才算——`营收 1.2 亿元` 与 `成本 3000 万元` **不判**，
因为不同指标用不同量级单位是**正常写法**，不是口径错误。

**为什么不做"全文出现两种单位就报"**：那会把绝大多数正常报告点亮（金额量级天然不同），
制造报警疲劳——与 `gate.py` 的"双条件"原则一致。

**仍不判**（留给 LLM）：同一指标在**不同段落**被写成不同单位但指标名未复现（无法可靠归属）。

### 7.2 `filter_mismatch` — 限定词**极性冲突**

**判据**：同一限定对象上出现**相反极性**的限定词。

| 极性 | 限定词 |
|---|---|
| 包含 | 含 / 包含 / 包括 / 仅含 / 只含 |
| 排除 | 不含 / 不包括 / 剔除 / 排除 / 扣除 / 去除 |

限定对象：退款 / 退货 / 税 / 运费 / 赠品 / 内部（订单）/ 测试（单）/ 异常 / 停用。
例：`含退款口径的营收` 与 `不含退款的营收` 同时出现 → `filter_mismatch`。

**仍不判**（明确的边界，留给 LLM 维度）：**单侧过滤**——
即只有一处限定词、另一侧未声明（如"不含退款后营收环比增 3%"却没说基期口径）。
识别它需要判断"哪两个数属于同一对比两侧"，结构性判据不足，硬做必然误报。

### 7.3 决策影响
两条新规则**均为披露级**（不抬 REPLAN）——与 `period_mismatch` / `denominator_missing` 同级：
口径提示应让人看见，但"单位写混了"不一定推翻结论。`iteration_drift` 仍是唯一抬 REPLAN 的。
