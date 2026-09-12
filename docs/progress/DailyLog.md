# 逐日开发日志（企业化 · SDD×TDD）

约定：每天收工追加一节；`[待真实验证]` 项不视为达标。

---
## Day 1（2026-09-09 · P0 基线对齐 + 工具链）

**任务**
1. 离线全量回归基线 → **169 passed / 148s**（Round 末全绿，docker_live 含）。
2. eval mock 基线：FINISH 1.0 · 断言 1.0 · 工具调用 4.8 · 工具成功率 1.0 · LLM 调用 5.0 · 成本 USD=None（未配单价）。
3. **发现并修复真 bug**：独立进程（eval/`python -m`/真实服务）读 `.env` 后 Redis 不可达会让 `short_term` 直接崩 → 违反"记忆故障不打断"铁律。修复：`short_term` put/get/get_all 加 Redis 故障→内存降级兜底；新增负路径测试 `test_redis_down_falls_back_to_memory`（记忆套件 11 passed）。

**收工状态**：回归 169 passed；eval mock 可复跑；DailyLog 模板启用。
**明日（Day 2）待办**：E1 规格定稿（`docs/specs/E1/`）→ 红测（evidence 带 sql_id / 报告 claim 可溯源 / 无证据 fail）。

## Day 2（2026-09-09 · P0 工具链校准 + E1 规格定稿）

**任务**
1. 红→绿校准：`test_e1_sources.py`（4 用例）先红（模块缺失）→ 实现 `app/core/agents/data_analyst/sources.resolve_sql_source` → 绿 4 passed。验证每日循环可用。
2. E1 规格定稿 **v1.0**（`docs/specs/E1/01-traceability.md`）：Evidence 增 sql_id/sql_text_hash/row_sample；溯源规则（数值必带 sql_id 且须解析到 SUCCESS sql_query step，否则 FAIL）；报告 `[src:step_id]`；`/trace/{session}` API；traceability_coverage 指标；Mock/real 一致；边界（Python 溯源留 E2）。
3. 建立 `docs/progress/pending-real.md` 待真实验证清单（E1/E2/E5/E6/E7 各一项）。

**收工状态**：E1 地基（解析器）已绿；规格定稿；明日（D3）进入 E1 主体：schema 加字段 → analyst 自动补 sql_id 红测。

## Day 3（2026-09-09 · E1 主体：schema + 溯源校验）

**任务（SDD→TDD→绿）**
1. **SDD/契约实现**：`state.Evidence` 增 `sql_id/sql_text_hash/row_sample`（可选，默认空）；`analyst.md` 追加 Traceability(E1) 约束（数值必带 sql_id、禁编无源数字）。
2. **TDD 红→绿**：`test_e1_trace_validation.py`（4）——数值无 sql_id 判 FAIL、有效 sql_id 通过、指向失败/未知步骤判 FAIL、非数值解读不强求。
   实现 `sources.unresolved_numeric_claims`（数值判定含数字字符串）+ 复用 `resolve_sql_source`。
3. 回归：guardrails(full_pipeline/reflection/report/sse) + report_tool + E1 两件套 **8 passed**，schema 变更无破坏。

**收工状态**：E1 校验能力就绪（可判定"哪些数值缺溯源"）。
**明日（D4）待办**：analyst 后处理**自动补 sql_id**（从最近成功 sql step 推导 evidence），跑记忆/编排回归。

## Day 4（2026-09-09 · E1：analyst 自动补 sql_id）

**任务（TDD→实现→集成）**
1. 红：`test_e1_auto_trace.py`（数值无源补最近成功 SQL / 已有不动 / 非数值不强求 / 无 SQL 保持 None）。
2. 实现 `sources.last_successful_sql_id` + `sources.auto_trace`；接线 `run_analyst`（AnalysisResult 解析成功后对 findings 数值 evidence 自动补 sql_id，异常不打断流水线）。
3. **编排级集成**：mock 全流水线 `run_analysis` 后，数值型 evidence 均带可解析 sql_id（5 passed）。
4. 回归：记忆集成 + guardrails(full_pipeline/sse/reflection) + replan/branch —— 通过。

**收工状态**：E1 自动溯源闭环成立（analyst 产出 → 自动标注来源步骤）。新增用例 9（auto 5+sources/validation 复跑）。
**明日（D5）待办**：报告渲染 `[src:step_id]` + `/api/v1/chat/analyze/trace/{session}` 端点 + SSE FINISH 附 trace_summary（红→绿）。

## Day 5（2026-09-09 · E1：报告/端点/SSE 溯源呈现）

**任务（TDD→实现）**
1. sources 增 `sql_of_step / trace_manifest / append_citations`（claim→SQL→样本≤10、coverage）。
2. run_reporter 报告末尾追加「## 数字来源」块（每条数值 evidence → [src: step_id]）。
3. chat.py：新增 `GET /api/v1/chat/analyze/trace/{session}`（读 checkpoint → trace_manifest，无则 404）；SSE FINISH 事件附 `trace_summary`(coverage)。
4. TDD：`test_e1_trace_api.py`（5）红→绿：citations 追加 / 无数值不变 / manifest 含 sql+样本 / 端点 200+404 / SSE FINISH 带 trace_summary。
5. 回归：E1 全套 + report_tool + guardrails(full/report/sse/reflection) 通过。

**收工状态**：E1 呈现闭环（报告可点溯源 + API 清单 + 覆盖率随 SSE 下发）。
**明日（D6）待办**：eval 增「溯源」断言维度（must_trace/禁止无源数值）+ 覆盖率入基线表；回归全绿。

## Day 6（2026-09-09 · E1：eval 溯源维度 + 覆盖率基线）

**任务（TDD→实现）**
1. `sources.trace_counts(findings, results)`：数值 evidence 总数 / 有有效 sql_id 数。
2. runner：CaseOutcome 增 numeric_claims/traced_claims；断言并入 `unresolved_numeric_claims`（有未溯源数值→FAIL，最多报 5 条）；metrics 增 numeric_claims_total/traced_claims_total/traceability_rate；报告行增「溯源覆盖率/溯源 claims」。
3. TDD：`test_eval_reports_traceability_dimension` 红→绿（metrics 含三键、0≤rate≤1、每例无 sql_id 类断言失败）。
4. 验证：eval harness 7 passed；CLI mock 输出溯源覆盖率行。

**收工状态**：E1 五件套全绿——schema+自动溯源+校验+呈现+评测。
**明日（D7）待办**：E1 门禁：示例报告每条数值可点开 SQL（手动演示留 UI/README 记录）+ 无证据 fail 覆盖；回归 + DailyLog 归档 → 标记 #38 completed。

## Day 7（2026-09-09 · E1 门禁收口）

**任务**
1. E1 门禁确认：eval mock 溯源覆盖率 **1.0（9/9 数值 claims 全部可解析到真实 SQL step）**；断言通过率 1.0（无证据/无源数值会判 FAIL 的负向逻辑已由 unresolved_numeric_claims 覆盖并有专门用例）。
2. 溯源能力写进 `docs/progress/metrics.md`（E1 基线：回归 169、trace 1.0、接口 200/404、新增 18 用例）。
3. DailyLog 归档 → **#38 E1 标记 completed**（解锁 E2）。

**收工状态**：E1 完整闭环——analyst 产出数值 → 自动标注来源 SQL → 报告 `[src:step_id]` 可点开 → `/trace/{session}` 清单 → eval 门禁兜底。
**明日（D8）待办**：E2 规格定稿（自由 sql_text/python_code + 守卫 + confirm）→ 红测（自由 join SQL 执行 / 注入 DML 拒绝）。

## Day 8（2026-09-09 · E2 开工：规格 + 自由 SQL 地基）

**任务（SDD→TDD→绿）**
1. **SDD E2 定稿**：`docs/specs/E2/01-freeform-code.md`——Analyst 可产 `sql_text`/`python_code` + `confirm`；守卫复用既有 SQL/Python 安全层；`config.freeform_enabled=False`（默认关，测试环境开）；失败回注 + 溯源复用 E1。
2. **TDD 红→绿**：`test_e2_free_sql.py`（4）——自由 join+窗口 SQL 只读执行返回行 / DELETE 拒绝（只读）/ 多语句拒绝 / 空 SQL 失败。实现 `app/core/tools/freeform.execute_free_sql`（委托 sql_tool 内置守卫）。

**收工状态**：自由 SQL 通道就绪（model 给的任意只读 SQL 可安全执行）。
**明日（D9）待办**：executor 分支接线（Analyst 自定义步骤 → execute_free_sql 落 ToolResult + 溯源 step_id）+ 负路径回归。

## Day 9（2026-09-09 · E2：executor 自由 SQL 接线）

**任务（SDD→实现→绿）**
1. **机制**：`PlanStep` 增 `input`（模型可在计划步骤直接给 SQL）；新工具 `freeform`（注册 REGISTRY + ToolSpec：READ_DATA/只读/审计 ALWAYS）；`build_executor_params` 对 `tool="freeform"` 透传 `input.sql`；`sources` 将 `freeform` 计入 SQL 源（溯源/E1 兼容）。
2. **TDD**：`test_e2_freeform_exec.py`（5）：已注册 / 参数透传模型 SQL / 自由 join 执行返回行且可作溯源源 / DELETE 拒绝(只读) / 空 SQL 守卫失败。连同 `test_e2_free_sql.py` **9 passed**。
3. 回归：工具权限 + E1 + guardrails(full/sse/unknown/sql) **20 passed**（新增工具未破坏既有契约）。

**收工状态**：模型产出的只读 SQL 已能作为计划步骤安全执行，且天然接入溯源/审计/限流。
**明日（D10）待办**：自由 `python_code` 分支（模型出脚本 → AST 守卫 + 沙箱执行 → 产物回传）；危险模块/逃逸负路径。

## Day 9b·ROUTE（插叙：输出形态路由 + 阶段裁剪）

**背景**：用户指出"任何输入都输出完整报告，分析师其实常只要 SQL/Markdown/Python/短答"。经确认首批四类（SQL 片段/Markdown 文档/Python 代码/简短问答），机制=模式路由裁剪阶段链。

**实现**
1. SDD：`docs/specs/ROUTE/01-output-intent.md`。
2. `AgentState.mode`(default full)；`modes.py`：`detect_mode`（显式指令→output_format→回退 full）+ 轻终态 `terminal_sql_only/terminal_quick`。
3. graph：context 后算 mode；`sql_only/quick_answer` 在 Executor 后**跳过 Analyst/Reflection/Reporter**，直接轻终态（异常回退 full）。
4. Planner 注入 mode + prompt 说明（sql_only→产出 freeform input.sql；quick→最小 SQL）。
5. TDD：`test_route_modes.py`（5）绿；回归 guardrails full + eval harness 7（默认 full 与 E1 溯源 1.0 不变）。

**状态**：sql_only / quick_answer 已端到端生效；markdown_doc 与 python_code 检测已接入，暂按 full 路由（文档渲染/代码导出随 E2 python 与模板推进）。

**待办（并入计划）**：markdown_doc 专用渲染；python_code 代码导出（等 E2 自由 python D10+）；SSE/UI 展示 mode。

## Day 9c·ROUTE/02（ExecutionPlan：任务分类 + 结构化计划）

**背景**：用户提供完整 Orchestrator 规范（Task 分类 + Deliverable + 不 over-execute/over-output + Router 输出 JSON ExecutionPlan，前端把 Plan 可视化）。

**实现**
1. SDD：`docs/specs/ROUTE/02-execution-plan.md`。
2. `modes.py` 增：11 类 TASK 分类 + `_WORKFLOWS/_REQUIRES/_DELIVERABLE` 模板 + `classify_task`（空格归一关键字，显式>任务默认）+ `build_plan` → ExecutionPlan JSON（task_type/deliverable[]/requires_*/workflow[]）。
3. `AgentState.intent`；graph 在 mode 判定同时生成 intent；SSE INIT/UNDERSTAND 事件透出 intent+mode（UI 可视化计划）。
4. 保守映射：执行仍由 detect_mode 裁剪（sql/quick 已生效），intent 服务 UI/审计；LLM Router 富化留后续。
5. TDD：`test_route_modes.py` → 8 passed（分类示例矩阵/plan requires+deliverable/intent 落 state）；回归 guardrails SSE/full + eval harness 通过。

**状态**：Agent 已能对"只要SQL/简短回答/周报/探索/口径/Python/解释结果/为什么"给出不同 task_type 与计划；sql/quick 实际裁剪，其余按 full。
**待办**：UI 渲染 Plan 卡片（TASK DETECTED/DELIVERABLE/PLAN ✓）；python_code/markdown_doc 输出落地；LLM Router 富化。

## Day 9d·ExecutionPlan 可视化 + markdown/python 输出落地

**后端**
1. `python_code` 落地：`terminal_python` 终态（交付可运行 pandas 脚本，引用会话 CSV 或占位；不强制取数/长报告）；graph 同步+流式都接入（python 在 planner 后即裁剪，避免 over-execute）。
2. `markdown_report`/`business_analysis` 走 full 报告但 intent 标明 task_type/deliverable；`_resolve_route` 统一给同步与流式注入 mode+intent（修复：此前 SSE(stream) 没走 ROUTE）。
3. SSE INIT/UNDERSTAND 事件透出 `intent` + `mode`。
4. TDD：route_modes → **11 passed**（python_code e2e、markdown label、stream intent+裁剪且无 REPORT/REFLECT）。

**前端（React web/）**
- `api.ts` AgentEvent 增 intent/mode；新增 `PlanCard.tsx`（TASK DETECTED / DELIVERABLE chips / PLAN 步骤 / 需要什么）；ChatMessage 渲染在阶段时间线前；`npm run build` 通过（tsc 干净，新 bundle 产出）。

**端到端（服务 8001）**
- “只要SQL…”→ intent sql + ```sql 输出 + 无 REPORT；“写 Python 清洗 CSV” → python + ```python；完整分析 → business_analysis + Executive Summary（默认 full 不受影响）。

**待办**：UI 已可展示 Plan 卡（需硬刷看）；E2 自由 Python 的真实沙箱写码仍待 D10。

## Day 9e·Plan 卡完成态联动
- `PlanCard` 接收 events：按阶段事件(STAGE_ORDER/REPLAN)推导 workflow 进度（maxRank→比例），FINSH 全部 ✓。
- 步骤状态三态：done=✓(emerald, 划线) / active=呼吸琥珀点 / todo=空心圆。
- ChatMessage 传 events；`npm run build` 通过（tsc 干净），服务已下发新 bundle（index-B2kOqZ-z.js, asset 200）。

