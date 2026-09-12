# E5/01 统计严谨 — 规格 v1.0

> 动因：基准 `docs/测试用例.md` 里 L4 最难的一档几乎全是**统计判断**：
> "总转化率 6% 涨 7%，说明优化成功吗？"（辛普森）、"为什么 A/B 结果每天波动？"（功效/方差）、
> "哪个模型更好？"（检验）、"NPS 和续费率有关系吗？"（相关≠因果）。Rubric 权重里 `统计 475`。
>
> 现状：`stats_notes` 根本没有这个字段；`system.md` 有统计四层（FACT/CORRELATION/HYPOTHESIS/CAUSAL）
> 但**没有任何机器可查的声明**。E5 计划里只写了"要求显著性/样本声明"，本规格把它变成**门禁**。

## 1. 契约

### 1.1 `AnalysisResult.stats_notes: list[StatsNote]`
```python
class StatsNote(BaseModel):
    claim: str = ""            # 对应哪条结论
    method: str = ""           # 检验/方法（t 检验、卡方、比例检验、回归…）
    n: int|None = None         # 样本量（分子/分母或实验单元数）
    significant: bool|None = None
    note: str = ""             # 口径与局限（如"未达最小样本量，仅供参考"）
```
- `_coerce`：真实模型可能写成字符串或 `[{...}]`，统一容错（与既有模型一致）。
- **不要求模型真跑 scipy**：门禁只检查"该声明有没有声明"，不检查"算得对不对"
  （后者依赖真实模型，标 `[待真实验证]`）。

### 1.2 门禁规则（`app/core/agents/data_analyst/rigor.py`，复用 `GateIssue`）
| code | 判据（AND） | 级别 |
|---|---|---|
| `untested_comparison` | 结论含**两期数值对比**（环比/同比/提升/下降/增长 + 百分比且 evidence 有数值）且无对应 `stats_notes` | ANNOTATE |
| `significance_without_n` | `stats_notes.significant` 非空但 `n is None` | ANNOTATE |
| `hypothesis_strong_without_n` | `hypotheses[].result in (SUPPORTED, PARTIALLY_SUPPORTED)` 且无 `n` | ANNOTATE |
| `multi_comparison_unadjusted` | 同一报告 ≥5 组对比且无多重比较说明 | ANNOTATE |
| `causal_overreach` | finding 含"导致/造成/因为/由于/caused by" 而对应 hypothesis 非 SUPPORTED | ANNOTATE |

**全部 ANNOTATE，无 BLOCK**：统计判断的多寡取决于问题类型，硬拦会制造报警疲劳。
唯一例外见 E5/02 的 `dq_override_silent`。

### 1.3 过度报警的三道闸
1. **每个 code + metric 每次运行最多报一次**（去重）。
2. **必须"有事实且有主张"**：描述性计数、"多少/排行"类请求不触发（词表不命中即可）。
3. 单一指标孤立陈述、无对比的结论 → 不触发。

### 1.4 边界
- **不得依赖 scipy**：沙箱只有 pandas/numpy/matplotlib 且只挂单个 CSV；
  门禁只做"该做未做"的提示，不代跑检验。
- `stats_notes` 由 LLM 产出；真实性/正确性 `[待真实验证]`。
- 与 E4/04 的关系：两者共用 `GateIssue` 与 `apply_gate`（只收紧），披露同走 `quality_notes`。

## 2. TDD
| 用例 | 断言 |
|---|---|
| 有环比无声明 | findings 含"环比提升 12%" + evidence 数值、无 stats_notes → `untested_comparison` |
| 有声明则不报 | 同上但带 `stats_notes` → 无该 issue |
| 显著性缺样本量 | `significant=True, n=None` → `significance_without_n` |
| 强结论假设缺 n | hypothesis SUPPORTED 无 n → `hypothesis_strong_without_n` |
| 多重比较 | ≥5 组对比无说明 → `multi_comparison_unadjusted` |
| 因果越界 | finding"由于 X 导致 Y" + hypothesis UNKNOWN → `causal_overreach` |
| 描述性不报警 | "华东营收最高" → 无任何 stats issue |
| 去重 | 同一 code 多条结论只报一次 |
