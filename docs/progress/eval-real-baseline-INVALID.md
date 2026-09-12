# Eval 报告

- 模式：real　用例数：7　时间：2026-09-12 10:30:43

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 0.143 |
| 断言通过率 | 0.0 |
| 平均工具调用 | 1.14 |
| 工具成功率 | 0.0 |
| 平均 LLM 调用 | 2.43 |
| Reflection PASS 率 | 0.0 |
| 平均报告长度 | 108.9 |
| 平均耗时(s) | 338.028 |
| prompt tokens | 53643 |
| completion tokens | 15576 |
| 总 tokens | 69219 |
| 成本 USD | None |
| 溯源覆盖率 | None |
| 溯源 claims | 0/0 |
| 跳过（需真实模型） | 0 |

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| r_caliber_period_mismatch | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少口径问题 k |
| r_ratio_denominator | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_decompose_before_attribution | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_join_amplification_guard | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少质量门禁 c |
| r_causal_overreach | FINISH | 8 | 11 | 1 | ReflectionDecision.REPLAN | ❌ | ['报告/发现未命中关键片段: 相关'] |
| r_multiple_comparison | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少质量门禁 c |
| r_simpson_check | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |