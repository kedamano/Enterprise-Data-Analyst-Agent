# E3 中间结果迭代（Session Dataset Iteration）— 规格 v1.0（D14 定稿）

> 痛点：分析师一轮分析后常接着改口径/下钻/换粒度；现状每轮都 Context→Plan→全链重跑。
> 目标：同会话内对「上一结果」的增量请求，**跳过发现/规划重链**，直接作用于上一数据集。

## 1. 会话数据集（last_dataset）
- 任一次成功的数据步（sql_query/freeform，或 python 产物 CSV）后，落 short_term：
  `{csv, columns[], rows(≤20 样本), sql, step_id, created}`（非敏感；仅样本行）。
- 也可由 `AgentState.last_dataset` 透出（单轮内存）。

## 2. 指代识别（确定性、保守）
- FOLLOWUP 触发词（显式）：基于上一结果、上一个结果、这个结果、在此基础上、改为、改成、只看、换成、再按、在这份数据上、同样数据、接着、继续按…
- FORCE 词优先否定：重新完整分析、全量重跑、重新查、从头分析 → 走全链。
- 同时必须存在 last_dataset，否则不触发（回退全链）。

## 3. 增量执行
- 命中：`mode=iteration`；**不跑** Planner/Executor 的 schema/sql 发现链；直接以 `last_dataset.csv`
  为数据源，让模型写 Python 增量脚本 → 沙箱执行 → 输出「增量结果 + 代码 + 沙箱验证」。
- 会话记忆仍写回（history/lessons 沿用）。
- 失败/无数据集 → 回退全链（绝不静默少做）。

## 4. TDD
- `is_followup` 矩阵（显式命中 / FORCE 否定 / 普通问题不命中）。
- save/load last_dataset 持久化（含 columns 与 csv）。
- e2e：同 session 第一轮产 CSV → 第二轮「基于上一结果，改为只看 region 1」→ mode=iteration、
  **无新的 schema_search/sql 发现步**、报告为增量结果；「重新完整分析」→ 回全链（planner 被调用）。
