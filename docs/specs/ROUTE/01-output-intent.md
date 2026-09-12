# ROUTE 输出意图路由与阶段裁剪 — 规格 v1.0

> 问题：目前任何输入都走 Context→Planner→Executor*→Analyst→Reflection→Reporter 并输出完整报告；
> 分析师常只要 SQL / Markdown 文档 / Python 代码 / 简短问答。需要按「任务类型+输出形态」裁剪阶段链。

## 1. 模式集合（默认 full）
- `full`：完整六节点业务报告（现状，默认）
- `sql_only`：只要一段只读 SQL（生成+可跑验证）→ 输出 ```sql 代码块，不跑 Analyst/Reflection/长报告
- `quick_answer`：能用 SQL 直接回答的小问 → 轻量短答（保留必要取数，跳过 Reflection/Reporter 重链）
- `markdown_doc`：结构化 Markdown 文档/纪要（渲染为文档风格；阶段同 full，终态模板强调章节/目录）
- `python_code`：输出可在沙箱跑通的 Python 分析代码（构建于 E2 自由 python，落地后生效；暂按 full）

## 2. 检测（先确定性，后 LLM 精修）
`detect_mode(user_query, context)`：
- 显式指令优先：含 "只要SQL/输出SQL/给SQL/sql语句"→sql_only；"python代码/写python"→python_code；"markdown文档/纪要"→markdown_doc；"简短回答/一句话/直接回答/是什么/是多少/哪个"→quick_answer。
- context.output_format 作为次信号（sql/markdown/python 命中对应）。
- 未命中 → full。**不误伤**：完整分析请求仍 full。

## 3. 阶段裁剪（graph `_drive_sync`）
- Context 解析后：`state.mode = detect_mode(...)`。
- `sql_only`：Planner 产出（模型给）`tool=freeform,input.sql` → Executor 只读执行 → **终态=SQL 代码块+行数/预览**（跳过 analyst/reflection/reporter）。
- `quick_answer`：取数后用短答渲染（保留 minimal 解读；跳过 reflection + 长报告）。
- `full`/`markdown_doc`：走原链（markdown_doc 复用报告模板但渲染风格为文档）。
- `python_code`：在 E2 python 自由码落地后接代码导出（见 E2 D10+）。

## 4. 可观测 / 契约
- mode 进 state 与 SSE（UI 可显示当前模式）。
- 各模式仍走 tool_results/audit/trace；SQL 可溯源（freeform 已是源）。
- 默认安全：无法识别一律 full（不因误判少输出）。

## 5. TDD
- detect_mode 关键词/上下文/回退矩阵。
- sql_only 端到端（mock planner 给 freeform SQL）→ report 含 ```sql、不含"执行摘要"重链、工具仅自由 SQL。
- quick_answer 端到端 → 短答、不含 Reflection/长报告。
- full 回归不变（guardrails 全绿）。
