# E4/04 质量门禁：把 profile 基元变成决策 — 规格 v1.0

> 动因：E4/01 让 `dataset_profile` 能算出**主键唯一性 / join 放大 / 粒度 / 日期连续性**，
> 但自 D20 起这些字段**除 `profile_tool.py` 外零引用**——每次分析都算，然后躺在
> `tool_results` 里，没有门禁、也不进报告。分析师最怕的"拿放大后的结果求和"，
> 工具明明知道，却没人拦。

## 1. 机制（仿 `sources.py` 的纯函数"违规清单"范式，不花 LLM）
新建 `app/core/agents/data_analyst/gate.py`：

```python
Severity = Literal["BLOCK", "REPLAN", "ANNOTATE"]
class GateIssue(BaseModel):
    code: str; severity: Severity; detail: str; metric: str|None = None; fixable: bool = False

def profile_gate(tool_results, analysis) -> list[GateIssue]
def join_amplification_facts(tool_results) -> list[dict]
def apply_gate(issues, reflection) -> tuple[ReflectionDecision, list[str]]  # 只收紧，不放松
```

- 挂接点：`nodes.run_reflection` 出口 —— LLM 的 `FAIL` 保持；`PASS` 可被收紧为 `REPLAN`/`ANNOTATE`；
  `REPLAN` **绝不**被放松。`ANNOTATE` 不改变决策，只追加披露。
- 所有 issue 落 `state.metadata["gate_issues"]`，披露文本落 `AnalysisResult.quality_notes`。

### 1.1 三级语义（为什么 BLOCK 不直接 FAIL）
| 级别 | 含义 | 决策效果 |
|---|---|---|
| `ANNOTATE` | 结论可用，但必须披露（稀疏日期、口径未声明） | 不改决策，追加 `quality_notes` |
| `REPLAN` | 缺证据，**能通过再查一次补齐**（如补主键/换过滤条件） | 抬到 REPLAN |
| `BLOCK` | 结论**按当前数据必然错**（如来自笛卡尔放大的求和） | 抬到 REPLAN（把门禁缺口喂给 planner 去修）；重试额度用尽时节点判 `FAILED` |

- BLOCK 不直接 FAIL 的理由：放大后的结论**可补救**——把"步骤 s3 结果 15600 行 = 基线 3120 行的 5 倍，需修正 join 条件"
  交给 planner，通常一次就能修对；直接失败等于放弃这次机会。
- BLOCK 又**不等于** REPLAN：额度用尽时 REPLAN 会 `status="REPORT"` best-effort 出报告（既有行为），
  而 BLOCK 在 `run_reflection` 里判 `FAILED`。
- 实现：`apply_gate` 返回决策下限；`is_blocked(issues)` → `state.metadata["gate_block"]`；
  `run_reflection` 在 `replan_count >= max_replans` 时据此决定 FAILED 还是 REPORT。

- ⚠ **如实说明（实现后修正）**：`FAILED` 只到节点层。`graph._drive_sync` 对失败态一律
  `return run_reporter(state)`，而 `run_reporter` **无条件** `state.status = "FINISH"`
  （既有设计："失败也给一份 best-effort 报告"）。因此全流程的 `status` 仍是 `FINISH`，
  **不得**声称"BLOCK 会让请求变成 FAILED"。
  BLOCK 对调用方可见的证据是三条，缺一不可：
  ① `error` 含"质量门禁：…（BLOCK 明细）"；② 响应 `quality_issues[]` 里含 `severity=BLOCK`；
  ③ 报告含 `## 数据质量与限制` 披露段。改 `run_reporter` 的终态语义是**独立改动**（会影响所有
  失败路径的既有契约），不在本规格范围。

## 2. 判定规则：**双条件（有事实 AND 有主张）**
> 单条件会把样例库点亮：`fact_sales` 的日期本来就是**稀疏**的（52/358 天），
> 任何"sparse ⇒ 报警"的朴素规则会在第一个 golden 用例就触发。

| code | 判据（全部 AND） | 级别 |
|---|---|---|
| `join_amplified_used` | 某 SQL 步骤被判放大，且该 step_id 出现在数值 evidence 的 `sql_id` 链上 | **BLOCK** |
| `join_amplified_unused` | 同上但结论未引用该步 | ANNOTATE |
| `key_not_unique` | `key_uniqueness.is_unique is False` 且存在该表的聚合主张（findings 含 SUM/AVG/COUNT/合计/汇总/平均/去重计数） | REPLAN（`duplicate_ratio < 0.01` → ANNOTATE） |
| `grain_misread` | `grain == "aggregated"` 且 findings 含"每行/逐条/明细/一条记录" | ANNOTATE |
| `date_sparse_claimed` | `date_continuity.sparse is True` 且 findings/report 含时间连续性主张（趋势/走势/连续/每天/逐日/近 N 天完整） | ANNOTATE（**绝不 REPLAN**：再查还是稀疏） |
| `null_high_on_group` | 某列 `null_ratio >= 0.3`（阈值可配）且该列名出现在 findings 文本里 | ANNOTATE |