## Day 9f·workflow 步精确落位事件
- modes：语义 workflow 步 → milestone token 映射（context/plan/sql/python/analyze/reflect/report/finish）+ `status_milestones` + `workflow_progress`（未知步仅 finish 算完成；进行中 last 步留待办）。
- chat SSE：逐事件累积 seen token，事件带 `workflow_progress:{done,total}`。
- 前端：AgentEvent/PlanCard 用精确 done（最新条），FINISH 全绿；无精确时退回比例。
- 验证：route_modes 12 passed（progress 单调、total=3、FINISH done==total）；端到端：UNDERSTAND done=1 → 首个 SQL 成功 done=2 → FINISH done=3/3。

**收口**：ExecutionPlan 可视化与精确打勾闭环完成。D9b–D9f 全部落档。

## Day 11（2026-09-09 · E2：自由 Python 失败自纠错）

**实现**：`pycode.deliver_python_code` 升级为有界重试循环（默认 3 次）：
每次 生成(python_code_gen)→沙箱验证；失败把**结构化错误回注**（"上次执行失败，请修复。错误：[第 N 次] …"）给下一次生成；成功记"第 N 次尝试通过 + stdout 摘要"；3 次全败如实告知最后错误（不假装通过）；无数据则不做沙箱验证。

**TDD**：`test_e2_pycode.py` 3 passed：
- 交付+沙箱验证通过（mock 端到端，含 sql_query 数据源）
- 负路径：危险代码仍被 AST 拒（回归）
- **自纠错**：注入 1/2 次坏代码（引用不存在列）→ 3 次均入轨迹，第 3 次 SUCCESS，报告标注"第 3 次尝试通过"，后两次生成都收到失败回注。

**进度**：D11/36 ✅。E2 剩 D12（UI/API 展示产物+溯源）与 D13（门禁+待验）。

## Day 12（2026-09-09 · E2：自定义产物/溯源对外 + 全量回归）

**实现**
1. chat.py：`GET /api/v1/chat/analyze/artifacts/{session}`（checkpoint→列出各 step 产物 CSV/PNG 等）；SSE FINISH 附 `custom_summary`（python 模式：attempts/validated/artifacts/data_source=sql）。
2. 修正工具契约测试：`test_all_seven_tools_declared` 纳入 `freeform`（8 工具）。

**TDD/回归**
- e2_pycode → 5 passed（含 artifacts 端点、SSE custom_summary）
- **全量回归 215 passed**（先 214+1 失败即上项，修正后绿）：guardrails/tracing/eval/hybrid/rerank/circuit/router/retry/permissions/error/prompt/memory×/lessons/cap/tenant/audit/checkpoint/no_progress/replan/branch/report/sql_timeout/missing/ui/docker_live/route/e2×/e1×

**进度**：D12/36 ✅（E2 只剩 D13 门禁+待验清单）。

## Day 13（2026-09-09 · E2 门禁收口）
- E2/ROUTE/E1 专项复验 **31 passed**（free_sql/freeform_exec/pycode 自纠错/route 模式与精确进度/artifacts+e1 trace）。
- 门禁达成：SQL（freeform 只读执行、注入拒绝、可溯源）+ Python（模型真写码→沙箱验证→自纠错→产物/artifacts/custom_summary）两条自定义分析端到端闭环；ROUTE 模式裁剪与 Plan 卡精确打勾可用。
- pending-real 更新：自由写码「实现完成(mock)，真实质量待 key」。
- **#39 E2 = completed**；解锁 E3。

**明日（D14）待办**：E3 SDD（会话最近数据集 + 指代触发 skip）→ 红测（同 session 指代上一结果不重跑全链）。

## Day 14（2026-09-09 · E3：会话数据集 + 指代识别 + 增量执行）

**SDD**：`docs/specs/E3/01-iteration.md`（last_dataset 契约 / 指代词与 FORCE 否定 / 增量执行不重跑发现链 / 失败回退全链）。
**实现**
1. `iteration.py`：`is_followup`（显式指代命中，FORCE 词优先否定）、`save_last_dataset`（成功数据步→short_term 存 csv/columns/rows≤20/sql/step_id）、`load_last_dataset`、`deliver_iteration`（以 CSV 为源，模型写增量脚本→沙箱验证→精简交付）。
2. `AgentState.last_dataset`；`nodes.run_executor` 成功后落数据集；`graph` 同步+流式在 Context 后接入 `_try_iteration`（命中则跳过 Planner/Executor 发现链）。
3. 修 `tools._to_result`：CSV 物化从仅 `sql_query` 扩到 **`freeform`**（否则自由 SQL 无产物、迭代无源）。

**TDD**：`test_e3_iteration.py` 4 passed——is_followup 矩阵 / save+load / **e2e：第一轮产 CSV→第二轮「基于上一结果，改为只看 region 1」mode=iteration 且无 schema_search、有 python_analysis** / FORCE 回全链（planner 被调用）。连同 E2 回归 14 passed。

**进度**：D14/36 ✅（E3 主体已通；D15-17 做细化+门禁）。

## Day 15（2026-09-10 · E3/02：增量意图分类 → 只执行目标阶段 + force_full_rerun）

**SDD**：`docs/specs/E3/02-targeted-iteration.md`（五类 kind 判定表与优先级 / stages+skips 契约 / 守卫回退 / 开关契约 / 可观测）。

**实现**
1. `iteration.py` 增：
   - `classify_followup`（确定性）→ `date_change | granularity | drilldown | filter | generic`；期间证据用正则（`20xx年`、`2024-03`、`近N天`、`Q1`、`本季度/上季度/去年同期`），**避免「按月」被误判为改期**；优先级 改期 > 粒度 > 下钻 > 筛选。
   - `iteration_plan(kind)` → `{kind, stages[], skips[]}`（`skips` 恒含 planner/schema_search/sql_query）。
   - `guard_iteration(ds, query, kind)` → 执行前确定性守卫：改期越界（请求年份 ∉ 数据集日期范围）/ 无日期列 / 时间粒度换算无日期列 → **不可增量**。
   - `save_last_dataset` 富化：探测日期列与 `date_min/date_max`（供守卫判定）。
   - `deliver_iteration(state, kind=...)`：把 stages 作为**明确指令**写进脚本 prompt（"只做 X，不要重新取数"），落 `state.iteration={kind,stages,skips,attempts,guard}`；守卫不通过抛 `IterationUnsafe` → 调用方回退全链。
2. `AgentState.force_full_rerun` + `iteration` 字段；`run_analysis/stream_analysis/stream_analysis_async(..., force_full_rerun=False)`；`_try_iteration` 优先级：开关 > 指代 > 数据集 > 守卫，回退原因写 `metadata["iteration_skip"]`。
3. API：`AnalyzeRequest.force_full_rerun`（同步+流式透传）；`AnalyzeResponse` 增 `mode/iteration`；SSE FINISH 事件增 `iteration`。

**TDD（先红后绿）**：`test_e3_iteration.py` **12 passed**（D14 的 4 条 + D15 新增 8 条）：
- `classify_followup` 矩阵（四类 + generic + 两条优先级）
- `iteration_plan` stages 非空且 skips 覆盖发现链
- 守卫：改期越界拒绝 / 期间内放行；时间粒度无日期列拒绝 / 筛选类免守卫
- e2e 下钻：第二轮 `mode=iteration`、`kind=drilldown`、**tools == {python_analysis}**（只跑目标阶段，无 schema_search）
- 负：改期越界 → 回退全链（planner 被调用）且 `metadata["iteration_skip"]` 有原因；期间内改期仍走增量
- 负：`force_full_rerun=True` + 明确指代 → 仍回全链
- API 契约：`AnalyzeRequest.force_full_rerun` 默认 False / 可置 True

**端到端（TestClient · mock）**
第一轮全链（schema_search/dataset_profile/sql_query×2/python_analysis）→ 四类增量各只跑 1 次 python_analysis：
`drilldown stages=['filter','groupby']` / `granularity ['regrain','aggregate']` / `date_change ['time_slice']` / `filter ['filter']`；
`改为 2025年6月` → mode=full（越界回退）；`force_full_rerun=True` → mode=full。

**回归**：离线全量 **231 passed, 5 skipped**（D12 基线 215 无回退）；eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 工具成功率 1.0 / avg LLM 5.0 / 溯源覆盖率 1.0，9/9 claims）。

**进度**：D15/36 ✅。E3 剩 D16（口径切换负路径 + 回归）、D17（门禁 + 演示脚本草稿）。
**待办**：① D16 补「未指代时仍全链」与口径漂移负路径；② UI 消费 SSE 的 `iteration` 字段展示「增量·下钻/改期/换粒度」徽标（后端已下发）；③ 真实模型下增量脚本质量 `[待真实验证]`（沿用 E2 口径）。

## Day 16（2026-09-10 · E3/03：口径切换 / 维度可用性守卫 · 负路径收敛）

**SDD**：`docs/specs/E3/03-caliber-switch.md`——负路径契约 N1（未指代）/ N2（口径切换）/ N3（维度不可用）+ 度量/维度词表与边界。

**问题**：01/02 保证「命中就只做那一步」，但**上一结果是某次 SELECT 的投影**——只含那次查询的列。
拿它硬算新口径（订单数）或新维度（城市）只会得到「列不存在」的失败，或更糟：**静默算错**。

**实现**（`iteration.py`）
1. `_MEASURE_HINTS` / `_DIM_HINTS` 词表（可扩展）：度量 `营收/收入/销售额 → revenue|sales|amount`、`订单数/量 → orders|order_cnt`、`客户数 → customers`、`销量`、`利润`；维度 `城市 → city`、`区域/地区 → region`、`产品/品类/类别`、`渠道`、`月份`、`日期`。大小写不敏感子串匹配（`region` 命中 `region_id`/`region_name`）。
2. `_unavailable(query, columns, mapping)` → 请求里提到但列里找不到的词；`guard_iteration` 在生成脚本**之前**拦截，`guard["missing"]` 列出缺失词、`reason` 写进 `metadata["iteration_skip"]`（可审计、可提示用户）。
3. 词表**只用双字词**（`月份`/`日期` 而非 `月`/`日`），避免 `2024年3月` 被当成「要月维度」、「按月」被当成「要月列」。
4. 收紧 `_GRAN_HINTS`：移除 `改成按/改为按/换成按`（它们只表示"切换"，不构成粒度证据），避免 `改成按订单数` 被误判为换粒度。
5. `save_last_dataset` 补 `_csv_columns`：0 行结果时读 CSV 表头取列名（否则守卫在空 columns 下会把一切判为不可用/可用）。

**TDD（先红后绿）**：`test_e3_iteration.py` **17 passed**（D14 4 + D15 8 + D16 5）：
- 负 N1：有数据集 + 「哪个区域的营收最高？」→ 不增量、planner 被调用
- 负 N2：数据集仅 `region_id/revenue` + 「改成按订单数看」→ 全链，`iteration_skip` 含「订单数」
- 负 N3：同上数据集 + 「下钻到城市看营收」→ 全链，`iteration_skip` 含「城市」
- 守卫单测：`missing == ["订单数"]` / `["城市"]`；值级词「只看华东的营收」不受约束
- 正：数据集含 `orders`/`region_id` → 度量切换与下钻**仍走增量**（守卫不能误杀）
- 同步修 D15 下钻用例：其数据集只有 `region_id`，查询由「下钻到城市」改为「下钻到区域」（原用例在旧行为下是**假绿**——沙箱拿不到 city 列）

**端到端（TestClient · mock）**：R1 全链 →「哪个区域的营收最高？」mode=full ✓；「下钻到城市」mode=full（缺城市）✓。
**一处需如实说明**：「改成按订单数」在端到端里**走了增量**——因为 mock planner 的 SQL 选了 fact 全列（`revenue/orders/customers` 都在），`订单数` 确实存在于上一结果，放行是正确行为；N2 的拒绝由窄投影单测（`SELECT region_id, revenue`）覆盖。守卫是**数据驱动**的，不是关键词硬拦。

**回归**：离线全量 **236 passed, 5 skipped**（D15 基线 231 无回退）；eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 工具成功率 1.0 / avg LLM 5.0 / 溯源 1.0，9/9）。

**进度**：D16/36 ✅。E3 剩 D17（门禁 + 3 分钟演示脚本草稿）。
**待办（并入 D17）**：① E3 门禁：01/02/03 三份规格契约全绿 + 端到端三主线；② `docs/progress/demo-script.md` 草稿（溯源→写码→迭代）；③ 增量结果是否应回写 `last_dataset`（当前增量轮保留上一数据集，便于连续下钻；如与"结果即新基线"语义冲突，D17 一并定夺）。

## Day 17（2026-09-10 · E3 门禁 + 演示脚本 + 一个 P0 沙箱缺陷）

### 一、E3/04 定稿与实现：增量产物回写为下一轮基线
**SDD**：`docs/specs/E3/04-baseline-writeback.md`（D16 遗留问题的定夺：**基线前移**，支持连续下钻）。
**实现**：增量步 SUCCESS 后，取**本步产出/覆盖的 CSV** → 前移为 `last_dataset`，带 `derived_from`（上一 step_id）可追溯；无产物则基线不变。
- 两处判定弯路（都记进规格"边界"）：
  1. **不能用** `ToolResult.artifacts` —— 它列的是整个 session workdir 的 csv/png/json，**含输入 CSV 与历史产物**，取用会回写错文件；
  2. **也不能只比"文件集合"** —— 模型常沿用固定文件名（`out.csv`）原地覆盖；且 workdir 跨轮次留存，上一轮遗留的同名文件会让 diff 恒为空（此坑在测试里真实复现了）。
  最终用 **mtime ≥ 本步起始时刻**（留 1s 粒度余量）判定，两类情形都覆盖。
**TDD**：`test_e3_iteration.py` **20 passed**（新增 3 条：真沙箱产 CSV → 基线前移 + `derived_from`；第三次请求入参 `data_csv == 新基线`；无产物 → 基线不变）。

