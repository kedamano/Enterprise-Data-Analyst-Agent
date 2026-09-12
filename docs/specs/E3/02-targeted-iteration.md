# E3/02 增量意图分类与目标阶段执行 — 规格 v1.0（D15）

> 承接 `E3/01-iteration.md`。01 解决「命中指代 → 跳过发现链，作用于上一数据集」；
> 02 解决「命中之后**只执行用户真正要的那一步**」，并给用户/调用方一个**显式全链开关**。

## 1. 增量意图分类（确定性、可测）
`classify_followup(query) -> kind`，仅在 `is_followup(query) == True` 时使用：

| kind | 触发（示例词） | 语义 |
|---|---|---|
| `date_change` | 改为…月/季/年、改期、换时间、时间范围、近 N 天、本期、上季度 | 在同一数据集上改时间切片 |
| `granularity` | 粒度、按天/周/月/季度/年、换成按…、汇总到、聚合到 | 换聚合粒度 |
| `drilldown` | 下钻、钻取、细分、拆到、展开到 | 沿维度下钻（筛选+分组） |
| `filter` | 只看、只保留、筛选、过滤、排除、剔除、限定 | 只做行筛选 |
| `generic` | 其余命中 followup 的请求 | 未知增量（交模型判定） |

优先级：`date_change > granularity > drilldown > filter > generic`（同时命中取更具体的）。

## 2. 只执行目标阶段
- 分类结果 → `iteration_plan(kind) = {kind, stages[], skips[]}`：
  - `filter` → `["filter"]`；`drilldown` → `["filter","groupby"]`；
    `date_change` → `["time_slice"]`；`granularity` → `["regrain","aggregate"]`；`generic` → `["custom"]`。
  - `skips` 恒含 `planner / schema_search / sql_query`（发现链不跑）。
- `deliver_iteration` 把 stages 作为**明确指令**写进增量脚本 prompt（"只做 X，不要重新取数/不要重新聚合"），
  并落 `state.iteration = {kind, stages, skips, attempts, guard}`，供 UI/审计。
- 仍只跑 **一次** python_analysis（沙箱）——目标阶段即该脚本；不因分类而新增步骤。

## 3. 安全守卫（不满足 → 回退全链，绝不静默少做）
在生成脚本**之前**确定性判定，命中即回退（`state.metadata["iteration_skip"]` 记原因）：
- 无 `last_dataset` / 无 `csv` → 回退（01 已定）。
- `date_change`：数据集无日期列，或请求期间**越出数据集日期范围**（年/年月粒度解析）→ 回退。
- `granularity`：目标为时间粒度（按天/周/月/季/年）但数据集无日期列 → 回退。
- `filter`/`drilldown`：作用于现有列，无需守卫（脚本失败由沙箱如实报错）。
- `force_full_rerun=True` → 直接回退（最高优先级，见 §4）。

## 4. force_full_rerun 开关（显式）
- 契约：`run_analysis/stream_analysis(..., force_full_rerun=False)`；`AgentState.force_full_rerun`；
  `POST /chat/analyze` 与 `/chat/analyze/stream` 的 `AnalyzeRequest.force_full_rerun: bool = False`。
- 语义：**为真即跳过增量**（即使命中指代且有数据集），走 Context→Planner→… 全链。
- 与 01 的 FORCE 文本词（"重新完整分析"等）叠加：任一成立即全链。

## 5. 可观测
- 增量轮：`state.mode == "iteration"`，`state.iteration.kind/stages/skips`；报告含"增量分析（基于上一结果）"。
- 回退轮：`state.metadata["iteration_skip"]`（原因），`mode` 不为 iteration。

## 6. TDD（正 + 负）
- `classify_followup` 矩阵（四类 + generic + 优先级）。
- e2e 下钻：第二轮 `mode=iteration`、`iteration.kind=="drilldown"`、无 `schema_search`、有 `python_analysis`。
- 负：改期越界（数据集 2024 全年，请求 2025-01）→ 回退全链（planner 被调用）且记 skip 原因。
- 负：`force_full_rerun=True` + 明确指代 → 仍走全链（planner 被调用）。
- API：`AnalyzeRequest(force_full_rerun=True)` 字段可解析并透传到 state。
