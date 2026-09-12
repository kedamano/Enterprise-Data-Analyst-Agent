# 3 分钟演示脚本 · v2（D18 dry-run 验证）

> 主线：**溯源（E1）→ 写码（E2）→ 迭代（E3）**。目标不是"展示功能多"，而是回答分析师的三个问题：
> ①「这数字哪来的？」②「能不能按我的想法自己写？」③「改个口径要重跑整条链吗？」
>
> **D18 状态**：已在 mock 模式**脚本化跑通三条主线**（见下"执行结果"）；
> 真实模型模式**未跑通** —— 卡在 OpenRouter 余额不足（402），详见"阻塞项"。

## 0. 跑起来

```bash
# 起服务（mock：可复现；真实：把 MOCK_LLM 换 false，先读第 4 节）
MOCK_LLM=true ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 一键 dry-run（推荐：已脚本化，见 scripts/demo.sh）
./scripts/demo.sh                      # 默认 http://127.0.0.1:8000
```

`scripts/demo.sh` 覆盖下面全部三步，并解决了一个 Windows 上的真坑（第 4.1 节）。

---

## 1. 0:00–0:40 溯源：每个数字都能点回 SQL（E1）

```bash
ask demo "分析最近营收变化的原因，按地区维度下钻"   # scripts/demo.sh 里的封装
curl -s localhost:8000/api/v1/chat/analyze/trace/demo | jq '.coverage, .claims[0]'
```

**看点**：`claims[].evidence[]` 带 `sql_id` / `sql`；`coverage` 是「数值 claim → 可溯源」比例。
**说辞**："报告里每个结论级数字都挂着 `sql_id`，点开就是那条查询和它的行样本——不是模型编的。"

## 2. 0:40–1:40 写码：模型自己写 SQL / Python（E2）

```bash
ask demo_sql "只要SQL：给出各渠道营收趋势的语句" | jq -r '.report'
ask demo_py  "帮我写个 Python 脚本分析各区域营收" | jq -r '.report'
curl -s localhost:8000/api/v1/chat/analyze/artifacts/demo_py | jq '.artifacts'
```

**看点**：SQL 片段带只读执行预览；Python 模式 `mode=python_code` 交付可运行脚本，
产物落在 `data/artifacts/<session>/`，artifacts 端点可列。
**说辞**："脚本在受限沙箱里真跑；跑挂了把结构化错误回注给模型重写（有界重试）。"
（`[待真实验证]`：真实模型写码正确率——见第 4.2 节，本轮未验）

## 3. 1:40–2:40 迭代：改口径不重跑全链（E3，本阶段重点）

```bash
ask demo_it "看看各区域营收" | jq -c '{mode, tools:[.tool_results[].tool]}'
ask demo_it "基于上一结果，下钻到区域看营收" | jq -c '{mode, iteration, tools}'
ask demo_it "基于上一结果，改为 2024年3月 的营收" | jq -c '{mode, iteration.kind}'
ask demo_it "基于上一结果，改为 2025年1月 的营收" | jq -c '{mode, iteration}'   # 守卫回退
ask demo_it "基于上一结果，只看 region 1" '"force_full_rerun":true' | jq -c '{mode}'
```

**看点**：下钻/改期那两次的 `tools` 只有 `python_analysis`（`schema_search`/`sql_query` 全不跑），
`iteration.kind/stages` 标明"只做了哪一步"；越界与新口径由守卫拦下，`mode` 回到 `full`。
**说辞**："上一结果是那一次 SELECT 的投影——新口径、新维度我们不硬算，直接回数仓重取；
能就地算的，只跑那一步。"
> ⚠ mock 下只演示**越界改期**被拦：mock planner 的 SQL 选了 fact 全列，
> "改订单数"这类请求会因列确实存在而**正确**地走增量。演示新口径拦截需窄投影数据集
> （见 `test_measure_switch_falls_back_to_full_chain`）。

## 4. 收口 2:40–3:00

```bash
python -m app.eval.runner --mode mock    # FINISH/断言/工具成功率/溯源覆盖率
```

---

## 4. 真实模式前置检查（D18 新增，别跳过）

### 4.1 中文请求体必须走文件（Windows Git Bash）
`curl -d '{"query":"中文…"}'` 在 Git Bash 下会把中文按本地代码页交给原生 curl，服务端直接报
`{"detail":"There was an error parsing the body"}`（管道根本没启动，0.2s 返回）。
正确写法（`scripts/demo.sh` 已封装）：

```bash
printf '%s' '{"query":"分析最近营收变化的原因","session_id":"demo"}' > body.json
curl -X POST localhost:8000/api/v1/chat/analyze -H 'content-type: application/json' \
  --data-binary @body.json
```

### 4.2 真实模型：先验余额，否则整条链静默降级为 mock
`.env` 的 key（OpenRouter）**有效**，但账户额度不足时每个调用都会 **HTTP 402**：

> "You requested up to 2048 tokens, but can only afford 1798."

`LLM_MAX_TOKENS=2048`（`app/config.py`）→ 每次真实调用都被拒 → 路由器 catch 后
**静默降级为 Mock**（日志 `LLM call failed (<stage>); falling back to mock`），
连续失败后熔断器打开、后续阶段直接短路。

**此时你看到的现象是"一切正常"**：`/health` 仍报 `mock_llm: false`（读的是配置值）、
请求返回 `status: FINISH`，但报告正文里会出现「**Mock分析师未做深度统计推断**」。
演示前务必：

```bash
# 1) 跑一次最小调用验余额（会花极少额度）
# 2) 或服务启动后检查日志有没有静默降级
grep -c "falling back to mock" _d18_server.log
```

> 这是**产品缺陷**（降级对调用方不可见），已记入 D18 阻塞项，待修。

---

## 5. D18 dry-run 执行结果与阻塞项

**执行结果（mock，2026-09-10）** —— `scripts/demo.sh` 全部通过：

| 步骤 | 结果 |
|---|---|
| ① E1 | `{"status":"FINISH","mode":"full","findings":2}` / `coverage={"numeric":2,"traced":2,"rate":1}` / `first_claim="step_4"` |
| ② E2 | 自由 SQL 交付 ```sql + 只读执行预览（100 行）；`{"mode":"python_code","has_code_block":true}`；artifacts 2 项 |
| ③ E3 | 首轮全链 5 工具 → 下钻 `{"mode":"iteration","kind":"drilldown","stages":["filter","groupby"],"tools":["python_analysis"]}` → 期间内改期 `kind=date_change` → 越界改期 `mode=full, iteration=null` → `force_full_rerun` `mode=full` |

**阻塞项**
1. ✅ 已解决：中文 JSON 内联 curl 在 Git Bash 下失效 → 改为文件体，封装进 `scripts/demo.sh`。
2. ⛔ **未解决（需你决策）**：OpenRouter 余额不足 → 真实模型三主线**未跑通**。
   修复方向：充值，或调低 `LLM_MAX_TOKENS`（额度只够 1–2 次调用，跑不完整链）。
3. ⛔ **未解决（产品缺陷）**：静默降级对调用方不可见（`AnalyzeResponse` 无降级字段、
   `/health` 不探真实可达性、`router.fallback_events()` 只给测试用）。
   按铁律 3「任何静默降级判失败」，建议：响应带 `degraded` + `llm_fallbacks[]`；
   `/health` 反映真实可达性；熔断打开时显式告警。