### 二、门禁：E1→E2→E3 三主线端到端
1. **E1 溯源**：`/analyze` FINISH → `/analyze/trace/{sid}` `coverage={numeric:2, traced:2, rate:1.0}`，`claims[].evidence[]` 带 `sql_id` + `sql`。
2. **E2 写码**：`mode=python_code` 交付可运行脚本（```python 块）。
3. **E3 迭代**：R1 全链（基线 8 列）→ R2 `mode=iteration kind=drilldown stages=[filter,groupby]`，tools **只有 python_analysis** → 基线前移为新产物（列变 `region_id,revenue`，`derived_from` 指向 R1 step）→ **R3 入参 `data_csv = ...region_drill.csv`**（确实作用于新基线）。
   另：越界改期 → `mode=full`；`force_full_rerun=true` → `mode=full`。
**专项**：`e1_auto_trace + e1_trace_api + e2_pycode + e3_iteration + route_modes` = **46 passed**。

### 三、顺带查出并修掉一个 P0（E2/E3 真实路径被 mock 掩盖）
**现象**：为写"真沙箱"版回写测试时发现沙箱里 `df is None`。
**根因**：`sql_query` 物化出的 `csv_path` 是**相对项目根**的（`data/artifacts/<sid>/s1.csv`），而 `python_tool._run_subprocess` 让子进程 `cwd=<session workdir>` → `pd.read_csv` 相对解析失败 → `df=None`。
**为何一直绿**：Mock LLM 的脚本首行是 `if df is not None:`，于是"跑通"了；真实模型脚本会直接 `AttributeError: 'NoneType' has no attribute 'groupby'` 崩掉。**E2 的沙箱验证此前是假绿**。
**附带缺陷**：非零退出时 `raw` 不带 `error`（只有 `stderr`），`ToolResult.error=None` → E2 自纠错 / E3 增量重试拿到的是 "[1] None"，等于没有反馈。
**修复**（`app/core/tools/python_tool.py`）：① 子进程用的 `env_csv` / `wd` / `DA_WORKDIR` 绝对化（产物路径形态保持相对，不动 API 面）；② 新增 `_failure_error`，非零退出时把 stderr 末 3 行写入 `error`（docker 与 subprocess 两条路径都加）。
**TDD**：新文件 `tests/test_python_sandbox_handoff.py` **3 passed**（相对路径读得到 df / 能写 CSV 产物到 session workdir / 失败带可读原因）——先红后绿。

**回归**：离线全量 **241 passed, 5 skipped**（D16 基线 236 无回退）；eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 工具成功率 1.0 / avg LLM 5.0 / 溯源 1.0，9/9）。
**交付**：`docs/progress/demo-script.md`（3 分钟演示草稿：溯源→写码→迭代，含可复制的 curl 与看点点位）。

**进度**：D17/36 ✅ —— **E3 门禁达成**（01/02/03/04 四份规格契约全绿 + 三主线端到端 + 连续下钻链闭合）。
**明日（D18）**：Gate-1 演示 dry-run（跑通三条主线、产出 `demo-script.md` 定稿、记录阻塞项）。
**待办**：① UI 消费 SSE 的 `mode/iteration` 展示"增量·下钻/改期/换粒度"徽标；② 沙箱修复后的 E2 真实写码质量仍 `[待真实验证]`（key 到位后按 pending-real 转真实）。

## Day 18（2026-09-10 · Gate-1 演示 dry-run）

**状态**：mock 三主线**脚本化跑通**；真实模型模式**未跑通**（余额 402），并顺带查出 2 个阻塞项（1 个已修、1 个产品缺陷待决）。

### 一、环境盘点（先于 dry-run）
- 8000/8001 上各有一个 **9/9 22:43–22:45 启动的陈旧服务**（D15–D17 之前的代码：无 `force_full_rerun`/`iteration`/沙箱修复）→ 已终止，dry-run 前用当前代码重起。
- `.env`：`MOCK_LLM=false` + **真实 key**（OpenRouter，`openai/gpt-4o-mini`）。最小探测 **HTTP 200 / 3.5s，key 有效**。

### 二、真实模式：402 → 静默降级（本轮最大发现）
真实请求 32.6s 返回 `FINISH`、5 步计划、2 findings，**但报告正文写着「Mock分析师未做深度统计推断」**。查日志：

```
LLM call failed (context|planner|analyst); falling back to mock:
  Error code: 402 ... "You requested up to 2048 tokens, but can only afford 1798"
LLM circuit open (reflection|reporter); skipping network call: circuit OPEN
```

- 根因：`LLM_MAX_TOKENS=2048`（`app/config.py:36`）+ 账户余额不足 → **每次真实调用都被拒** → 路由器 catch 后**静默降级为 Mock**；连续失败后熔断器打开，后续阶段不再发网络请求。
- **为什么危险**：`/health` 报的是**配置值** `mock_llm: s.use_mock_llm`（`health.py`），响应 `AnalyzeResponse` **无任何降级字段**，`router.fallback_events()` 只服务测试。于是"真模型配置 + 全程模板兜底"对外表现得**一切正常**。铁律 3「任何静默降级判失败」目前只在测试侧由 conftest 兜住，**运行态没有**。
- 结论：真实三主线**未验证**（不是 key 无效，是额度不足）。pending-real 已如实更新。

### 三、mock 模式：脚本化 dry-run 全绿
产出 `scripts/demo.sh`（"脚本化跑通三条主线"的落地物），并修掉一个 Windows 真坑：
- **坑**：`curl -d '{"query":"中文…"}'` 在 Git Bash 下中文按本地代码页传给原生 curl → 服务端 `{"detail":"There was an error parsing the body"}`，0.2s 返回、**管道根本没跑**。
- **修**：统一把请求体写 UTF-8 文件再 `--data-binary @file`（脚本内 `ask()` 封装）。
- **执行结果**：① E1 `FINISH/full/2 findings`、`coverage 1.0 (2/2)`、`first_claim=step_4`；② E2 自由 SQL 交付 + 100 行只读预览、`mode=python_code` + 代码块、artifacts 2 项；③ E3 首轮全链 5 工具 → 下钻 `mode=iteration/kind=drilldown/stages=[filter,groupby]/tools=[python_analysis]` → 期间内改期 `kind=date_change` → **越界改期 `mode=full, iteration=null`** → `force_full_rerun` `mode=full`。

### 四、阻塞项（3 条）
1. ✅ **已修**：中文 JSON 内联 curl 失效 → 文件体 + `scripts/demo.sh`。
2. ⛔ **待你决策**：OpenRouter 余额不足 → 真实主线不可演示。方向：充值，或调低 `LLM_MAX_TOKENS`（额度只够 1–2 次调用，跑不完整链）。
3. ⛔ **产品缺陷（建议尽快修，属防假绿主线）**：降级不可见。建议：`AnalyzeResponse` 增 `degraded: bool` + `llm_fallbacks: [{stage,error}]`；`/health` 反映**真实可达性**而非配置值；熔断打开时显式告警。

### 五、交付1
- `scripts/demo.sh`（可复现 dry-run，跨平台编码安全）
- `docs/progress/demo-script.md` **v2**（含执行结果表、真实模式前置检查、阻塞项）

**回归**：本日未改产品代码（仅新增 `scripts/demo.sh`），D17 基线 241 passed 不受影响。
**明日（D19）**：E4 定稿（profile 质量基元 + 脱敏开关 + reflection 口径可比检查）。
**待办**：① 阻塞项 2/3 的决策；② UI 消费 SSE `mode/iteration`；③ 沙箱修复后的 E2 真实写码质量仍 `[待真实验证]`。

## Day 19（2026-09-10 · E4 规格定稿 + TDD 红）

### 一、SDD：E4 三份契约定稿
| 规格 | 内容 | 实现日 |
|---|---|---|
| `docs/specs/E4/01-profile-quality.md` | `dataset_profile` 质量基元：主键唯一 / join 放大 / 粒度 / 日期连续性 | D20 |
| `docs/specs/E4/02-masking.md` | 输出脱敏分级（默认**开**；`sample`/`strict`；单一收口在工具结果进上下文处） | D21 |
| `docs/specs/E4/03-caliber-comparability.md` | Reflection 口径可比检查（期间长度/过滤条件/分母/迭代漂移） | D22 |

**动因**：分析师拿到表/结果集最先要问的四件事——主键唯不唯一、join 有没有放大、一行什么粒度、日期连不连续——
现状 profile 一个都答不了，而这四件事正是"结论翻车"的常见来源（重复计数、放大后求和、粒度误读、稀疏日期当连续）。

### 二、设计决策（写进规格的理由）
1. **"唯一"是弱信号**：样例库 `revenue` 是浮点度量，也恰好 3120/3120 唯一（实测）。
   若只报"候选键"会误导；故 **`candidate_keys` 按列序全量透明列出**，另给 `likely_key`
   （优先 `*_id` 且非浮点度量的候选）供下游用。启发式只影响排序，不改变候选内容。
2. **`grain` 描述结果集粒度，与 `declared_key` 解耦**：`fact_sales` 声明 `key=["region_id"]`
   时虽不唯一，但结果集仍是行粒度 → `grain` 仍为 `row`。避免把两个概念混成一个字段。
3. **join 放大需要对照**：只给 `sql` 无从知道基准，故引入可选 `base_table`；
   两者齐备才算 `factor = result_rows / base_rows`（阈值默认 1.5，可配）。
4. **联合键用子查询**：`COUNT(*)` vs `COUNT(*) FROM (SELECT DISTINCT k1,k2…)`，
   避开 `k1||k2` 拼接在不同方言下的类型陷阱（数值/日期转字符串的隐式行为）。
5. **顺手修既有隐患**：现实现把 `table` 直接 f-string 拼进 SQL（`SELECT COUNT(*) FROM {table}`）
   → 规格要求标识符白名单 `^[A-Za-z_][A-Za-z0-9_]*$`，列名同理；非法即拒绝且**绝不拼进 SQL**。

### 三、TDD 红
`tests/test_e4_profile_quality.py` **13 用例**（红测重点：主键唯一判定）：
- **红证据（实现前）**：`11 failed, 2 passed`。通过的两个是**兼容性**用例（`row_count`/`null_ratio` 保留、
  不存在的表可读失败）——它们本就该绿，作为"实现不得破坏现状"的锚点。
- 覆盖：候选键/`likely_key`/`declared_key`、非唯一键（`duplicate_rows == 3115`）、联合键唯一、
  聚合结果 `grain == "aggregated"` 且 `is_unique is None`、join 放大 `factor == 624.0`、
  日期稀疏（`distinct_days 52 / expected 358 / missing 306`、gap 样例）、
  非法表名与非法列名被拒、缺 `table`/`sql` 参数报错。
- **为守住铁律 5（改动不破坏基线）**：11 条红测钉为 `xfail(strict=True)`，D20 实现后**必须删除标记**转绿；
  `strict=True` 保证"实现已落地但忘记撤标记"时套件报 XPASS 错误，逼迫清理。

### 四、门禁
离线全量 **243 passed, 5 skipped, 11 xfailed**（D17 基线 241 + 2 兼容用例；无回退）。

**进度**：D19/36 ✅。E4 规格已定稿，实现节奏 D20 profile 基元 → D21 脱敏 → D22 口径可比 + eval golden → D23 门禁。
**遗留（D18）**：① 真实模型主线因 OpenRouter 余额不足未跑通；② 「降级不可见」产品缺陷待决策修复 —— 两项仍在等你拍板，不阻塞 E4。

## Day 19b（2026-09-10 · DEGRADE/01：修复「降级不可见」产品缺陷）

**动因**：D18 dry-run 暴露——真实 key 被 402 拒绝后路由器**静默降级为 Mock**，
而 `/health` 报配置值、响应无降级字段，对外一切正常，只有报告正文露出「Mock分析师」。

**SDD**：`docs/specs/DEGRADE/01-visible-degradation.md`。

**实现**
1. **归因**：`tracing.current_run_id()`；`record_fallback()` 记 `run_id`（取当前 trace 上下文）+ `ts`，
   `error` 截断 300 字（402 body 很长，不能整段回前端）；`fallback_events(run_id=None)` 支持按次过滤（缺省全量，向后兼容）。
2. **响应可见**：`AnalyzeResponse.degraded` + `llm_fallbacks[]`；SSE 每个事件带 `degraded`（降级当刻即可提示），FINISH 带明细。
   `graph._attach_llm_fallbacks()` 在同步返回处与流式每次 yield 前挂载。
3. **`/health` 分清「配置意图」与「观测事实」**：新增 `llm_mode`（配置）、`llm_degraded`（观测：最近一次真实调用是否降级）、
   `llm_fallbacks_total`、`llm_last_error`；`mock_llm` 保留原语义。
4. **`GET /health/llm` 主动探测**：1 token 真实调用，**绝不降级**（直接调用、异常即 false，且不写降级事件、不动熔断器）——
   若走 `get_llm().complete()`，探测会被自己的兜底骗过。
5. **日志**：降级与熔断从 `warning` 提到 **`error`**（可被告警规则捕获）。
6. **主动 mock ≠ 降级**：`MOCK_LLM=true` 时 `degraded=false`——两件事必须可区分。

**TDD**：`tests/test_degrade_visibility.py` **10 passed**（先红后绿，红时 9 failed / 1 passed）。

**真实场景验证（复现 D18 原况，不花额度）**：`LLM_MAX_TOKENS=3000` + 真实 key → 真实 402：
```
{"status":"FINISH","degraded":true,
 "fallback_stages":["context","planner","analyst","reflection","reporter"],
 "first_error":"Error code: 402 - {'error': {'message': 'This request requires more credits, or fewer max_..."}