**放行（必须显式实现，避免"没画像"被当成"有质量问题"）**：
- profile 未返回对应键（`None`）→ OK；
- `row_count == 0` → OK（空表无质量可言）；
- `grain`/`is_unique` 为 `null`（无从判定）→ OK。

## 3. join 放大的两条检测路径（关键约束）
1. **声明式**（E4/01 已有）：profile 带 `sql` + `base_table` → `join_amplification.factor`。
2. **事后启发式**（本规格新增，因为默认路径的 profile 只画像**表**、拿不到 `join_amplification`）：
   `join_amplification_facts()` 从已有数据推断，**不额外发查询**：
   - 取 `schema_search` 结果里的各表 `row_count`；
   - 对每个成功且 `input.sql` 含 `JOIN` 的 SQL 步骤，取其 `output.row_count`；
   - `baseline = max(所涉及表的 row_count)`；`factor = result_rows / baseline`；
   - `factor >= threshold`（`settings.profile_join_amp_threshold`，默认 1.5）→ 判放大。
   - 事实性校验：维表 join 事实表（N:1）结果行数 ≈ 事实表行数 → factor ≈ 1.0，**不误报**；
     忘写 join 条件（笛卡尔）→ factor = 维表行数 → 报警。
   - 边界：SQL 里解析不出表名 / 无 `schema_search` 行数 → 不判（宁缺勿滥）。

## 4. REPLAN 与"无进展检测"的冲突（最高危）
`graph._drive_sync` 有 `_REPLAN_MAX_STALL=2`：同 `(plan_tools, replan_objectives)` 连发两次 REPLAN → `FAILED`。
门禁发出的 REPLAN 若重复同一目标就会**打破 eval 的 FINISH 1.0 基线**。护栏：
- 门禁 REPLAN 的 objective **必须带唯一缺口描述**（含表名/列名/比例），不重复；
- 同时置 `state.metadata["gate_replan"] = True`，`_drive_sync` 对该轮**不计入停滞**。

## 5. 披露与透出
- `AnalysisResult.quality_notes: list[str]`（默认空，向后兼容）。
- `report_tool.run()` 追加 `## 数据质量与限制` 段（仅在有事可报时出现）；
  **不得改动既有 H1 objective 行**——mock 的 `must_find` 断言依赖它。
- `AnalyzeResponse.quality_issues: list[dict]`；SSE 的 `REFLECT`/`FINISH` 帧带 `quality_issues`。

## 6. 边界（明确不做的）
- **轻模式**（`sql_only`/`quick_answer`/`python_code`）与**迭代轮**（`_try_iteration`→`deliver_iteration`
  直接 FINISH）**不跑门禁** —— 它们本就跳过 Analyst/Reflection/Reporter。规格显式写明，
  否则会写出永远无法满足的断言。
- 门禁只做"事实 → 披露/回退"，**不做数值修正**（不替分析师改数）。
- 不引入新查询：只用已产出的 tool_results。

## 7. TDD
| 用例 | 断言 |
|---|---|
| 放大被用于结论 | 造 `schema_search(3120)` + `freeform` CROSS JOIN 结果 15600 + evidence 指向该步 → `join_amplified_used` / BLOCK |
| 放大但未引用 | 同上但 evidence 不指向 → `join_amplified_unused` / ANNOTATE |
| 正常维表 join 不误报 | 结果行数 == 基线 → 无 issue |
| 无 schema 行数 | 不判（宁缺滥无） |
| 主键不唯一 + 聚合主张 | profile `is_unique=False, duplicate_ratio=0.5` + findings 含"合计" → REPLAN + `fixable` |
| 主键不唯一 + 无聚合主张 | 同上但 findings 无聚合词 → 无 issue |
| 日期稀疏 + 趋势主张 | profile `sparse=True` + findings 含"趋势" → ANNOTATE |
| 日期稀疏但无主张 | → 无 issue（**样例库默认路径必须不触发**） |
| 空表 / 字段缺失 | `row_count=0` 或 profile 无该键 → 无 issue |
| `apply_gate` 只收紧 | LLM=PASS + BLOCK → FAIL；LLM=FAIL + 任何 → 仍 FAIL；LLM=REPLAN + ANNOTATE → 仍 REPLAN |
| 门禁 REPLAN 不触发停滞 | 两轮门禁 REPLAN → **不** FAILED（`gate_replan` 豁免）；且 eval FINISH 基线不变 |
| 报告披露 | 有 issue 时报告出现「数据质量与限制」；无 issue 时**不出现**该段 |
| sync/stream parity | 同一 mock 场景下 `run_analysis` 与 `stream_analysis` 终态一致 |
