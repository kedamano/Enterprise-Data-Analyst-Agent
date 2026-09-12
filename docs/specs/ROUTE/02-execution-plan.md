# ROUTE/02 Execution Plan — 任务分类 + 执行计划 JSON

> 演进 ROUTE/01：把"输出意图"升级为「任务分类(Task) + 交付物(Deliverable) + 执行计划(Plan)」。
> Router 既可由 LLM 节点给出结构化 JSON，也可由确定性 classifier 兜底（离线/mock 一致）。

## 1. ExecutionPlan JSON（放入 `AgentState.intent`，SSE 透出供 UI 可视化）
```json
{
  "task_type": "business_analysis",
  "deliverable": ["markdown"],
  "requires_data": true,
  "requires_sql": true,
  "requires_python": false,
  "requires_report": true,
  "workflow": ["define_metrics", "query_data", "analyze", "generate_insight", "write_markdown"]
}
```

## 2. Task 分类（单一主任务 + 交付物列表）
TASK 集合：
- `business_analysis`（为什么…/对比/渠道质量）→ mode full（六节点）
- `sql` / `sql_optimization`（只要/优化 SQL）→ mode sql_only
- `markdown_report`（报告/周报/总结/输出文档）→ mode full（报告模板）
- `data_exploration`（看看这份数据/探索）→ mode full（发现导向）
- `python`（写脚本/清洗/处理Excel/留存代码）→ mode python_code（落地后接 E2 python）
- `metric_definition`（指标定义/公式/SQL 实现）→ mode sql_only（产出定义+SQL）
- `data_modeling`（表/模型/数仓设计）→ mode full（输出 DDL/Markdown）
- `visualization`（图表/看板方案）→ mode full
- `data_interpretation`（解释结果/SQL结果）→ 轻问答
- `quick_answer`（可直接回答的小问）→ mode quick_answer

Deliverable 检测优先级：显式措辞 > 任务默认 > full 报告。

## 3. 保守映射（先稳后扩）
- 执行 mode 仍由 ROUTE/01 `detect_mode` 决定（已裁剪 sql_only/quick）。
- `state.intent`（ExecutionPlan）由 `classify_task`（确定性关键字）生成，服务 UI/审计/后续 LLM Router。
- LLM Router 富化（真实模型产出更细 task/workflow）作为后续增强，接在 intent 阶段；mock 一律走确定性。

## 4. 过度执行护栏（不over-execute）
- sql_only：只产 SQL（不跑 python/图表/长报告）。
- python_code（模型给脚本时）：沙箱验证可运行后交付代码，不强制业务报告。
- 无数据/无必要工具时不强行取数（数据不足→给数据需求 + 方案/模板，不编数）。