```
`/health` 随后：`{mock_llm:false, llm_mode:"real", llm_degraded:true, llm_fallbacks_total:5, last:"Error code: 402 ..."}`。
`/health/llm` 探测：`{reachable:true, latency_ms:3538}` —— **顺手印证 D18 诊断**：1 token 在额度内能通，
而流水线 2048 的请求全被拒。故规格补了一条语义边界：**「可达」≠「跑得动」**，前者看探测、后者看观测字段。

**顺带发现的性能问题（未改产品代码）**：`.env` 的 `REDIS_URL` 指向未启动的 Redis，
每次操作要等 ~2s 连接超时 → 每个节点 ~4s（`run_context` 4.4s、每步 executor 4.4s、reporter 12.3s），
本地跑一次全链 ~29s、全量回归 ~3.5 分钟。测试侧已在 fixture 里关掉中间件（本文件 209s → **7.7s**）。
**建议**：给 `short_term._redis()` / `cache/redis.py` 加短连接超时（如 `socket_connect_timeout=0.2`），
让"配置了但没启动"的中间件**快速失败**而不是拖满超时——这是独立的小改动，待你确认再动。

**文档**：`docs/部署上线.md` 自检清单从「`mock_llm:false`」改为
「`llm_mode=="real"` 且跑完一次分析后 `llm_degraded==false`」，并补 `/health/llm` 与「可达≠跑得动」说明。

**回归**：离线全量 **253 passed, 5 skipped, 11 xfailed**（243 + 10 新用例，无回退）。

**收工状态**：D18 阻塞项 3 已闭环。剩阻塞项 2（OpenRouter 余额）——现已有 `degraded` 字段与 `/health` 观测值兜底，
即使余额不足也不会再"假装正常"。**D20** 回到 E4：实现 profile 质量基元并把 11 条 xfail 转绿。

## Day 19c（2026-09-10 · DEGRADE/02：中间件快速失败）

**动因**：D19 定位"测试为什么慢"的后续——`.env` 配了 `REDIS_URL` 但本机 Redis 没启动，
每次操作等满 OS 级 TCP 超时（实测 **2.04s/次**），摊到每个流水线节点 ~4s。

**SDD**：`docs/specs/DEGRADE/02-fast-fail-middleware.md`。

**实现**
1. `config`：新增 `redis_connect_timeout_s=0.2`、`redis_socket_timeout_s=0.5`（跨机房部署可上调）。
2. `infrastructure/cache/redis.py`：新增 `build_client()`，`Redis.from_url(..., socket_connect_timeout=…, socket_timeout=…, retry_on_timeout=False)`
   ——**不**在超时上再退避重试（重试是"想连上"的策略，这里要"快点放弃"）。
3. `core/memory/short_term.py::_redis()` 从"自己 `from_url`"改为**委托** cache 层——消除两处重复，超时设置单一收口。
   降级语义不变：URL 为空 → `None`；构造异常 → `None`；操作失败 → 内存兜底、绝不打断主流程。

**TDD**：`tests/test_fast_fail_middleware.py` **5 passed**（红时 2 failed：实测 2.04s/次；绿后整个文件 **0.98s**）。
覆盖：超时真被传进 `connection_kwargs`、不可达时一次操作 < 1.0s、降级语义不变（写进去仍读得出）、空配置返回 None。

**效果（实测）**
| 场景 | 修复前 | 修复后 |
|---|---|---|
| Redis 不可达时**单次操作** | 2.04s | **0.43s**（7 次调用共 3.0s） |
| 全链一次分析（Redis 未启动） | ~29s | **9–13s** |
| `test_degrade_visibility.py`（5 次全链） | 209s | **7.7s** |

**诚实说明**：全量回归总时长**没有明显变化**（196s → 203s）——因为剩下的 200 秒不在 Redis 上：
`test_document_ingest` 31.8s（ETL/embedding 加载）、两个退避重试用例各 23s（**测的就是指数退避，慢是设计**）、
docker-live 与若干 3–8s 的用例。**不要**把本次改动当成"套件提速"。
若还要压时间，应单独针对这三类处理（属独立工作，未做）。

**文档**：`.env.example` / `.env.prod.example` 增两个超时项；
`docs/部署上线.md` 自检清单加"Redis 超时按链路调整"（**跨机房必须上调**，否则正常 Redis 会被误判为不可用）。

**回归**：离线全量 **258 passed, 5 skipped, 11 xfailed**（253 + 5 新用例，无回退）。

## Day 20（2026-09-10 · E4/01 实现：profile 质量基元转绿）

**SDD**：`docs/specs/E4/01-profile-quality.md`（D19 定稿）。**11 条 xfail 全撤 → 18 passed**。

### 一、实现
1. `profile_tool.run()` 重写：
   - **参数**：`table` ⨯ `sql` 二选一（`sql` 只读、以子查询包一层）、`key`（单列/联合）、`base_table`（放大对照）、`date_column`；
     `table`/`key`/`base_table`/`date_column` 全走**标识符白名单**（`^[A-Za-z_][A-Za-z0-9_]*$`），非法即拒且**绝不拼进 SQL**
     ——修掉既有隐患（旧实现把 `table` 直接 f-string 拼进 `SELECT COUNT(*) FROM {table}`）。
   - **`key_uniqueness`**：`candidate_keys`（`nulls==0 且 distinct==row_count`，按列序全量透明）/ `likely_key`（候选里像业务键者）/
     `declared_key` / `is_unique` / `duplicate_rows` / `duplicate_ratio` / `grain`。
   - **`join_amplification`**：`result_rows / base_rows`，阈值取自 `settings.profile_join_amp_threshold`（默认 1.5），无 `base_table` 时为 `null`。
   - **`date_continuity`**：`min/max/distinct_days/expected_days/missing_days/coverage_ratio/sparse/gap_samples`。
2. `sql_tool.guard_readonly_sql()` 抽出为**共享**只读守卫（规格 §3 要求），`sql_query` 与 `dataset_profile` 同一套规则，避免两处漂移。
3. 编排可达：`build_executor_params` 允许计划步骤用 `input` 声明 `key/base_table/date_column/sql`（缺省仍只传 `table`，向后兼容）。

### 二、实现中被数据纠正的两处（都写回规格）
1. **`grain` 规则改对**：原规则"有任一候选键 → `row`"会误报——`GROUP BY region_id, product_id` 的 20 组里
   `SUM(revenue)` 恰好 20 个不同值，于是聚合结果被判成"行粒度"。改为以 **`declared_key`** 为准
   （能指出一个键才是行粒度）→ 该例 `aggregated` ✓。规格 §3 已记录修正理由。
2. **`likely_key` 的作用被验证**：同一例里 `candidate_keys == ["revenue"]` 如实列出（透明），
   但 `likely_key == None` → `declared_key == []` → `is_unique == None`——
   **"有唯一列"与"能宣称键"是两件事**，这正是"唯一是弱信号"的落地。新增用例固定该行为。
   （D19 写规格时我按纯规则列出候选，D20 用真实数据才发现必须把两者分开。）

### 三、TDD 增补（规格 §6 之外的卡片要求）
- **多表 join 放大样例红绿**：
  - 正：`dim_product JOIN fact_sales`（N:1）→ `factor 1.0`、`amplified False`（**不能误报**）；
  - 负：`fact_sales CROSS JOIN dim_region`（忘写 join 条件）→ `result 15600`、`factor 5.0`、`amplified True`；
  - 既有：自连接 624× 。
- 编排可达性 2 例（显式声明传入 / 缺省只传 table）。

### 四、修掉一个自己引入的回归
`build_executor_params` 里 `step.input` 直接取属性 → `test_schema_fallback` 用 `type("PS",…)` 造的**无 `input` 假 step** 报 AttributeError。
改为 `getattr(step, "input", None) or {}`。**教训**：全量回归抓到，单跑相邻套件（当时它们还没覆盖到）抓不到。

### 五、门禁
- 离线全量 **274 passed, 0 xfailed, 5 skipped**（D19c 基线 258 + 16）。
- eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 工具成功率 1.0 / 溯源 1.0，9/9）；
  **`avg_duration_s` 28.8 → 4.08**（DEGRADE/02 快失败在评测上的兑现）。
- 新基元实测输出（写入本日志备查）：
  `key_uniqueness{sale_id,revenue → likely sale_id, unique, grain row}`、
  `date_continuity{52/358 天, sparse, gaps 01-02…}`、`join_amplification{15600/3120, factor 5.0, amplified}`。

**进度**：D20/36 ✅。E4 剩 D21 脱敏、D22 口径可比 + eval golden、D23 门禁。

## S1（D21）· E4/04 质量门禁：让 profile 基元真正拦住错误结论

**背景**：用 `docs/测试用例.md`（300 条分析师基准）反查架构后确认的第一条硬缺陷——
E4/01 算出的 `key_uniqueness`/`join_amplification`/`date_continuity` **除 profile_tool 外零引用**：
每次分析都算，然后躺在 `tool_results` 里，没有门禁、不进报告。基准 53/300 要多表 join，
"拿放大后的结果求和"是必然事故。

**SDD**：`docs/specs/E4/04-quality-gate.md`。

**实现**
1. 新建 `app/core/agents/data_analyst/gate.py`（仿 `sources.py` 的**纯函数违规清单**范式，不花 LLM、不发新查询）：
   6 条规则全部**双条件（有事实 AND 有主张）**——单条件会把样例库点亮（`fact_sales` 日期天然稀疏 52/358 天）。
2. **join 放大两条检测路径**：① profile 声明式（E4/01）② **事后启发式**（规格 §3 新增）——
   默认路径的 profile 只画像表、拿不到 `join_amplification`，故改为用 `schema_search` 带回的各表行数当基线，
   对含 JOIN 的 SQL 步骤算 `结果行数 / 基线`；维表 join（N:1）factor≈1 不误报，笛卡尔积必报；拿不到基线不判。
3. 三级语义：`ANNOTATE`（披露）/ `REPLAN`（可补查）/ `BLOCK`（结论必然错）。**BLOCK 不直接 FAIL**——
   放大后的结论可补救，把缺口喂给 planner 通常一次能修对。
4. 披露：`AnalysisResult.quality_notes` → 报告新增 `## 数据质量与限制` 段（有问题才出现）；
   `AnalyzeResponse.quality_issues` + SSE 的 REFLECT/REPLAN/REPORT/FINISH 帧透出。
5. 阈值显式进 `config.py`（`profile_join_amp_threshold` / `profile_null_high_ratio`），
   顺手修掉 `profile_tool` 里 `getattr(settings, ...)` 兜底的坏先例。
6. **sync/stream 停滞逻辑抽共享**（护栏 2）：`_stall_after_replan()` 两条路径共用，流式此前**没有**无进展检测
   （同一场景会空转到 safety 上限才 FAIL，与同步结局不一致）。

**TDD**：`tests/test_e4_quality_gate.py` **27 passed**（纯函数 17 + 节点/编排/契约 10）。

**端到端实证（真跑那条放大 SQL，不是造假数据）**：让 planner 产 `fact_sales CROSS JOIN dim_region` →
`join_amplified_used` **BLOCK**（5000 行 / 基线 3120 = 1.60×），决策被收紧、缺口写进 `replan_objectives`、
报告出现披露段。

**两处必须如实说明的边界（写回规格 §1.1）**
1. **BLOCK 不等于请求 FAILED**：`graph` 对失败态一律 `run_reporter` 兜底，而 `run_reporter`
   **无条件**把 status 置回 `FINISH`（既有设计"失败也给一份报告"）。所以 BLOCK 的可见性是三条：
   `error` 含"质量门禁：…" + 响应 `quality_issues[]` 含 `severity=BLOCK` + 报告披露段。
   改 `run_reporter` 终态语义是独立改动（影响所有失败路径），不在本规格范围。
2. **迭代轮与轻模式不跑门禁**（它们本就跳过 Analyst/Reflection/Reporter），规格 §6 显式写明。

**顺带挖出并修掉一个既有 P0**（探针触发，非本次改动引入）：`_cap_output` 截断大结果时会在 `rows` 末尾
追加哨兵行 `{"_truncated": N}`，而 CSV 物化用 `DictWriter(fieldnames=列名)` 写全部行 → `ValueError` →
**任何超过 2000 行的查询整体 FAILED**，报的还是与业务无关的那句话。
修复：**上下文预算与落盘数据分开**——CSV 用未截断的完整行物化，哨兵行不写盘，新增 `csv_rows` 供对账。
`tests/test_tool_row_cap_csv.py` **3 passed**（大结果成功且 CSV 行数=真实行数 / 上下文仍截断且有标记 / 小结果无哨兵）。

**门禁**：离线全量 **304 passed, 5 skipped**（274 基线 + 30 新用例，零回退）；
eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 溯源 1.0）。

**注意**：期间发现仓库有**并行改动**（`graph.py`/`state.py` 出现 `app/core/attachments.py` 的 ATTACH/01 附件功能），
非本次工作；我的编辑均干净落地，全量回归已确认无冲突。

**下一步**：S2 口径可比 + 方法论（`E4/03` v1.1 + `E4/05`）。

## S2（D22）· E4/03 口径可比 + 方法论：把"分析师最常翻车的地方"变成门禁

**背景**：`docs/测试用例.md` 的 Rubric 权重里 **口径 535 / 分母 420 / Mix 1135 / 分层 775 / 分群 585** 占了大头，
而 7 份提示词里 `decomposition`/`mix`/`denominator`/`comparable`/`attribution` **一个都没有**。

**SDD**：`docs/specs/E4/03-caliber-comparability.md` → **v1.1**（补实现说明）。

**实现**
1. `app/core/agents/data_analyst/caliber.py`（纯函数，与 `gate.py` 同范式）：
   - `parse_period_days()`：`近N天/周/月/季/年`、`2024年3月`、`本季度/上月/去年同期` → 天数；
     **解析不出返回 None 且不判定**（宁缺勿滥）。
   - `caliber_check()` 三类结构性判定：`period_mismatch`（期间长度差 >20%）、
     `denominator_missing`（出现率类指标但无分子/分母声明）、`iteration_drift`（E3 迭代改了时间切片/粒度后又做环比）。
   - `apply_caliber()` **只收紧**：`iteration_drift` → REPLAN（跨口径比较是结论错误，不是措辞问题）。
2. 状态：`CaliberIssue` / `CaliberCheck` 挂 `ReflectionResult.caliber_comparability`。
   **刻意不复用 `ReflectionDimension`**——后者 `_coerce` 会把 issues 压成字符串，eval 的结构化断言就废了。
3. 披露：报告新增 `## 口径说明` 段（有才出现）。
4. **迭代轮耦合**（规格 S2 就必须定夺的那条）：`deliver_iteration` 绕过 Reflection→Reporter，
   故在增量终态直接调 `caliber_check(..., iteration=...)`，命中漂移时把 `> ⚠ **口径提示**` 追加进增量报告。
5. 提示词**只追加**（planner.md 被 `test_prompt_negative` 钉死）：
   `system.md` 加量价拆解/Mix 结构效应/贡献度/分层分群/分母纪律/可比性四对齐/归因纪律；
   `analyst.md` 加拆解与口径要求；`reflection.md` 加第 7 维 JSON 契约。

**TDD**：`tests/test_e4_caliber.py` **16 passed**（含期间解析矩阵、三类问题正负例、决策只收紧、
节点写入结构化结果、报告渲染）；`test_e3_iteration.py` 增 1 例（迭代跨口径比较必须提示）。

**实现中踩的坑（写回规格 §6）**：期间模式表**顺序即优先级**——`2024年3月` 被泛化的 `月` 先命中判成 30 天，
必须把具体写法前置；`parse_period_days` 的矩阵用例正是靠这条抓出来的。

**未实现（诚实标注）**：`unit_mismatch`（万元/亿元混用）与 `filter_mismatch`（含不含退款）语义判读 → `[待真实验证]`。

**门禁**：离线全量 **320 passed, 5 skipped**（304 + 16，零回退）；eval mock 基线不变。

**剩余**：S3 澄清回路（前后端 10 处）→ S4 业务语义层 → S5 统计+对抗 → S6 评测接入。

## S3（D23）· CLARIFY/01 澄清回路：模糊请求应当反问，而不是报错终止

**背景**：基准里 **38/300 涉口径澄清、19 条是"该不该"的决策题**。真实分析师遇到模糊请求会反问一句；
我们旧的行为是 `status=ERROR` + `error="需要澄清: …"` **直接终止**，而且**在 `state.context = ctx` 之前
就 return**，结构化问题被丢进错误字符串——问题本身都拿不到。

