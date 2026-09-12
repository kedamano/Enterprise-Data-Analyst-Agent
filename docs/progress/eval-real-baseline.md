# Eval 报告

- 模式：real　用例数：7　时间：2026-09-12 11:25:32

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 0.286 |
| 断言通过率 | 0.0 |
| 平均工具调用 | 1.57 |
| 工具成功率 | 0.0 |
| 平均 LLM 调用 | 3.43 |
| Reflection PASS 率 | 0.0 |
| 平均报告长度 | 19.9 |
| 平均耗时(s) | 109.417 |
| prompt tokens | 124368 |
| completion tokens | 30534 |
| 总 tokens | 154902 |
| 成本 USD | None |
| 溯源覆盖率 | None |
| 溯源 claims | 0/0 |
| 跳过（需真实模型） | 0 |

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| r_caliber_period_mismatch | FINISH | 6 | 8 | 0 | ReflectionDecision.FAIL | ❌ | ['缺少口径问题 kind: period_mismatch（实际 []）'] |
| r_ratio_denominator | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_decompose_before_attribution | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_join_amplification_guard | FINISH | 5 | 11 | 0 | ReflectionDecision.REPLAN | ❌ | ['缺少质量门禁 code: join_amplified_used（实际 [] |
| r_causal_overreach | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |
| r_multiple_comparison | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少质量门禁 c |
| r_simpson_check | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '报告/发现未命中 |