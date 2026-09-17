# E6/01 真实评测与计分口径 — 规格 v1.0（2026-09-12 回溯定稿）

> **回溯说明**：E6 的实现（15 条 golden / `requires_real` 门控 / LLM-judge / 轻路径）
> 在 D27–D30 与 #5 补丁中已陆续落地，但 `docs/specs/E6/` 一直是**空目录**——
> 是本项目唯一缺规格的 Epic。本文件按"代码事实 + 本轮踩坑"回溯补齐契约，
> 之后对本 Epic 的改动以本规格为准。

## 1. 目标与动因

`app/eval` 起初只有 5 条 golden 与**结构性断言**（子串命中、工具是否被调用）。
两类问题它答不了：

1. **"答案对不对"**：字段都在、但结论是错的（算错、口径混、因果越界）→ 结构断言照样绿。
2. **"这批数字算不算数"**：真实模型被限流/欠费后**静默降级为 mock**，
   评测器照单全收 → 产出一份"看起来真实"的假基线。**这是本 Epic 最重要的一条。**

## 2. 契约

### 2.1 `GoldenCase`（`app/eval/golden.py`）

| 字段 | 语义 | 生效模式 |
|---|---|---|
| `must_find` | 报告/发现里必须出现的片段（大小写不敏感） | real（mock 下不保证业务内容） |
| `expected_tools` | 本次**必须实际调用**的工具（读 `state.tool_results`，非埋点） | mock + real |
| `must_not_appear` | 绝不允出现的片段（如退化 SQL `SELECT 1`） | mock + real |
| `expect_finish` | 是否要求终态 FINISH | 两者 |
| `expect_quality_codes` | 必须出现的质量门禁 code | 两者 |
| **`must_not_have_quality_codes`** | **绝不允出现的门禁 code（负向断言）** | 两者 |
| `expect_caliber_kinds` | 必须出现的口径问题 kind | 两者 |
| `expect_refusal` | 用户下"忽略数据质量"指令时必须**不静默** | 两者 |
| `must_have_limitations` | 报告必须带 limitations / quality_notes | 两者 |
| **`requires_real`** | 需真实模型才能验证 → mock 下**跳过并单独计数** | — |
| `judge_min_score` | 要求 LLM-judge 分 ≥ 阈值；0 = 不要求 | 两者 |

**`must_not_have_quality_codes` 的存在理由（本轮新增）**：
有些 code 只在 Agent **写错**时才由门禁产生，例如 `join_amplified_used` 要求
"结果行数 ≥1.5×最大输入表 **且** 该结果被引用进结论"。
用 `expect_quality_codes` 正向索取这类 code，会让**正确实现永远无法通过**——
即一条"惩罚正确行为"的测试。表达"正确行为不得报警"必须用负向字段。

### 2.2 `requires_real` 门控（铁律 6）

- mock 模式下，`requires_real=True` 的用例记为 `SKIPPED`，**不进通过率**，
  指标里单独计 `skipped_requires_real`；
- **只有 `requires_real` 才允许被跳过**——套件里必须钉住
  `skipped ⊆ requires_real`，否则"跳过"会变成刷通过率的后门。

### 2.3 计分口径：`DEGRADED` 与 `scored_cases`

`real` 模式下若本轮发生过 LLM 降级（`state.metadata["degraded"]`），则该用例记为
**`DEGRADED`**，与 `SKIPPED` 同级，**从所有计分口径中剔除**：

| 指标 | 含义 |
|---|---|
| `scored_cases` | 实际计分的用例数（排除 SKIPPED / DEGRADED） |
| `degraded_excluded` | 因降级被剔除的用例数 |
| （其余 `*_rate` / `avg_*`） | 一律只按 `scored` 计算 |

markdown 报告在存在降级时**必须**输出 ⚠️ 告警块（点名用例 + 降级阶段），
使"这批数字不可用"成为报告的结论，而不是读者自己去翻日志。

> 动因（真实踩坑）：429 限流 → router 静默降级 → 旧 runner **零引用**
> `degraded`/`fallback` → 把 mock 输出当真实成绩计分。
> 当时那份基线的可信度**是靠人读日志**撑着的，换个时间点跑就会得到假基线。

### 2.4 LLM-judge（`app/eval/judge.py`）

- `judge_case()` 统一入口，返回 `JudgeResult{score, method, rationale}`；
- `JUDGE_USE_LLM=1` 且配了 key → 走 LLM 语义评分（`method="llm"`）；
- 否则 / 任何异常 → 确定性 rubric 兜底（`method="rubric-offline"`），**永不抛、永不假绿**；
- rubric 是**结构质量代理**，不是语义正确性——报告里必须能看出用的哪种方法
  （指标 `judge_method`）。

### 2.5 成本折算语义（#6 修复后）

| 配置 | 含义 | 报告 `cost_estimate_usd` |
|---|---|---|
| `COST_*_PER_MTOK` **未设置**（`None`） | 单价未知 | `None`（"不知道多少钱"） |
| `COST_*_PER_MTOK=0` | **已知免费**（`-free` 档模型） | `0.0`（"确实不花钱"） |

**这两件事必须可区分**。此前用 `0.0` 兼表"未知"且判据是 `if pi > 0 or po > 0`：
免费模型的真实成本 0 与"没配单价"输出长得一样，成本列**恒为 None**。

## 3. CLI

```bash
# 离线（mock，无 key）——CI 口径
python -m app.eval.runner --mode mock

# 真实：只跑 requires_real 的用例（省时间/额度）
DATA_DB_URL=sqlite:///./data/sample_analyst.db DATA_DB_DIALECT=sqlite \
  python -m app.eval.runner --mode real --only-real \
  --out docs/progress/eval-real-analyst-dataset.md

# 单用例冒烟
python -m app.eval.runner --mode real --only-real --ids r_join_amplification_guard
```

## 4. 数据集契约

`requires_real` 的用例命题必须能**落到数据上**，否则测的是"数据缺口"而不是模型能力：

- `data/sample_enterprise.db`：基础星型模型（`fact_sales` + 3 张维表）；
- `data/sample_analyst.db`：评测专用**超集**（`scripts/generate_analyst_sample.py`，
  固定种子 `2026`），补齐转化率 / GMV / 跨年 / ≥8 渠道 / 品类 / 分层，
  并**刻意植入陷阱**（辛普森悖论、GMV 同比内部分化）。
  `tests/test_analyst_sample_dataset.py` 钉住"陷阱真的落在库里"。

## 5. 边界（如实记录）

- `must_find` 是子串命中，受措辞影响；因此**新增 `must_find` 需谨慎**，
  不能用它去表达"方向性"要求（见 §2.1 的 `must_not_have_quality_codes`）。
- 离线 rubric 只能代理"结构质量"，**不得**用它的分数声称"答案正确"。
- 降级剔除是**本轮**粒度；跨轮对比时须同时看 `scored_cases` 是否变化。
- 评测器读的是 `state`（事实来源），**不读埋点**——`tool_calls` 曾因并行执行
  合并 span 从 4.8 掉到 1.0，后改为从 `state.tool_results` 统计。

---

## 6. v1.1 增量（2026-09-13 · D39）：badcase 回流 + 两条"补 vacuous 漏洞"的断言

> 动因：D38 的真实基线 7 条里 6 条失败，但**跑完就散了**——没落盘、没法复跑、
> 没法变成回归。下一轮改动是"修好了"还是"又坏了"只能人肉比对两份 markdown。
> 这正是 `docs/progress/` 里堆了一排 `eval-*-INVALID/CONTAMINATED/INCOMPLETE` 的原因。

### 6.1 `app/eval/badcase.py` —— 把"失败"变成可复跑的资产

| 能力 | 说明 |
|---|---|
| `record_from_report(report, out_dir)` | 从报告挑出**该记的**失败用例落盘 |
| **同一 case 只留一条** | 重复失败 `runs` 累加（否则每跑一次堆一堆文件） |
| `replay(dir, only=...)` | 拿落盘用例原样复跑 → `{"fixed": [...], "still_bad": [...]}` |
| `promote_draft(case)` | 生成**待人工审阅**的 golden 片段，**不写文件** |

**不记什么**：`SKIPPED`（`requires_real` 在 mock 下没跑——**没跑不等于失败**，铁律 6）
与 `DEGRADED`（限流/余额导致的降级**不是模型的错**，记进来会污染复跑结论）。

**为什么 `promote_draft` 不自动写入**：断言该立什么需要人判断——
失败原因里既有"措辞没命中"（脆弱、该改断言）也有"确实没做到"（该保留作回归）。
**让模型给自己出题等于把门槛交给被考的人。**

CLI：`python -m app.eval.badcase --list/--replay/--promote <id>`；
runner 侧 `--badcases <dir>` 在评测结束时自动落盘。

### 6.2 两条新断言：都为了补"恒真"漏洞

**① `min_findings`**（§2.1 已述）：`must_find` 会被**回显的问题**满足——
报告天然有标题/目标段，于是"问题里出现过的词"让断言恒真。
D38 基线里 `r_join_amplification_guard` 正是这么被误判成 ✅ 的（正文写着"状态：无法完成"）。

**② `min_numeric_claims`（本轮新增，从真实 badcase 固化）**：
E1 现有断言是"**每个**数值 claim 都要能溯源"——当数值 claim **一个都没有**时
它**恒真**（vacuous truth）。D38 真实基线 `溯源 0/0` 却全程判过，
于是"分析停留在口径/表结构的元讨论、没给出任何数据结论"这件事**测不出来**。
数据类用例设 ≥1，把"零数值结论"从"通过"改成"失败"。

> 两条都属同一族：**断言的"通过条件"太弱，弱到可以被空内容满足**。
> 与 §2.1 的 `must_not_have_quality_codes`（惩罚正确行为）是一体两面——
> 都是"断言测的不是它想测的东西"。

### 6.3 与 `accept_clarify` 的关系（刻意设计，不是冲突）

`min_findings` / `min_numeric_claims` 在 `CLARIFY_OK` 早退**之后**才检查，所以：

> **反问可以没有发现；一旦选择作答，就必须真有发现、真有数值结论。**

`r_causal_overreach` 同时带 `accept_clarify=True` 与 `min_findings=1` 正是此意。