**SDD**：`docs/specs/CLARIFY/01-clarification-loop.md`。

**实现（后端）**
1. `AgentStatus` 增 `"CLARIFY"`（一次对话回合的**终止态**）；`run_context` **先落 context 再分支**，
   `error=None`，问题进 `metadata["clarification"]={questions,assumptions,objective}`。
2. **续跑语义**（错了会死循环）：pending 存 `short_term`（键 `pending_clarification`——**不是 state 字段**，
   续跑会重建 state）；下一轮把 `pending_clarification` 回注 context payload，解析成功后清除。
3. **`resume_analysis` 独立语义**：旧实现把"非 FINISH"一律当重试 → 会重跑 Context **再次反问**；
   现在 CLARIFY + 无新 query → **原样返回**（让调用方重新展示问题）。
4. **反问上限**：连续 2 轮后第 3 轮按已有假设推进（`clarify_rounds` 计数），绝不无限反问。
5. `graph` 两条路径都在 CLARIFY 早退（不进 planner）；CLARIFY 帧**照常**经 `_attach_llm_fallbacks`
   （否则"降级后反问"时降级不可见）。
6. API：`AnalyzeResponse.clarification`、`_status_message["CLARIFY"]="需要澄清"`、SSE 帧带 `clarification`；
   `AnalyzeRequest.clarification_answer` 真正被消费（并入本轮 query，否则字段是死的）。
7. 提示词只追加 `context.md`：pending 的合并规则 + **最多 3 个**具体可答的问题 + 能自己查的别问。

**实现（前端，跨栈 6 处）**
`api.ts`（`Clarification` 接口 / `AgentEvent.clarification` / `stageLabel` / **`isTerminal` 含 CLARIFY**）
→ `types.ts`（`Message.clarification/answered`）→ `App.tsx`（存 clarification、**不**写进 error、作答后 `send(answer)`）
→ 新 `ClarifyCard.tsx`（问题清单 + 输入框 + 发送）→ `ChatMessage.tsx`（渲染卡片）→ `StageTimeline.tsx`（tone=warn、
不再当作"仍在运行"）。`npm run build` 通过（tsc 干净）。

**TDD**：`tests/test_clarify_loop.py` **17 passed**（节点/编排早退/流式末帧/续跑清 pending/payload 回注/
轮次上限/resume 不循环/API/SSE/前端源码级契约 4 条）。
**最易漏的一处**（规格里点名）：`isTerminal` 不含 CLARIFY → SSE 结束后界面永久转圈，已用源码级断言钉住。

**门禁**：离线全量 **345 passed, 2 failed, 5 skipped**。
⚠️ **那 2 个失败与我无关**，属于**并行开发中的 ATTACH/01 附件功能**（`tests/test_attachment_routing.py`：
上传 CSV 未注册成可查询表、`schema_search` 未返回上传表、缺 `origin` 字段）。
证据：失败点全在我**未改动**的文件（`app/core/attachments.py`、`app/core/tools/schema_tool.py`），
且 `attachments.py` 的时间戳（18:25）**晚于**我的最后一次编辑（18:24）。我的所有套件（含 S1/S2/S3）全绿。
eval mock 基线仍为 FINISH 1.0 / 断言 1.0 / 溯源 1.0。

**实现中踩的坑（第二次）**：用 heredoc 里的 Python 脚本写入含 `\n` 的 f-string 时，转义再次落成真实换行 →
`chat.py` 语法错误、4 个 API 测试连带失败。已改用 Edit 工具直接改。**教训：写含转义的源码用 Edit，不要走 heredoc 脚本。**

**剩余**：S4 业务语义层 → S5 统计+对抗 → S6 评测接入。

## S4（D24）· SEMANTIC/01 业务语义层：让 Agent 看懂列的业务含义

**背景**：Agent 只看得见**列名**。`schema_search` 只返回 `{name, type}`，于是 `region_id=1` 就是数字 1，
不是"华东"；基准 53/300 要多表 join —— 没有语义，**join 键只能靠猜**（猜错的代价 E4/04 量过：5 倍放大）。
更关键的是：**`planner` 此前完全看不到 schema**（`run_planner` 的 task_context 只有 context+mode），
它凭 LLM 从问题里猜维度，而不是看着真实表结构规划。本规格第一次把"数据长什么样"喂给它。

**SDD**：`docs/specs/SEMANTIC/01-business-semantics.md`。

**实现**
1. `app/core/semantics.py`（纯函数 + 有界查询，**不花 LLM**）：
   - `infer_relationships()` 按**命名约定**推断维表键与多对一关系（样例库**没有真实 FK 约束**），
     `confidence="naming_convention"` 如实标注——上游不得当事实。
   - `collect_semantics()` 采集维表取值（`SELECT key,label ... LIMIT cap`），
     带 `short_term` 缓存（`semantics_ttl_s`，键含 db_url 哈希）。
   - `describe_semantics()` 输出**有界**紧凑文本（≤6 维 × ≤8 值）直接进 prompt。
   - **任何失败都退化为空语义，绝不抛**（元数据查询绝不能拖垮分析）。
2. **注入三处**：`run_context`（"华东"→region_id）、`run_planner`（首次获得真实表结构）、`run_analyst`（结论写"华东"而非"区域 1"）。
3. `dataset_profile` 增 `enums` / `enums_skipped`（低基数、跳 PII）。
4. 顺手校正 `schema_search` 的 ToolSpec 漂移（`required:["query"]` 而实现只读 `keyword` → 改 `required: []`，
   `query` 作为 `keyword` 别名兼容）。
5. **入库走离线脚本** `scripts/build_semantics.py`（`--dry-run` 可预览，source 带内容哈希幂等）。
   理由：分析请求全程只读（§22 NO WRITE ACCESS），把写库放进请求路径等于"只读分析"在运行中改状态。

**实测**（`scripts/build_semantics.py --dry-run`）：
```
- dim_channel: channel_id → channel_name（直销/合作伙伴/线上）
- dim_product: product_id → product_name（企业版SaaS/硬件终端/咨询服务/培训）
- dim_region: region_id → region_name（华东/华北/华南/西部/境外）
- 关系(fk-naming)：fact_sales.region_id → dim_region.region_id；…product_id/…channel_id
```

**TDD**：`tests/test_semantics.py` **14 passed**（PII 识别/关系推断/真实库采集/描述有界/缓存命中/
失败不致命/注入 context+planner/profile enums/ToolSpec 校正/query 别名）。

**实现中抓到自己写的一个静默降级**（正是本项目一直在防的模式）：`profile_tool` 里
`from ....core.semantics import is_pii_column` **点号多了一层** → ImportError → 我的兜底 `return False`
→ **PII 跳过完全失效**（姓名的值真的会进上下文）。已修导入深度，并把兜底改为**失败即关闭**
（检测不出 PII 时一条枚举都不采）。是靠"含 PII 列的表必须跳过"这条用例抓到的。

**另修一个评测指标失真**（非我引入，如实说明）：并行会话的 P1-1 并行执行新增 `run_executor_all`，
整波只发**一个** `@trace("executor")` span → eval 的 `tool_calls` 从数 span 得到的 4.8 掉到 **1.0**。
功能没坏（`expected_tools` 断言读的是 `state.tool_results`），但 `docs/progress/metrics.md` 追的指标失真。
已改为**从 `state.tool_results`（事实来源）统计**，不再受埋点改动影响 → 恢复 `avg_tool_calls 4.8`。

**门禁**：离线全量 **489 passed, 5 skipped, 0 failed**（并行会话的附件功能此时也已完成，全绿）；
eval mock 基线 FINISH 1.0 / 断言 1.0 / 溯源 1.0 / avg_tool_calls 4.8（恢复后）。

**剩余**：S5 统计+对抗门禁 → S6 评测接入 10 条 golden。

## S5（D25）· E5 统计严谨 + 对抗性数据质量指令

**背景**：基准 L4（最难一档）几乎全是**统计判断**——辛普森悖论（"总转化率涨 7% 说明优化成功吗"）、
实验波动（"为什么 A/B 结果每天波动"）、检验（"哪个模型更好"）、相关≠因果（"NPS 和续费率有关系吗"），
Rubric 里 `统计 475`。另有一类**对抗性指令**：**"你分析完告诉我结论，数据有问题也别管。"**

**SDD**：`docs/specs/E5/01-statistical-rigor.md` + `E5/02-adversarial-quality.md`。

**实现**（`app/core/agents/data_analyst/rigor.py`，复用 `gate.py` 的 `GateIssue`/`apply_gate`）
1. `AnalysisResult.stats_notes: list[StatsNote]`（claim/method/n/**significant**/note，含松散结构容错）。
2. `ReflectionResult.adversarial: AdversarialCheck`（结构化，供 eval 断言）。
3. 统计规则 5 条（**全部 ANNOTATE**——统计多寡取决于问题类型，硬拦会制造报警疲劳）：
   `untested_comparison`（有两期对比却无声明）、`significance_without_n`、
   `hypothesis_strong_without_n`、`multi_comparison_unadjusted`、`causal_overreach`。
4. 对抗规则 2 条：`dq_override_requested`（**不遵从**：仍必须披露）；
   **`dq_override_silent`（命中覆盖词却完全无质量声明）= BLOCK —— 本项目唯一新增的 BLOCK**。
   理由：这不是"分析得不够好"，而是**顺从用户隐瞒已知问题**，与铁律 4 直接冲突。
5. **不得依赖 scipy**（沙箱只有 pandas/numpy/matplotlib 且只挂单个 CSV）：门禁只检查
   "该声明的有没有声明"，不代跑检验；`stats_notes` 的正确性标 `[待真实验证]`。
6. 三道防误报闸：每 code 只报一次 / 必须"有事实且有主张" / 描述性结论不触发。
   否定优先：**"别管数据问题，但也要告诉我哪里有问题" → 不算覆盖请求**。
7. 提示词只追加：`analyst.md`（stats_notes 契约，"没做检验就写未检验，别声称显著"）、
   `system.md`（对抗性指令：可以顺从分析，**绝不顺从沉默**）。

**TDD**：`tests/test_e5_rigor.py` **17 passed**（统计 5 条正负例 + 去重 + 描述性不报；
对抗 5 条含否定优先；节点级 BLOCK 生效 + 披露强制写入 + 结构化字段透传）。

**顺带修掉一个既有隐蔽缺陷**（`state.py`）：`AnalysisResult` 的容错把非 dict 条目包成 `{"text": repr}`，
于是**传模型实例（`Hypothesis(...)`）会被静默毁掉**——`result` 等字段全丢。
改为 `isinstance(item, (dict, BaseModel))` 放行。这个坑只在"用模型实例构造"时出现，
而测试与真实路径（`model_validate(裸 JSON)`）恰好都绕过了它，所以一直没暴露。

**门禁**：离线全量 **506 passed, 5 skipped, 0 failed**；eval mock 基线全保（FINISH 1.0 / 断言 1.0 /
溯源 1.0 / avg_tool_calls 4.8）。

**剩余**：S6 评测接入（10 条 L2/L3 golden + 结构化断言）。

## S5（D25）· E5 统计严谨 + 对抗性数据质量指令

**背景**：基准 L4 最难的一档几乎全是统计判断（辛普森/功效/检验/相关≠因果，Rubric `统计 475`），
外加一类对抗性指令——**"你分析完告诉我结论，数据有问题也别管。"**

**SDD**：`docs/specs/E5/01-statistical-rigor.md`、`E5/02-adversarial-quality.md`。

**实现**（`app/core/agents/data_analyst/rigor.py`，与 `gate.py` 同范式：纯函数、不花 LLM、**不依赖 scipy**）
1. **E5/01 统计声明**：新增 `AnalysisResult.stats_notes: list[StatsNote]`（claim/method/n/**significant**/note），
   门禁查 5 条：`untested_comparison`（有两期数值对比却无任何统计声明）、`significance_without_n`、
   `hypothesis_strong_without_n`、`multi_comparison_unadjusted`（≥5 组对比未说明校正）、`causal_overreach`。
   **全部 ANNOTATE**——统计判断的多寡取决于问题类型，硬拦会制造报警疲劳。
2. **E5/02 对抗性指令**：`AdversarialCheck` + `dq_override_requested` / **`dq_override_silent`**。
   后者是本项目**唯一**新增的 BLOCK：静默顺从"别管数据问题"不是"分析得不够好"，
   而是**顺从隐瞒已知问题**，与铁律 4 直接冲突。命中覆盖请求时强制写入 `quality_notes` 披露。
   **否定优先**（"别管数据问题，但也要告诉我哪里有问题" 不算覆盖请求）。
3. 提示词只追加：`analyst.md` 加 `stats_notes` 契约（没做检验就写 `significant: null` 并说明），
   `system.md` 加 **Adversarial Instructions** 段（"可以按用户要求分析，但不能按用户要求沉默"）。

**TDD**：`tests/test_e5_rigor.py` **17 passed**。

**实现中挖出一个既有缺陷**：`AnalysisResult` 的容错把非 dict 条目包成 `{"text": ...}`，
于是**传 pydantic 模型实例会被悄悄毁掉**（`Hypothesis(hypothesis=..., result="SUPPORTED")` 的 `result` 字段丢失）——
我的两条用例正是被它挂掉的。已修（`isinstance(item, (dict, BaseModel))` 原样放行）。
这类"静默丢字段"与门禁要防的静默降级是同一类问题。

**门禁**：全量 **506 passed**（S4 的 489 + 17）。

---

## S6（D26）· 评测接入：分析师 golden（**由并行会话完成，我验证并修一处红**）

**核实**：并行会话已把 S6 完整落地——`GoldenCase` 增 `expect_quality_codes` / `expect_caliber_kinds` /
`expect_refusal` / `must_have_limitations` / **`requires_real`**；`runner.evaluate_case` 实现全部对应检查；
新增 10 条分析师用例（`a_` 确定性可跑 3 条 + `r_` 需真实模型 7 条），覆盖对抗性指令、口径不等、
分母缺失、拆解先于归因、join 放大、因果越界、多重比较、辛普森。

**我修的一处红**：`test_per_case_counts_come_from_real_trace` 仍断言"每个用例都必须 FINISH"，
而新的 `requires_real` 用例在 mock 下是 `SKIPPED` → 套件变红。
修法不是简单跳过，而是**钉住"只有 requires_real 才允许 SKIPPED"**：
```python
real_gated = {c.id for c in GOLDEN if c.requires_real}
skipped = {c["case_id"] for c in cases if c["status"] == "SKIPPED"}
assert skipped <= real_gated, "非 requires_real 用例不得被跳过"
```
——否则"跳过"会变成刷通过率的后门（铁律 6）。

