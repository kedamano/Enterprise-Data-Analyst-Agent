# E1 数字溯源（Traceability）— 规格 v1.0（D2 定稿）

> 目标：报告/发现里每个"数值型结论"都能解析回一条真实、成功的 SQL step。
> 契约变化点：`Evidence.sql_id`；`sources.resolve_sql_source`（D2 已红绿）。

## 1. 数据契约
- `ToolResult`（已有）：`step_id/tool/status/output`；SQL 结果含 `output.csv_path` + 行样本（物化于 execute_tool）。
- `Evidence`（`state.py`）**新增可选字段**：
  - `sql_id: Optional[str]`（指向 tool_results 里某 step_id）
  - `sql_text_hash: Optional[str]`
  - `row_sample: list[Any] = []`（≤10 行，来自该查询 output.rows）
- `Finding` 不新增字段；数值语义由其 evidence 表达。

## 2. 溯源规则（写入 analyst.md / 校验）
- 凡 finding 引用查询得到的**数值**（金额/数量/占比/排名），其 evidence 必须带 `sql_id`。
- `sql_id` 必须能通过 `sources.resolve_sql_source` 解析到一条 **SUCCESS 且 tool=sql_query** 的 step：
  - 未知 id / 非 SQL step / 失败 step → 解析 None → 该 finding 视为**无有效溯源**，在 eval 里判 FAIL。
- 非数值结论（解读/建议/假设）不强求 sql_id。
- **Mock 与 real 一致**：mock 产出的数值 finding 同样必须链到真实 sql step（禁止 mock 编数）。

## 3. 报告与 API
- 报告渲染：数值 claim 后跟 `[src: <step_id>]`（可点开/可解析）。
- 新增 `GET /api/v1/chat/analyze/trace/{session}` → `{objective, claims:[{finding, evidence:[{sql_id, rows_sample}]}], steps:[{step_id, tool, sql?}]}`（服务端由 state/tool_results + trace 数据拼装）。
- SSE FINISH 事件附 `trace_summary`（溯源覆盖率）。

## 4. 可观测指标
- `traceability_coverage = 有有效 sql_id 的数值 claim / 数值 claim 总数`（进入基线指标）。

## 5. 验收 / DoD（D7 门禁）
- 示例报告每条数值可点开到底层 SQL（mock 端到端）。
- 无证据/无源数值 → eval 断言 FAIL（新增 must_trace 维度）。
- 回归全绿 + eval mock 基线记录。

## 6. 边界
- Python 步骤（读 csv 分析）暂不计为溯源源（v1 仅 sql_query；Python 溯源进 E2 时扩展）。
- 单会话内解析（不跨会话）。
