# ⚠️ 本文件不是有效基线 —— 请勿引用其中数字

> **为什么不能引用**：本轮 7 条用例中有 **5 条发生了 LLM 降级**（OpenRouter 免费档
> 429 `openrouter_free_tier_daily`，50 次/日已用尽），runner 已按 DEGRADE/01 的口径
> 把它们标记为 `DEGRADED` 并**从计分中剔除**。也就是说：
> **"实际计分用例数 = 2"**，下面所有比率都只基于这 2 条，不能代表模型能力。
>
> 这份文件保留下来，是因为它**恰好证明了测量诚信那一层在真实运行中生效**——
> 若按旧 runner，这 5 条 mock 模板产出会被当成真实成绩计分，产出一份
> "看起来正常"的假基线。现在报告自己写出了"这批数字不可用"。
>
> **重跑**（额度 2026-09-14 08:00 重置后）：
>
> ```bash
> DATA_DB_URL=sqlite:///./data/sample_analyst.db DATA_DB_DIALECT=sqlite \
>   .venv/Scripts/python.exe -m app.eval.runner --mode real --only-real \
>   --out docs/progress/eval-real-analyst-dataset.md
> ```
>
> 若再次出现 ⚠️ 降级块，说明额度仍不够（单轮约需 70+ 次调用，免费档 50/日**不够跑完一轮**）。

---

# Eval 报告

- 模式：real　用例数：7　时间：2026-09-13 12:57:18

## 指标

| 指标 | 值 |
|---|---|
| FINISH 率 | 0.5 |
| 断言通过率 | 0.0 |
| 平均工具调用 | 3.0 |
| 工具成功率 | 1.0 |
| 平均 LLM 调用 | 6.0 |
| Reflection PASS 率 | 0.0 |
| LLM-judge 平均分 | 0.525 |
| LLM-judge 方法 | rubric-offline |
| 平均报告长度 | 1161.1 |
| 平均耗时(s) | 267.037 |
| prompt tokens | 314919 |
| completion tokens | 28004 |
| 总 tokens | 342923 |
| 成本 USD | 0.0 |
| 溯源覆盖率 | None |
| 溯源 claims | 0/0 |
| 跳过（需真实模型） | 0 |
| **降级剔除（非真实模型产出）** | 5 |
| 实际计分用例数 | 2 |

> ⚠️ **本轮有 5 条用例发生 LLM 降级，已从真实基线剔除**：r_decompose_before_attribution、r_join_amplification_guard、r_causal_overreach、r_multiple_comparison、r_simpson_check
> 降级阶段：analyst、context、planner、reflection、reporter。这些用例的产出其实是 **Mock 模板**，不代表真实模型能力。
> 常见原因：上游 429 限流 / 余额不足 / 模型不可用。**请先解决额度问题再重跑**，否则基线不可用。

## 用例明细

| id | status | tools | llm | findings | refl | assert | err |
|---|---|---|---|---|---|---|---|
| r_caliber_period_mismatch | CLARIFY | 0 | 1 | 0 | None | ❌ | ['期望 FINISH，实际 CLARIFY: None', '缺少口径问题 k |
| r_ratio_denominator | FINISH | 6 | 11 | 0 | ReflectionDecision.REPLAN | ❌ | ['报告/发现未命中关键片段: 分母', '缺少质量门禁 code: untes |
| r_decompose_before_attribution | DEGRADED | 8 | 11 | 5 | ReflectionDecision.PASS | ⚠️ | ['本轮发生 LLM 降级（analyst、planner、reflection |
| r_join_amplification_guard | DEGRADED | 4 | 5 | 1 | ReflectionDecision.PASS | ⚠️ | ['本轮发生 LLM 降级（analyst、context、planner、re |
| r_causal_overreach | DEGRADED | 5 | 5 | 2 | ReflectionDecision.PASS | ⚠️ | ['本轮发生 LLM 降级（analyst、context、planner、re |
| r_multiple_comparison | DEGRADED | 5 | 5 | 2 | ReflectionDecision.PASS | ⚠️ | ['本轮发生 LLM 降级（analyst、context、planner、re |
| r_simpson_check | DEGRADED | 4 | 5 | 1 | ReflectionDecision.PASS | ⚠️ | ['本轮发生 LLM 降级（analyst、context、planner、re |