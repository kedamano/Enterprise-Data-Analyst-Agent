# Eval 报告

- 模式：mock　用例数：15　时间：2026-09-15 18:54:42

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 1.0 |
| 断言通过率 | 1.0 |
| 平均工具调用 | 4.75 |
| 工具成功率 | 1.0 |
| 其中：跳过（依赖未满足，未执行） | 0 |
| 平均 LLM 调用 | 4.62 |
| Reflection PASS 率 | 0.467 |
| LLM-judge 平均分 | 0.881 |
| LLM-judge 方法 | rubric-offline |
| 平均报告长度 | 405.5 |
| 平均耗时(s) | 4.738 |
| prompt tokens | 0 |
| completion tokens | 0 |
| 总 tokens | 0 |
| 成本 USD | 未计（未配单价） |
| 溯源覆盖率 | 1.0 |
| 溯源 claims | 13/13 |
| **疑似幻觉率**（数值无源占比） | 0.0 |
| 跳过（需真实模型） | 7 |
| **降级剔除（非真实模型产出）** | 0 |
| 其中：判断型问题被接受的澄清 | 0 |
| 实际计分用例数 | 8 |

## 门禁（E6/02）

| 门禁 | 判据 | 现值 | 结论 |
|---|---|---|---|
| 幻觉 | `hallucination_rate <= 0` | 0.0 | PASS |
| 正文数值溯源 | 无源大额数值 = 0 | 0 条 | PASS |
| 证据完整性 | 无降级；`real` 下另须无跳过 | 跳过 7（mock 按设计，不否决） / 降级 0 | PASS |

**总体：PASS**

> 幻觉率 `None` = 零数值结论（**未定义**，不是'零幻觉'），不否决；但'没有数值结论'本身由 `min_numeric_claims` 在用例级拦。

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| q_revenue_diag | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| q_region_top | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| q_category_orders | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| q_channel_trend | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| q_general_revenue | FINISH | 4 | 5 | 1 | ReflectionDecision.PASS | ✅ | [] |
| a_dq_override_not_silent | FINISH | 4 | 2 | 0 | None | ✅ | [] |
| a_normal_query_no_adversarial | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| a_region_revenue_by_name | FINISH | 5 | 5 | 2 | ReflectionDecision.PASS | ✅ | [] |
| r_caliber_period_mismatch | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_ratio_denominator | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_decompose_before_attribution | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_join_amplification_guard | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_causal_overreach | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_multiple_comparison | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |
| r_simpson_check | SKIPPED | 0 | 0 | 0 | None | ⏭ | requires_real：mock 无法验证，需 LLM_API_KEY |