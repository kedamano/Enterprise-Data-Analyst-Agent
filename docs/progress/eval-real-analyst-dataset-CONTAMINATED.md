# ⚠️ 本基线已被污染 —— 请勿引用其中数字

> **为什么不可信**：本报告跑于 **2026-09-13 13:30**，当时 `build_executor_params` 的
> 退化占位符是 `SELECT 1` —— 它是**合法只读 SQL**，会照常执行、返回 1 行、把该步记成
> `SUCCESS`。于是**每一步 SQL 都是 `SELECT 1` 的用例，在假数据上"通过"了断言**。
>
> 具体地：`r_caliber_period_mismatch` 与 `r_decompose_before_attribution` 两条 ✅
> **实为假绿**（checkpoint 里 step_1/2/4/5 全是 `SELECT 1`、`rows=1`）。
> 唯一指出问题的是 Reflection（"所有数据查询步骤均返回占位数据"）。
>
> 根因已修（占位符改空串 → 该步响亮 FAILED），复跑见
> `eval-real-analyst-dataset.md`。本文件**保留作证据**：它记录了
> "离线测试永远发现不了"的一类假绿是怎么产生的。
>
> 相关：`docs/progress/pending-real.md` §E.1 / §F、`metrics.md`「dataset_profile 规模修复」上方说明。

---

# Eval 报告

- 模式：real　用例数：7　时间：2026-09-13 13:30:19

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 0.571 |
| 断言通过率 | 0.429 |
| 平均工具调用 | 3.86 |
| 工具成功率 | 1.0 |
| 平均 LLM 调用 | 6.71 |
| Reflection PASS 率 | 0.0 |
| LLM-judge 平均分 | 0.571 |
| LLM-judge 方法 | rubric-offline |
| 平均报告长度 | 1035.0 |
| 平均耗时(s) | 160.589 |
| prompt tokens | 372271 |
| completion tokens | 45719 |
| 总 tokens | 417990 |
~~| 成本 USD | 0.0 |~~
| 溯源覆盖率 | None |
| 溯源 claims | 0/0 |
| 跳过（需真实模型） | 0 |
| **降级剔除（非真实模型产出）** | 0 |
| 实际计分用例数 | 7 |

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| r_caliber_period_mismatch | FINISH | 7 | 11 | 2 | ReflectionDecision.REPLAN | ✅ | [] |
| r_ratio_denominator | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_decompose_before_attribution | FINISH | 8 | 11 | 2 | ReflectionDecision.REPLAN | ✅ | [] |
| r_join_amplification_guard | FINISH | 6 | 11 | 0 | ReflectionDecision.REPLAN | ✅ | [] |
| r_causal_overreach | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_multiple_comparison | FINISH | 6 | 11 | 0 | ReflectionDecision.REPLAN | ❌ | ['缺少质量门禁 code: multi_comparison_unadjust |
| r_simpson_check | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |