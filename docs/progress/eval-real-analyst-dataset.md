# Eval 报告

- 模式：real　用例数：7　时间：2026-09-13 16:13:17

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 0.857 |
| 断言通过率 | 0.143 |
| 平均工具调用 | 6.29 |
| 工具成功率 | 1.0 |
| 平均 LLM 调用 | 9.14 |
| Reflection PASS 率 | 0.143 |
| LLM-judge 平均分 | 0.693 |
| LLM-judge 方法 | rubric-offline |
| 平均报告长度 | 1788.3 |
| 平均耗时(s) | 232.709 |
| prompt tokens | 849222 |
| completion tokens | 65185 |
| 总 tokens | 914407 |
| 成本 USD | 0.0 |
| 溯源覆盖率 | None |
| 溯源 claims | 0/0 |
| 跳过（需真实模型） | 0 |
| **降级剔除（非真实模型产出）** | 0 |
| 其中：判断型问题被接受的澄清 | 0 |
| 实际计分用例数 | 7 |

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| r_caliber_period_mismatch | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少口径问题 k |
| r_ratio_denominator | FINISH | 6 | 11 | 2 | ReflectionDecision.REPLAN | ❌ | ["缺少质量门禁 code: untested_comparison（实际 [' |
| r_decompose_before_attribution | FINISH | 8 | 11 | 1 | ReflectionDecision.REPLAN | ❌ | ['报告/发现未命中关键片段: 拆解', '报告/发现未命中关键片段: 贡献'] |
| r_join_amplification_guard | FINISH | 7 | 11 | 2 | ReflectionDecision.REPLAN | ✅ | [] |
| r_causal_overreach | FINISH | 8 | 11 | 0 | ReflectionDecision.REPLAN | ❌ | ['报告/发现未命中关键片段: 相关', '报告/发现未命中关键片段: 因果'] |
| r_multiple_comparison | FINISH | 6 | 8 | 4 | ReflectionDecision.PASS | ❌ | ['缺少质量门禁 code: multi_comparison_unadjust |
| r_simpson_check | FINISH | 9 | 11 | 4 | ReflectionDecision.REPLAN | ❌ | ['报告/发现未命中关键片段: 分群', '报告/发现未命中关键片段: 分层'] |