**eval mock 实测**（15 用例）：8 条真跑（FINISH 1.0 / 断言 1.0 / 溯源 1.0），
**7 条如实 SKIPPED 并单独计数**（`skipped_requires_real: 7`，不进通过率）；`tool_calls_total 38`。
其中 `a_dq_override_not_silent` 走轻模式（2 次 LLM、无 Reflection），靠并行会话在 `rigor.py` 追加的
`ensure_dq_disclosure` 轻模式兜底满足断言——轻模式跳过整条门禁，正是"静默顺从"最容易发生的地方。

**门禁**：全量 **512 passed, 5 skipped, 0 failed**。

---

## 「更懂分析师」冲刺收尾（S1–S6）

| 阶段 | 交付 | 规格 |
|---|---|---|
| S1 | 质量门禁：profile 基元 → 决策与披露（BLOCK/REPLAN/ANNOTATE） | `E4/04` |
| S2 | 口径可比 + 方法论（拆解/结构效应/分母/可比性） | `E4/03 v1.1`、`E4/05` |
| S3 | 澄清回路（CLARIFY 状态 + 前后端闭环 + 续跑不死循环） | `CLARIFY/01` |
| S4 | 业务语义层（维表枚举/键推断 → 注入 context/planner/analyst） | `SEMANTIC/01` |
| S5 | 统计声明 + 对抗性指令门禁（`dq_override_silent` 为唯一新增 BLOCK） | `E5/01`、`E5/02` |
| S6 | 15 条 golden（含 7 条 requires_real 门控）+ 结构化断言 | `E6/01`（并行会话） |

**基线**：全量 **512 passed / 5 skipped / 0 failed**；eval mock FINISH 1.0、断言 1.0、溯源 1.0。
**仍需真实 key**：所有 `requires_real` 用例、写码质量、语义判读、统计判断 → `docs/progress/pending-real.md`。

## E4/02 · 输出脱敏（v1.1 落地）

**背景**：规格 D19 定稿后一直顺延。手机号/邮箱/身份证/姓名一旦进 LLM 上下文就等于**出境且不可撤回**；
而分析师本机产物必须可用——所以是"**先落盘、再脱敏**"两件事。

**实现**：`app/core/security/masking.py`
1. **单一收口**：`tools._to_result()` —— 所有工具共用一条路径（不会漏网）。
   顺序：CSV 按**完整数据**落盘 → 再脱敏"进上下文"的那份。
2. **三级**：`sample`（默认，掩值）| `strict`（敏感列彻底不出现，连 `distinct` 都不给，防反推）|
   `none`（关闭）。**关闭会写审计**——"默认开"的边界不能被悄悄绕过。
3. **掩码算法**：`138****5678`（留前 3 后 4）、`z***@example.com`、其余等长星号；
   刻意**不做哈希**（低基数可反推，且对模型无信息增益）。
4. **值形态兜底**：列名没命中但值形如电话/邮箱/身份证/银行卡 → 照样掩；
   字符串**内部**夹着的号码也掩（如备注列里的"联系电话 139…"）。
5. **失败即关闭**：脱敏自身异常 → `rows=[]` + `masking_error`，**绝不 fail-open**。
   隐私控制与其他降级不同，不能"退化为可用"。
6. **模式表统一**：`semantics.is_pii_column` 委托 `security.masking.is_sensitive_column`
   （SEMANTIC/01 留的 TODO 已清），两处规则不再漂移。
7. 覆盖 `rows` / `enums`（维表枚举是 S4 新开的数据出口）/ `columns[].distinct`（strict）。

**TDD**：`tests/test_masking.py` **11 passed**（掩码算法/模式表共用/sample 与 strict/
值形态兜底/哨兵行不动/enums 与 profile/工具端到端「上下文掩码 + CSV 保留原值」/关闭留痕/失败即关闭）。

**实测**：`execute_tool` 返回的行样本里 `13812345678 → 138****5678`，而同一批数据的 **CSV 落盘仍是原始值**——
这正是"上下文"与"本机产物"的分界。

**文档**：`.env.example` 增三个开关；`docs/部署上线.md` 增 §4.5（默认开、strict 语义、审计位置、失败即关闭）。

**门禁**：全量 **523 passed, 5 skipped, 0 failed**（512 + 11）；
eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 溯源 1.0）——样例库无 PII，脱敏不改变行为。

**至此 E4 全部完成**：01 质量基元 ✓、02 脱敏 ✓、03 口径可比 ✓、04 门禁 ✓、05 方法论 ✓。

## E5 收口 · scipy + 交付物导出（D24–D26 补齐）

**SDD**：`docs/specs/E5/03-export.md`。

**1. scipy（D24 的"python 模板可用 scipy"）**
- `requirements.txt` + `docker/Dockerfile.sandbox` 都加上 `scipy`；装进 **`.venv`（服务与沙箱实际用的那个）**。
  踩了一次：先装到了 `venv/`，但测试与服务跑的是 `.venv/` —— 两个虚拟环境并存，必须认准。
- **真实沙箱实测**：`from scipy import stats; stats.ttest_ind(...)` → `p=0.3466` ✓
  （至此 E5/01 的门禁"不依赖 scipy"与"分析师能自己算检验"两件事都成立。）

**2. 交付物导出 `GET /api/v1/chat/analyze/export/{session_id}?format=zip|sql|report|csv`**
- **zip（默认）**：`report.md` + `queries.sql` + `trace.json` + `data/<step>.csv` + `README.txt`
  —— 分析师终于能"一次拿走"（此前报告在界面、SQL 在 trace 接口、CSV 在 artifacts 接口）。
- **安全**：只打包会话工作目录**之下**的产物（路径白名单，越界跳过并在 README 记明）；
  只读该 session 自己的 checkpoint；纯读，不新增写操作。
- **脱敏边界写进 README**：导出物**保留原始值**（E4/02 只约束"进 LLM 上下文"那份），
  并提示"对外分享前请自行确认"。这条**必须显式写**，否则会被误读为"导出物也是安全的"。
- UI：报告卡加「导出」按钮（与「复制」并列），源码级断言钉住（防只做后端漏按钮）。

**实测**：包内 `['README.txt','report.md','queries.sql','trace.json','data/step_3.csv','data/step_4.csv']`。

**踩的坑（第三次同类）**：README 文案里用了 ASCII 双引号嵌套 → `SyntaxError`。
教训再次生效：**含引号/转义的文本一律用 Edit 工具改，不要走 heredoc 脚本**。

**TDD**：`tests/test_export.py` **8 passed**（包内容/SQL 只读/CSV 保原始值/单文件格式/404/
**路径穿越不进包**/无产物也能导出/前端按钮）。
**门禁**：全量 **531 passed**；eval mock 基线不变（FINISH 1.0 / 断言 1.0 / 溯源 1.0）。

**进度**：D24–D26 收口完成 → 下一步 E7（多源 + Dockerfile.prod，D31–D34）。

## E7/01 · 多数据源 + 部署硬化（D31–D34）

**SDD**：`docs/specs/E7/01-multi-source-deploy.md`。

**1. 命名数据源（D31–D32）**
- `app/core/tools/datasource.py`：`resolve_source(name)` → `(url, dialect)`；
  **不传 = 主源**（既有行为完全不变，向后兼容是硬要求）。
- 配置 `DATA_SOURCES`（JSON 数组或 `name=url` 逗号列表）；**写坏不阻塞启动**（忽略该条 + warning）。
- 接入 `sql_query`/`freeform`/`dataset_profile`/`schema_search`（可选参数 `source`），
  计划步骤可用 `input.source` 指定；未知源名返回**可读错误并列出可用源**（分析师能自己纠正）。
- `/health` 的 `data_sources` **只回源名**，不回 DSN。
- ⚠ **明确不做跨源 JOIN/联邦查询**（需联邦引擎与下推优化，另一量级）：需要跨源就在各源取数后用
  `python_analysis` 合并。规格显式写明，避免被当成"已支持多源分析"。

**2. 部署硬化（D33–D34）**
- `Dockerfile.prod`：多阶段 / 非 root / HEALTHCHECK / **不整仓 COPY**（不带 tests、.git、node_modules）。
- **<2GB 的关键取舍**：默认**不装 `sentence-transformers`**（它会拉 torch，单这一项就把镜像推到 >2GB），
  检索自动降级 BM25（代码本就支持）；需要向量检索时 `--build-arg WITH_EMBEDDINGS=1`。
- `scripts/smoke_container.sh`：build→run→health→analyze→记录体积；**「跳过」是独立退出码 2**
  （CI 里既不能当通过=假绿，也不能当失败=误报）。
- `docs/部署上线.md` 增 §4.6 多源 / §4.7 生产镜像 / **§4.8 安全**（鉴权必须在网关后、
  **SSE 需 `proxy_buffering off`**、HTTPS 终止在网关、多租户的数据源隔离由 DSN 决定、
  导出物不脱敏需自行确认）。

**TDD**：`tests/test_multi_source.py` **11 passed** + `tests/test_deploy_hardening.py` **5 passed**。

**三个 Windows 坑（全是同源：路径/编码）**
1. 测试夹具把 `str(Path)` 拼进 DSN → 反斜杠进 JSON 变非法转义 → 解析失败。改 `.as_posix()`。
   （**我的补丁脚本自己又踩了一次同样的 `\U` 转义**，被迫改用 Edit 工具——教训第三次生效。）
2. `subprocess.run(["bash", ...])` 在 Windows 上解析到 **WSL 的 bash** → `/bin/bash not found`、退出码 1。
   改为显式找 Git Bash（`EXEPATH`/常见安装路径，且排除 system32）。
3. 子进程中文输出被宿主 locale（GBK）解码成乱码 → 断言失败。加 `encoding="utf-8"`。
   （与记忆里那条 eval runner GBK 坑同源。）

**容器冒烟实测（如实说明）**
- 脚本**跑起来了**：构建失败时正确输出 `[FAIL] 镜像构建失败` 并退出 1；跳过态 `[SKIP]` + 退出 2 —— 两条路径都是真跑出来的。
- **但镜像构建无法完成**：本机 Docker 三个镜像源全部不可用
  （`docker.m.daocloud.io` EOF、`docker.nju.edu.cn` **403**、`dockerhub.azk8s.cn` 不可达）。
  → **D33 的 `<2GB` 体积与容器内冒烟未实测**，已记入 `docs/progress/pending-real.md`。
  **不声称通过**（铁律 6）。

**门禁**：全量 **547 passed, 5 skipped, 0 failed**（531 + 16）；eval mock 基线不变。
**指标归档**（D35 前置）：`docs/progress/metrics.md` 重写——回归 169→552 用例、溯源覆盖率 13/13、
`skipped_requires_real: 7` 单独计数。

**剩余**：Gate-2 正式收口（D35–D36）：README/面试稿刷新、待验清单清点、计划 v2。

## Gate-2 · 收口（D35–D36）

**D35 指标归档** → `docs/progress/metrics.md` 重写（不再停在 D7）：
- 离线回归：**169 → 552 用例 / 547 passed / 5 skipped / 333s**
- eval mock：FINISH 1.0 · 断言 1.0 · 工具成功率 1.0 · **溯源覆盖率 13/13** · avg 工具 4.75 · avg LLM 4.62
- 用例总数 15（其中 `requires_real` 7 条**单独计数**，不进通过率）
- 新增能力指标：门禁规则 13 条、口径检查 5 类、语义采集 3 维表/3 关系、脱敏 3 个出口、交付包 5 类文件
- **未验证项如实记录**：生产镜像体积（镜像源不可用）、real 基线（余额）

**D36 文档收口**
- `README.md`：API 清单补 **CLARIFY / 质量门禁 / 交付物导出 / 溯源 / 产物**；
  新增「多数据源」与「默认开的安全边界」两段。
- `docs/部署上线.md`：新增 §4.6 多源 / §4.7 生产镜像与冒烟 / **§4.8 安全**（鉴权在网关后、
  **SSE 需 `proxy_buffering off`**、HTTPS 终止在网关、多租户数据源隔离由 DSN 决定、导出物不脱敏）。
- `docs/面试稿_STAR.md`：追加「v2 增量」——把这一轮讲成 STARD 结构，
  并留下两个"反直觉"点（BLOCK 为何不直接 FAIL；我自己踩的三次同类坑都被门禁抓住）。
- `docs/开发计划_企业化.md`：追加 **§8 计划 v2**——完成情况对照表（含 D27–D30 标"部分"、
  E7 标"镜像体积未实测"）、计划外新增（CLARIFY/SEMANTIC/DEGRADE）、未完成/受阻清单、
  **C 里程碑候选**（指标语义层重版 / 真实评测闭环 / 跨源联邦 / DLP / UI 对齐）。

**待验清单清点**：`docs/progress/pending-real.md` 现有 7 项（E2 写码质量、E5 统计判断、
eval real 基线、多方言 SQL、真实长报告溯源、E3 语义变体、E4/02 口径语义判读）+ 1 项环境阻塞（镜像源）。

**最终门禁**：全量 **547 passed, 5 skipped, 0 failed**；eval mock 基线不变。

---

## 计划执行总结（D1–D36 对照）

| | |
|---|---|
| 完整完成 | D1–D23、D24–D26（E5）、D31–D34（E7）、D35–D36（Gate-2） |
| **部分完成** | **D27–D30（E6）**：golden 与轻路径已做，**real 基线卡在 402 余额** |
| 计划外新增 | CLARIFY 澄清回路、SEMANTIC 业务语义层、DEGRADE 降级可见性/快速失败、E4/04 质量门禁 |
| 受阻（环境） | 生产镜像体积未实测（Docker 镜像源三选一全不可用） |

**三条里程碑**：M1（D18 可演示 MVP）✅ / M2（D30 信任主线）**除 real 基线外 ✅** / M3（D36 部署硬化 + 归档）✅（镜像体积除外）。

## INTERVIEW/01 · 八股文五项缺口补齐

**动因**：拿 `references/.../01-面试八股文/` 9 篇目录逐条 grep 对照项目实现，
找出 5 处"八股文会问、项目答不实"的缺口（约 70% 已落地）。**SDD**：`docs/specs/INTERVIEW/01-gap-fill.md`。

| # | 项 | 交付 | 用例 |
|---|---|---|---|
| ① | 记忆三因子打分（05.6） | `memory/scoring.py`：相关性 0.6 + 时效 0.25（指数衰减，半衰期 30 天）+ 重要性 0.15；`append` 补 `ts`；`search` 换 `rank()` | `test_memory_scoring.py` 14 |
| ② | LangGraph 测试覆盖 | 节点齐全 + **真跑到 FINISH** + 缺依赖可读报错 | `test_langgraph_path.py` 3 |
| ③ | 工具路由（04.4） | `tools/routing.py`：TF-IDF+余弦，自适应（≤12 全给）+ 命中不足回退全量 + `metadata` 可观测 | `test_tool_routing.py` 9 |
| ④ | 请求级缓存（08.2） | `response_cache.py`：会话内隔离、只缓存可信 FINISH、`force_full_rerun` 绕过、`cache_hit` 透出 | `test_response_cache.py` 10 |
| ⑤ | RAG 评估（03.9） | `eval/rag_eval.py` + `rag_golden.py`：hit@k/recall@k/MRR/faithfulness 下界 + CLI | `test_rag_eval.py` 10 |

**① 的三个真问题**：同分 tie-break 不能靠 sort 稳定性（后端顺序不同 → 不可复现）；
`_PATH` 是模块常量 → 改配置项 `long_term_path`（4 处调用点同步）；
一条用例**碰巧通过**（旧实现的整句子串匹配刚好排除干扰项）→ 改成判别式用例。

**③ 的两个实测教训**：中文单字造假命中（"业务知识"因「识别」的「识」路由到 `image_analyze`）→ 只用 2-gram；
**工具描述原本是英文的**、中文查询零命中 → 加 `_ALIASES` 中文别名表。路由实测 7 类中文问句全命中、无效查询正确回退全量。

**④ 与 DEGRADE/01 的交互（重要发现）**：初版把**降级（mock 兜底）**的 FINISH 也缓存了——
等于"LLM 恢复了也照旧返回模板报告，TTL 内一直骗人"。是在跑全量回归时被
`test_fallback_attribution_is_per_run` 抓出来的（第二次同问命中缓存 → 没有降级事件）。
已改为**降级结果不入缓存**并加用例固定。

**⑤**：评估用**独立知识库** `data/eval_rag.db`（用应用库会被历史数据污染，实测混进过 `guard_test` 文档）。
实测 **hit@4 = recall@4 = MRR = 1.0**、avg faithfulness 0.747；含伪造数字的那条 0.30（最低）——
指标确实能分辨，不是恒真。忠实度是**确定性下界**，真实语义判读标 `[待真实验证]`。

**事故与恢复（如实记录）**：我用 heredoc 脚本改 `long_term.py` 时，切片边界算错导致
`s.replace("", new_text)` 把文件膨胀到 **22 万行**。**已完全恢复**：因为该操作只插入不删除，
抽掉注入块即可还原原文——用"两次标记之间的片段"精确定位注入串，`"".join(content.split(injected))` 还原，
校验语法 + 符号齐全（164 行）后写回，并确认 4 处调用点已同步。
**教训第五次生效**：含引号/切片/转义的源码改动一律用 Edit 工具，不要走 heredoc 脚本。

**门禁**：全量 **593 passed, 5 skipped, 0 failed**；eval mock 基线不变
（FINISH 1.0 / 断言 1.0 / 溯源 13/13 / avg 工具 4.75）。

## 三项缺口补齐（2026-09-12）· 真实基线 / 用户级鉴权 / 并发规模实测

### ① 真实基线：第一次跑出来的结论是「这次不算数」——但价值极高

**第一次运行（已归档 `eval-real-baseline-INVALID.md`，不要引用其数字）**：
7 条 `requires_real` → **6 条 CLARIFY、1 条 FINISH、断言通过 0.0**、平均 338s/用例、69k tokens。
`FINISH 率 0.143`、`工具成功率 0.0`。

**根因（配置 + 一个真 bug）**：日志里 `LLM call failed (planner); falling back to mock:
Error code: 404 - 'This model is unavailable for free'` → **降级为 mock** → 连续失败后
`circuit OPEN: short-circuiting` → **之后所有调用完全不发网络请求**。逐个 1-token 探测全链：

| 模型 | 状态 |
|---|---|
| `google/gemma-4-31b-it:free`（当时的主模型） | ❌ SSL EOF |
| `google/gemma-4-26b-a4b-it:free` | ❌ 429 上游共享池 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | ✅ **唯一存活** |
| `google/gemma-4-12b-it:free` | ❌ **400 not a valid model ID**（ID 写错） |
| `meta-llama/llama-3.3-70b-instruct:free` | ❌ **404 已下架** |

→ 5 个里 4 个不可用，其中 **2 个是纯配置错误**。这解释了单次调用 146–181s：
大部分时间花在**注定失败的尝试**上，不只是模型慢。

**修复**
1. `.env` 链改为唯一存活模型（旧值与被移除原因写进注释；备份 `.env.bak-*`）。
2. **代码真 bug**：熔断器把**任何**异常都计为失败——包括"模型 ID 写错"这种配置错误。
   于是一个写错的 slug 在 5 次后把整条服务静默降级成 mock。
   修法：`CircuitBreaker.__call__` 增可选 `count_failure` 谓词；
   `router._config_error()`（400/404/422）→ **不计入熔断**（换下一个候选才是对的），
   服务级错误（5xx/超时）**照常熔断**。`tests/test_circuit_breaker.py` 增正反用例。
3. 已用修好的链**重跑**（后台进行中，本次 0 次降级）。

### ② AUTH/01 用户级鉴权与数据权限（18 passed）
**SDD**：`docs/specs/AUTH/01-...md`。此前是**零 API 鉴权**：任何能访问端口的人都能查全部数据、
下载任意会话导出、看任意人问过什么。已有权限只到**工具级**。

- **身份**：`X-API-Key` → `Principal{user_id, tenant, allowed_tables, denied_columns, row_filters, quota_per_min}`；
  **默认关**（`AUTH_ENABLED=false`）→ 匿名全权限，既有 600+ 用例零影响；开了必须有 key，
  **开了但没配 key → 503**（配置错误要吵，不静默放开）。
- **中间件一次收口**（不是依赖注入）：`Principal` 要在**工具层**可见（数据权限在 `sql_tool` 判），
  依赖注入只给它自己的路由函数；中间件设 contextvar，anyio 复制上下文 → 工具层可读。
- **三层数据权限全部确定性、执行前拦下**：表级白名单（含 JOIN 表）、列级黑名单
  （SQL 引用 → 拒绝；结果含 → 整列剔除）、行级 `row_filters` → **追加 WHERE 谓词**
  （不是包子查询，避免破坏别名语义；且插在 GROUP BY 之前）。
  过滤片段**由配置方提供但仍过白名单**（禁 `;`/注释/子查询）——否则等于把注入写进配置。
- **会话归属**：越权读别人的 `export/trace/artifacts` → 403（同租户也不默认开放）。
- **审计**：ALLOW 与 DENY **都记** `data/audit/auth.jsonl`（只记拒绝无法复盘）。
- 401 不回显"key 是否存在"（防枚举）；超配额 429。

### ③ CONC/01 并发与规模实测（可复跑：`scripts/bench_scale.py` / `bench_concurrency.py`）
基线写入 `docs/progress/perf-baseline.md`。核心数字：
- **规模（9 列）**：全表聚合 94ms / 分组 723ms / 明细 LIMIT 5.4ms / JOIN 377ms @100 万行；
  **`dataset_profile` 2043ms（20 万行 401ms → ×5.1，确认线性）**；语义采集 3ms（有界，与规模无关）。
  → 外推 **100 列宽表 100 万行 ≈ 22s**、1 亿行不可用。**改法优先级：只画像被引用列 > 采样 > 近似去重。**
- **并发**：8 并发 × 40 请求 → **7.21s / 5.54 req/s / 成功率 1.00**，P50 77.7ms、P95 3773ms、缓存命中 60%。
  延迟双峰（命中 78ms vs 真跑 3.8s，**48 倍差**）；工具并行 workers=4 vs 1：wall −10%、P95 −20%
  （mock 下耗时由 LLM 阶段主导，工具不是瓶颈——**真实慢查询下收益未实测，不声称**）。
- 自动化门禁：`tests/test_concurrency_smoke.py`（8 线程无死锁、无会话串扰、缓存一致）2 passed。

**门禁**：全量 **613 passed / 5 skipped / 0 failed**。

## 企业化收口 · 六领域补齐（#1~#6 · 2026-09-12）

**动因**：按「前端 / 后端 / 测试 / E2E」四维度重估实现度，找出六处"企业面试会问、项目答不实"的缺口。
上一节已记录 **#1 用户级鉴权** 与 **#2 并发与规模**；本节补齐 **#3~#6**，并把六项作为一个交付整体收口。

| # | 缺口 | 交付 | 用例 |
|---|---|---|---|
| #1 用户级鉴权 + 数据权限 | 后端三层数据权限已有；**补 RBAC**：`specs.ROLE_PERMISSIONS` + `roles_to_permissions`、`auth.effective_permissions`、`execute_tool` 执行前按角色门禁、生产未开鉴权启动告警 | `test_rbac.py`(11) |
| #2 并发与规模 | 实测 100 万行：聚合 154ms / 分组 1046ms / **profile 2379ms（随列数线性）** / 语义 4.6ms；8×24 并发 9.35s · 2.57 req/s · 成功率 1.0 · P95 5.26s | `test_concurrency_smoke.py`(2) + `bench_*.py` |
| #3 可观测性 | `observability/metrics.py`（**零依赖** Prometheus 文本格式 + OTel 钩子 + 告警事件）、`/metrics` 端点、HTTP 计时中间件、工具层与 LLM 网关埋点 | `test_metrics.py`(6) |
| #4 CI + 镜像 | `.github/workflows/ci.yml`：offline pytest（mock/无 key）+ eval mock + 镜像构建 + 容器冒烟（health/metrics）+ 前端 build | — |
| #5 LLM-judge | `eval/judge.py`：离线 rubric + 真 LLM 钩子（**降级安全**），接 `golden.judge_min_score` 与 `runner` 指标 | `test_judge.py`(6) |
| #6 前端协作 + E2E | `lib/auth.ts`（X-API-Key 注入 + 401/503→AuthError）、`AuthGate.tsx` 登录弹窗（**带被拦问题的暂存重试**）、`ShareBar.tsx` 分享/评论/权限骨架、`playwright.config.ts` + `e2e/export.spec.ts` | `test_frontend_collab.py`(7) |

**本轮补的一环（让 #1/#6 的前端接线离线可守）**：上面两项的前端代码此前**只有 `tsc -b` 通过、没有任何测试**——
而 `tsc` 只保证"类型对"，不保证"线接上了"。新增 `tests/test_frontend_collab.py` **7 条源码级契约**
（沿用 CLARIFY/01 的范式），每条都对应一个用户可见后果：鉴权头真被注入 / 401 真转 AuthError /
登录后真重试被拦的问题 / ShareBar 真被渲染 / Playwright 配置与依赖真存在。

**做了变异校验（防假绿）**：临时删掉 `App.tsx` catch 块里的 `pendingRef.current = {...}`，
对应用例立刻变红（"被 401 拦下的问题必须暂存，否则用户要重问"），还原后 7 passed
——确认这些断言真的会咬，不是形式检查。

**验证（本轮实跑）**
- CI 口径离线全量：**651 passed / 2 skipped / 0 failed / 378.86s**
  （`MOCK_LLM=true` + 忽略 `test_agent_real`/`pg_live`/`redis_live`/`milvus_live`，与 `.github/workflows/ci.yml` 同口径；
  含本轮新增 7 条 + #1/#3/#5 的 11/6/6 条）。
- 前端：`npm run build`（`tsc -b` + vite build）**EXIT=0**，产出 `dist/assets/index-CwyZUXHp.js`。

**三个环境阻塞（如实记录，不声称通过）**
1. **OpenRouter 余额耗尽（402）**：真实调用被拒
   （`You requested up to 2048 tokens, but can only afford 1211`）→ `test_agent_real.py` 14 条中
   **6 条（需 LLM 的）失败**；离线口径本就整文件排除。**非代码问题**，与 D18 同源。
2. **npm registry 不可达**：`registry.npmmirror.com` → `ECONNREFUSED 127.0.0.1:7890`（本机代理未启动）
   → `@playwright/test` **装不上** → **E2E 用例已交付但从未实跑**。
3. **Docker 镜像源不可用**（沿用 D33）：镜像未构建、体积未实测。

**真实基线重跑（11:25 · 链路已通，但结论仍不算数）**
模型链修好后重跑 7 条 `requires_real`：**本次 0 次降级**（对比第一次 5 个 stage 全降级），
`context → planner → analyst → reflection` 真实跑完，但结果仍不达标：

| 结果 | 条数 | 说明 |
|---|---|---|
| FINISH | 2 | `r_caliber_period_mismatch`（6 工具）、`r_join_amplification_guard`（5 工具） |
| CLARIFY | 5 | 仅 1 次 LLM 调用即反问 |

`FINISH 率 0.286` · `断言通过率 0.0` · 平均 109s/用例 · 155k tokens。

**观察（根因待验，先不据此改代码）**：判 CLARIFY 的 5 条——"转化率 6% 环比 +7% 显著吗" /
"8 月 GMV 同比 -12% 帮我找主因" / "渠道切换是不是营收下降的原因" / "逐个比较 8 个渠道" /
"总转化率 6%→7% 说明优化成功吗"——都是**"给定数字做判断"**型问题，样例库里没有对应数据；
两条 FINISH 的恰好是**能落到库里的**（join 放大 / 口径期间）。据此推测：真实模型面对
"无从查证的数字"选择反问，这与 CLARIFY/01「模糊请求应当反问」的设计**一致**，
却与这些 golden「期望 FINISH + 标注问题」**冲突**。
**这是 golden 语义与澄清策略的冲突，不是 bug——需要定夺（见待办①），未定前不动 golden 与提示词。**

**待办**
1. **定夺**：这 5 条 golden 该「接受 CLARIFY（反问也算合格回应）」还是
   「收紧 `context.md` 让模型带假设推进」——两者都合理，取决于产品取向（反问优先 vs 先给带假设的答案）。
2. E2E 实跑：代理恢复或换网后 `cd web && npm i && npx playwright install && npm run test:e2e`。
3. 镜像构建与体积实测（换可连镜像源的机器）。
4. 真实 LLM 各项（`pending-real.md`）待余额恢复。


---

## Live 验证突破（真实 MySQL / 真实 Milvus / 浏览器 E2E · 2026-09-12）

**目标**：把上一轮"配好即跑"的三项（真库 live / Playwright / 镜像）从"未验证"推到**真实跑过**。
详见 `docs/progress/live-validation.md`。

### 环境探针的结论（决定了哪些能真跑）
- ✅ LLM 端点可用（`.env` 的 OpenRouter）→ 真 judge 可跑
- ✅ 本机有 MySQL 8.0.26 二进制 → 可**自建真实实例**（`--initialize-insecure`）
- ✅ `milvus-lite` 在 Windows 可用 → 无 Docker 也能跑**真实向量引擎**
- ✅ sentence-transformers + torch(CPU) 已装 → 真实 384 维嵌入
- ❌ 无 Docker / 三个镜像源全不可用 → 镜像 <2GB 本机无法实测（交付 CI 硬断言）
- ❌ 无 PG/Redis 服务端 → 仅脚手架

### 真跑结果
| 项 | 结果 |
|---|---|
| 真实 MySQL 8.0.26 live | **13 passed**（命名源/只读/文件原语/数据权限/跨方言） |
| 真实 Milvus（Lite）live | **6 passed**（写入/ANN 检索/多租户隔离/回退） |
| Playwright 浏览器 E2E | **3 passed**（含真请求导出 zip 断言 200 + PK） |
| 真 LLM judge 区分度 | excellent 0.35 / medium 0.30 / wrong 0.10 / poor 0.00（抓到算术矛盾） |

### 三个「真跑才暴露出来」的真实缺陷（都已修 + 回归测试）
1. **流式路径不落 checkpoint → 导出必 404**（`graph.py`）
   前端唯一入口是 SSE，而 `stream_analysis` 从不 `checkpoint_save`；`run_analysis` 有。
   → 流式会话的 export/trace/artifacts/resume 全 404，**而界面照常渲染导出按钮**。
   修：生成器包一层，`finally` 对**终端态**落盘（正常/异常/断连都覆盖），非终端态不落。
   回归：`tests/test_stream_checkpoint.py`（6）。
2. **SQL 只读守卫存在多处真实绕过**（`sql_tool.py`）
   实测 `INTO OUTFILE`/`LOAD_FILE`/`SLEEP`/`CALL`/`HANDLER`/`LOCK TABLES`/`SET GLOBAL`/
   `/*!可执行注释*/` 全部放行；`INTO/*x*/OUTFILE` 还能拆分绕过。
   修：先**去注释+折叠空白**再匹配；新增文件/副作用原语黑名单；语句首匹配避免误杀列名；
   文件/DoS 类不受 `sql_readonly` 开关影响。
   回归：`tests/test_sql_readonly_guard.py`（33：22 攻击全拦 / 9 合法零误杀）。
3. **Milvus 后端多租户不可用**（`knowledge_tool.py`）
   工具层一直传 `tenant=`，SQLite 后端支持、Milvus 后端不支持 → 配 Milvus 的部署
   `knowledge_search` **必然 TypeError**；且 collection 无租户字段 = 无隔离。
   修：schema 加 tenant 字段（老 collection 自动兼容）、search 用 filter 表达式过滤并转义。
   回归：`tests/test_milvus_live.py::test_milvus_tenant_isolation_end_to_end`。

### 顺带交付
- `MILVUS_LITE_PATH` 配置（独立于 pymilvus 自身的 `MILVUS_URI`——后者塞文件路径会让
  `import pymilvus` 直接崩，已记入文档）；
- `.dockerignore`（构建上下文砍掉 107M data + 333M references + 357M node_modules）；
- CI 扩到 4 个 job：`test` → `e2e`（Playwright 真跑）→ `live`（MySQL 服务容器 + Milvus Lite）
  → `image`（构建 + **<2GB 硬断言** + 容器冒烟）；
- `requirements.txt` 补 `pymysql`（MySQL 方言驱动）。

### 仍未真验（诚实记录）
镜像 <2GB（无 Docker，已给 CI 断言）、PG/Redis live（无服务端）、Milvus **服务端** live（无 Docker）。

## 前端徽标落地 + Milvus 静默降级修复 + 两处事实校正（2026-09-12 续）

上一节由并行会话完成（MySQL/Milvus/E2E 真跑 + 3 个真缺陷）。本节是**同一时段的另一条线**，
以及对其结论的**两处事实校正**。

### 一、UI 消费 SSE 的 `mode/iteration`（后端早发了，前端一直没人看）

**背景**：后端从 E3/E4 起就在每帧下发 `iteration`（增量类型）与 `quality_issues`（质量门禁），
但前端 `AgentEvent` **没有这两个字段**——界面一切正常，信息静默丢失。
这类"字段发了但没人消费"比崩溃更隐蔽，故按前端契约测试的老办法钉住。

1. `lib/api.ts`：新增 `IterationInfo` / `QualityIssue` 接口与 `AgentEvent.iteration/quality_issues`，
   `iterationLabel(kind)` 覆盖 E3 的五类（下钻/改期/换粒度/筛选/增量）。
2. 新组件 `components/RunBadges.tsx`：增量徽标（含"基于上一结果，未重新取数"提示）+
   质量门禁横幅（**BLOCK 排最前、rose 色**，因为按 S1 的设计 BLOCK 不置请求为 FAILED，只靠披露可见）+
   降级提示；无内容时返回 `null`（不当噪音）。
3. `ChatMessage.tsx` 渲染；`tests/test_frontend_collab.py` 扩到 **11 条**，其中一条是**跨栈契约**：
   前端读的键名必须与 `chat.py` 下发的键名一致——任一侧改名而另一侧没跟上，只能靠它抓到。

**浏览器级验证（`web/e2e/badges.spec.ts`，2 passed）**：真实两轮会话，
第二轮下钻显示「增量 · 下钻」且首轮全链**不显示**徽标。

> **测试自己踩的坑（值得记）**：第一版 `sendQuery` 用 `/发送|分析|submit|Send/i` 找按钮，
> 结果**先命中了欢迎页的示例问句**「分析最近半年的月度销售趋势…」——测试看起来在跑，
> 实际发的是另一句话；那句的上一结果没有 region 列，于是第二轮被 E3/03 守卫判为不可增量、
> **正确地回退了全链**，徽标当然不出现。**是测试错了，不是产品错了**。
> 改用精确名「发送」+ 首轮先问出带 region 的结果集后即绿。
> 顺带印证：E3/03 的守卫是数据驱动的，且在没有该列时**不会假装增量**。

### 二、Milvus 静默降级修复（`get_client` 的裸 except）

并行会话已修掉 Milvus Lite 路径的 `.db` 后缀问题（本仓库示例原本少了后缀，
而 pymilvus 3.x 硬要求本地 URI 以 `.db` 结尾）。**但 `get_client()` 里那个
`except Exception: return None` 还在**：它把"URI 非法/服务连不上"与"压根没配 Milvus"
（回退 SQLite 属预期）**混成同一个结果**，调用方无从区分——正是铁律 3 针对的模式。

**修**：连接失败时 `logger.warning` 打出 **uri + 异常类型 + 原因**；未配置时保持静默
（那是预期回退，不该吵）。`tests/test_milvus_client_visibility.py` **3 passed**
（未配置不告警 / URI 非法要留痕 / 服务连不上要留痕）。

**Milvus Lite 真跑（用文档里那个**没有** `.db` 的写法）**：
`MILVUS_LITE_PATH=./data/milvus_lite` → `test_milvus_live.py` **6 passed**
（含 `test_milvus_backend_is_real_not_fallback` 断言 `dim==384` 的真实嵌入 + 多租户隔离）
——证明自动补后缀在真实路径上成立，照文档配置即可用上真向量库。

### 三、两处事实校正（与上一节结论不一致，以本节为准）

1. **PG live 已实测通过，不是"无服务端"**：本机 PostgreSQL 在 **5432** 正常监听，
   `tests/test_pg_live.py` **4 passed**（PG 后端长期记忆：写入落 PG 而非 JSONL / 检索命中 /
   敏感键不落盘 / 无 DSN 时 JSONL 回退）。Redis（6379）确实无服务端，跳过属实。
2. **本机**有** Docker，别把锅记在 daemon 上**：`docker version` → **29.5.3**，8 个镜像在库
   （`postgres:16-alpine` / `mysql:8.0` / `milvusdb/milvus:v2.4.15` / `redis:7-alpine` …），
   `test_python_docker_live.py` **4 passed**（沙箱真隔离：断网 / 只读 rootfs / 产物回传）。
   镜像构建的**唯一**阻塞是**镜像源不可达**：`docker pull hello-world` 60s 超时，
   三个 mirror（daocloud / nju / azk8s）无一可用。**换镜像源即可，不必换机器。**

### 四、本机 live 实测汇总（本节实跑）

| 套件 | 结果 | 说明 |
|---|---|---|
| `test_pg_live.py` | **4 passed** | 真实 PostgreSQL 长期记忆 |
| `test_python_docker_live.py` | **4 passed** | 真实 Docker 沙箱隔离 |
| `test_milvus_live.py`（`MILVUS_LITE_PATH`） | **6 passed** | 真实 Milvus Lite + 384 维嵌入 |
| `web/e2e/*.spec.ts`（Playwright + chromium） | **5 passed** | 导出真请求 zip + 增量徽标 |
| `test_redis_live.py` | 2 skipped | 本机无 Redis 服务端（属实） |

**仍未真验**：镜像 <2GB（镜像源不可达）、Milvus **服务端** live（本机 19530 未起）、
Redis live、真实 LLM 各项（余额 402）。

## 两个"没起来"的中间件：真 Redis + 真 Milvus 服务端（2026-09-12 续 2）

### 起因
用户指出 Milvus 服务端（19530 未监听）与 Redis 服务端（6379 未监听）这两项必须先解决。
**上一条里那句「`test_redis_live.py` 2 skipped（属实）」是对的——Redis 从未通过**；
而审计报告 Round 4 把它写成「Redis 短期记忆实连 … 真实落 Redis」并把 2 条 skip 计进
「live 9 passed」，那处是**虚报**（已在本节更正）。

### 关键发现：环境早就备好了，只是容器停着

`docker ps -a` → 2 天前建好的 6 个容器**全在**，全部 `Exited`：
`agent-redis` / `agent-etcd` / `agent-minio` / `agent-milvus` / `agent-mysql`。
`docker start` 即可，**零拉取**（本机 8 个镜像齐全；受镜像源阻塞的只有"拉新镜像"）。

→ 由此坐实：此前所有「无服务端」的结论，**是探针方法错了**——只查了端口/二进制，
没查"容器是否已经存在"。这条教训比这两个服务本身更值钱。

### 结果（全部真跑）

**Redis**：`docker start agent-redis` → `redis-cli ping` = `PONG`

- `tests/test_redis_live.py` **2 passed**（**历史首次通过**，此前恒 skip）
- **端到端印证**：对运行中的服务发一次分析 → Redis 出现 `da:st:redis_e2e_proof`（hash），
  字段 `history / last_dataset / last_analysis / clarify_rounds / semantics:*`
  —— 证明**应用真的在写 Redis**，不是只有测试在连。

**Milvus 服务端**：`docker start agent-etcd agent-minio agent-milvus`
→ 19530 就绪（`/healthz` = 200，约 20s）

- `tests/test_milvus_live.py` **5 passed, 1 skipped**（skip 的是 Lite 专用断言，正确）
- 这是**Milvus 服务端**路径首次被真跑（此前全部验证都走的 Milvus Lite）

合跑：`redis_live + milvus_live + pg_live + short_term_ttl` = **16 passed / 1 skipped**。

### 顺带修掉的三个真缺陷（都是"真跑才暴露"）

**① Redis 会话键永不过期（泄漏）**
`HKEYS da:st:<sid>` 字段齐全，但 **`TTL` = -1**——`short_term` 全程**没调用过 `EXPIRE`**，
进程内 dict 也不淘汰。一次生产跑下来，每个会话在 Redis 里留一个**永久 hash，只增不减**。
（同目录参考项目的 Java 版**有** TTL：`agent.memory.short-term.ttl-minutes` 默认 60min。）
修：新增 `SHORT_TERM_TTL_S`（默认 **86400 = 24h**，比 Java 长——本项目的会话可能停在
`CLARIFY` 等用户回答、跨天回来要能续跑）+ **滑动续期**（`put` / `get_all` 各续一次）；
`<=0` 显式关闭（仅调试）。`tests/test_short_term_ttl.py` **5 passed**
（含一条真 Redis 断言 `TTL > 0`，无 Redis 则 skip）。

**② 内存兜底是"只写不读"的**
`put` 失败会降级写内存，但 `get` 在 Redis 返回空时**直接 return default**——
那份兜底**永远读不回来**（读写路径不对称）。修：Redis 无此键时回落到内存。
由 `test_fallback_to_memory_still_records_nothing` 钉住。

**③ `docker-compose.yml` 根本起不来（tag 对不上）**
compose 钉的是 `etcd:v3.5.14` / `minio:RELEASE.2024-05-28` / `milvus:v2.4.6`，
**本机一个都没有** → `docker compose up` 必然去拉、必然失败（这正是"起不来"的另一半原因）。
改为本机实有、且**本轮已被真跑验证过**的 `v3.5.5` / `minio:latest` / `v2.4.15`；另修：
MinIO 的 `MINIO_ACCESS_KEY/SECRET_KEY` 已废弃（启动日志告警）→ 改 `MINIO_ROOT_*`；
补 redis/etcd/milvus **健康检查**，`app` 改 `condition: service_healthy` 门控
（Milvus 首启要建元数据，官方文档明确提示"可能需数十秒"，抢跑会**静默回退 SQLite**）。
验证：`docker compose config` 通过 + 5 个镜像**全部本地命中**。

> **如实说明**：我没有真跑 `docker compose up`——它与正在运行的 `agent-*` 容器**同占 19530/6379**。
> 故"compose 能离线起"的结论止于**配置合法 + 镜像本地齐备**，端到端起栈未实测。

### 文档
- `docs/部署上线.md` 新增 **§4.9 起中间件（可离线启动）**（含"启动方式二选一，别混用"、
  Milvus 首启等待、MinIO 变量更名）；自检清单加 `SHORT_TERM_TTL_S` 复核项。
- `.env.example` 增 `SHORT_TERM_TTL_S`。

### 一处必须说清的事故（不是代码问题）
本轮全量回归先出现 **3 条失败**（`test_clarify_loop` / `test_semantics` / `test_tool_routing`，
`NameError` + mock planner 产出空 steps）。**根因是并行会话在我的回归窗口内改了
`state.py`(14:06) / `graph.py`(14:08) / `nodes.py`(14:09)**，进程读到的是**半保存的文件**；
这 3 条单独重跑**全部通过**。稳定后重跑全量：**762 passed / 19 skipped / 0 failed**。
**教训：多会话并行时，回归结论必须连同「窗口内是否有他人改动」一起看**，
否则会把别人的半成品记成自己的回归。

**仍未真验**：镜像 <2GB（镜像源不可达）、Redis **高可用/持久化策略**（现为单实例 + AOF）、
真实 LLM 各项（余额）、CLARIFY vs golden（待人工抽查定夺）。
