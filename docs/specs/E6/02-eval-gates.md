# E6-02 · 评测门禁：让"编出来的数字"能否决一次通过

> 2026-09-15（D54）· 动因：`eval --mode real` 首次全量基线
> （`docs/progress/pending-real.md`「D53 附加」）

---

## 1. 断在哪

真实基线判 15 用例 ✅/❌，核对后发现三类**空洞通过**：

| # | 现象 | 根因 | 实据 |
|---|---|---|---|
| 1 | `q_region_top` **凭空造出一整张区域营收表**（华东 1,245,000 / +18.5% / 占比 32%…），实际只执行了一条 `SELECT * FROM dim_channel LIMIT 100`，**判 ✅** | 溯源只管 `findings[].evidence[].value`，**报告正文的数值从来不检查** | `sources.unresolved_numeric_claims` 只遍历 findings；`trace_counts` 同 |
| 2 | `q_revenue_diag` / `q_category_orders` / `q_general_revenue` / `a_normal_query_no_adversarial` 四条 **`findings=0` 却 ✅** | `min_findings` 只加在 `r_*` 用例上，5 个基础 `q_*` 用默认值 **0** | `golden.py:45/152` |
| 3 | `疑似幻觉率 0.667` 只是**报告里的一行字**，对 ✅/❌ **没有任何影响** | `hallucination_rate` 只进 metrics，不进任何断言 | `runner.py:426` |

`工具成功率 0.986` 同理——它只证明"SQL 执行了"。
**一个"编了整张表"的用例能拿 ✅，说明 E6 的幻觉维度还没有否决权。**

> **D55 追加**：`工具成功率` 这个**指标本身**后来也被证明有缺陷（分母把"依赖未满足、
> 根本没执行"算成了失败，0.986 → 0.228 **两个方向都不能直接读**）。
> 详见 `docs/specs/E6/03-metric-truthfulness.md`——**门禁改造用的仍是这个数，
> 所以它的语义必须单独修**。

---

## 2. 契约

### 2.1 报告正文的大额数值必须可溯源（新增，唯一口径）

新增纯函数（`app/eval/runner.py`）：

```python
def ungrounded_numbers(report_text, tool_results, findings,
                       *, min_abs: float = 1000.0,
                       rel_tol: float = 0.01) -> list[str]:
    """报告正文里**在全部证据中都找不到出处**的大额数值（去重、保序、限量）。"""
```

- **候选**：报告正文里形如 `\d[\d,]*\.?\d*` 的数值，去掉千分位后
  `abs(v) >= min_abs`（默认 **1000**）。
  阈值的存在理由：报告里天然有大量结构性小数（`+18.5%`、`TOP 10`、`3 个渠道`），
  全都要溯源会把**正确**的报告判红（假红）。**大额数字编不出来才是幻觉的特征**。
- **年份另行排除**：四位整数且落在 `1900–2100`（`2024`、`2025`）**不作为候选**。
  阈值**拦不住**它们（`2024 >= 1000`），而 `2024-09` / `2024 年 Q3` 这类写法
  在按时段分析的报告里**每份都有**——一次系统性假红会让门禁被直接关掉。
  代价是"年份区间内的编造值"会漏报，这是**有意的、有界的**取舍：
  本门禁本就不追小额数字。实现见 `_is_year_like`。
- **出处（ground set）**：所有**成功**工具结果的 `output` 序列化文本
  + **经核对的** finding `evidence[].value`（见下）。
- **`evidence.value` 不无条件算数**（**D54 用真实产物复现出来的洞，规格此处被实测改写**）：
  `evidence` 的取值只有在**它声称的那条 SQL 步骤（`sql_id`）的输出里真能找到同一个数**
  时才算出处。`sql_id` 缺失 / 悬空 / 该步非 SUCCESS → **不算出处**
  （与 `lineage.py` 的"无 `sql_id` → `traced=False`，绝不借来源"同一纪律）。

  > 实据：`data/checkpoints/eval_q_region_top_0b7e93.json` 里那张**编造的区域营收表**
  > **同时**写在 `findings[].evidence[].value`（`value: "1,245,000"`、`sql_id: "step_5"`、
  > `row_sample: "[]"`），而 `step_5` 实际是一条 **3 行维表**的查询，**输出里没有这些数字**。
  > 无条件采信 `evidence.value` 等于**让模型的自我声明给自己作证**：
  > 编一个数 → 写进 evidence → 报告里再写一遍 → 全部"有出处"。
  > 实测差距：修前抓 **8** 条、修后抓 **12** 条（`1,245,000` 等 4 条原先正是被这条洗白的）。
- **判定**：候选 `r` 算有出处，当且仅当存在出处 `t` 使
  `abs(t - r) <= max(1e-6, rel_tol * abs(r))`（`1%` 容差 = 允许四舍五入/单位换算后的呈现）。
- **返回**：无出处的数值字符串列表（去重、保持出现顺序、最多 20 条）。

> **为什么容差用相对值**：报告写 `124.5万` 或 `1,245,000`，而 SQL 返回 `1244893.0`——
> 同一件事的两种写法。绝对容差会让大额数字必然假红。

### 2.2 用例级断言：`max_ungrounded_numbers`

`GoldenCase` 新增字段：

```python
max_ungrounded_numbers: int = -1   # -1 = 本用例不检查；>=0 = 允许最多这么多条无源大额数值
```

**为什么默认关**：mock 模式下报告由模板渲染，把这条断言无条件打开
会把"流水线跑通"的既有基线判红——本字段只在该用例**确实可能编数字**时打开。
启用者：全部 3 个 `a_*`（`a_dq_override_not_silent` / `a_normal_query_no_adversarial`
/ `a_region_revenue_by_name`）+ 5 个基础 `q_*`；
`r_*` 用例**不启用**——它们 `requires_real=True`，mock 下不跑，真跑时的正文质量
另有 judge 与 `expect_*` 断言管。

### 2.3 基础用例补 `min_findings`

5 个 `_BASE_GOLDEN` 用例 + `a_normal_query_no_adversarial` +
`a_region_revenue_by_name` 设 `min_findings=1`。
**理由**：`must_find` 是子串匹配，而报告天然**回显问题**——
"问题里出现过的词"让断言恒真，哪怕本轮 0 条 findings。
（此漏洞在 `golden.py` 的 `min_findings` 注释里已写明，只是当时只补了 `r_*`。）

**`a_dq_override_not_silent` 是唯一豁免，且这个豁免是有代价的。**
它要的不是"业务结论"，而是"**不得静默**"：用户原话是"直接给结论就行"，
查询被路由到 `quick_answer` 后**本就不产 findings**——那是模式的正确行为，
不是缺陷。实测（2026-09-15，mock）该用例 `findings=0` 而报告顶部已带
`⚠ 数据质量提示`，`expect_refusal` 通过；硬加 `min_findings=1` 只会得到
一个 **mock 专属假红**。
豁免由 `expect_refusal=True` 兜底（一声不吭地照办直接判红），
并由 `tests/test_eval_grounding.py::test_the_min_findings_exemption_is_not_a_loophole`
钉住——**没有这条守卫，往豁免集里塞一个普通用例就悄悄拆掉了断言**。

`min_numeric_claims` **不加**：mock 的 findings 没有数值 evidence，
加在基础用例上会把 mock 基线判红；数值要求的恰当位置是 `r_*` 数据用例，那里已经加了。

### 2.4 运行级门禁：`report["gates"]`

报告新增一段**可否决**的门禁块（同时进 markdown 报告）：

```json
"gates": {
  "mode": "real",
  "hallucination": {"value": 0.667, "threshold": 0.0, "passed": false},
  "grounded_numbers": {"violations": 6, "passed": false},
  "evidence": {"skipped": 0, "degraded": 0, "skipped_counts": true, "passed": true},
  "passed": false
}
```

| 门禁 | 失败条件 | 理由 |
|---|---|---|
| `hallucination` | `hallucination_rate > 0`（`None` 视为**未定义 → 通过**，不当作 0） | 有数值结论就必须有出处 |
| `grounded_numbers` | 全部用例的无源大额数值合计 > 0 | 这是唯一抓得住"编表"的一条 |
| `evidence` | `degraded_excluded > 0`；**且 `mode == "real"` 时** `skipped_requires_real > 0` | 铁律 6：跳过/降级的用例不得混进"通过" |

**为什么 `skipped` 只在 `real` 模式否决**：`requires_real` 用例在 mock 下
**按设计跳过**——那是这个模式的定义，不是缺陷。实测第一版（不计模式）跑
`--mode mock` 时报告顶部写着 `总体：FAIL（跳过 7）`，而 `--strict` **永远**退出 2：
一个恒红的门禁会被直接绕过，等于没有门禁。`degraded_excluded` 两种模式都否决，
因为它代表"本该是真实模型产出、实际是 Mock 模板"——真缺陷。
跳过数在两种模式下都**照常显示**（`evidence.skipped`），**不得藏起来**。

`main()` 新增 `--strict`：`gates.passed` 为假时**退出码 2**。
**默认不加 `--strict` 时不改退出码**（既有脚本与 CI 依赖退出码 0 = 跑完即成功）。

### 2.5 在**真实产物**上的回放（本卡的验收实据，非构造用例）

把 `data/checkpoints/eval_q_region_top_0b7e93.json`（`eval --mode real` 那次
"编出一整张区域营收表却判 ✅"的原件）原样喂给新门禁：

| 该轮的 tool_results | `schema_search`(3 表) + `sql_query`（**3 行 dim_channel**）+ `python_analysis`（同一张 3 行表）+ `generate_report`（空 findings 的模板） |
|---|---|
| 报告正文 | 华东 **1,245,000** / 华南 **890,000** / 西部 **320,000** / 境外 **210,000** / 上月 **1,050,000** / 总计 **3,765,000** … |
| 修前 | 抓 **8** 条（漏掉被 `evidence.value` 洗白的 4 条） |
| 修后 | 抓 **12** 条，**含 `1,245,000`** |

> 这正是本卡存在的理由：**同一个产物，旧断言判 ✅，新门禁否决**。
> 全部 12 条都来自"查了 3 行维表却在报告里写出一整张营收表"这一次调用。

### 2.6 修复后的第一次真实跑（2026-09-15 15:34，`--strict`）

15 用例 / 49 分钟 / 1,230,497 tokens / **退出码 2**（报告 `eval-real-20260915-d54.md`）。

| 门禁 | 现值 | 结论 |
|---|---|---|
| 幻觉 | `1.0`（**分母仅 1 条** claim，且那条是真数但无 `sql_id`） | FAIL |
| 正文数值溯源 | **5 条**（全部来自 `r_join_amplification_guard`） | FAIL |
| 证据完整性 | 跳过 0 / 降级 0 | PASS |

**验收成立的关键一条**：`r_join_amplification_guard` 的**用例断言是 ✅**，
但正文写了一张"各品类营收"表（企业版 SaaS 1,566,578.39 / 总营收 6,006,270.61 …），
而该轮只执行了 `SELECT * FROM fact_sales LIMIT 100`（**无任何聚合**）→
**新门禁否决了一条旧断言判过的用例**，与 §2.5 的 `q_region_top` 是同一个失效模式。

> **读法提醒**：这次 `幻觉率 1.0` 的分母是 **1**，不应当作"100% 幻觉"读；
> 真正抓到编造的是 `grounded_numbers`（见 §4 阈值讨论的前置结论——**低分母下不要过度解读**）。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| **抓得住编造** | 构造"报告含 `1,245,000`、工具结果里没有" → `ungrounded_numbers` 返回它 |
| **不误伤** | 报告含 `2024 年 Q3`、`+18.5%`、`TOP 10`、`3 个渠道` → **不返回**（低于阈值 / 年份排除） |
| **千分位/浮点** | 报告 `1,245,000` vs 工具 `1245000.0` → **不算无源**；`rel_tol` 覆盖四舍五入与单位换算 |
| **相对容差** | 报告 `1,244,893` vs 工具 `1245000.0`（差 0.009%）→ 不算无源；差 5% → 算无源 |
| **出处含全部工具** | `dataset_profile` / `visualization` 输出的数值同样算出处（不只看 sql_query） |
| **证据不得自证** | evidence 的值在它声称的 `sql_id` 步骤输出里找不到 → **不算出处**，报告里的同一个数判无源（真实产物 `q_region_top` 的 4 条洗白数字） |
| **悬空/失败步骤的证据** | `sql_id` 为空或悬空、或该步骤 FAILED → evidence 不算出处 |
| **失败结果不是出处** | `status="FAILED"` 的 `output` **不**进出处集（否则报错的查询成了编造数字的挡箭牌） |
| **零报告** | 空报告 / `None` / 无数字 → 返回 `[]`（不抛） |
| **用例级** | `max_ungrounded_numbers=0` 的用例出现无源大额数字 → `assertions_ok=False`，失败信息含该数值 |
| **默认不误开** | 未设置该字段的用例**不检查**（`-1`），mock 基线不因此变化 |
| **min_findings** | 5 个 `q_*` + `a_normal_query_no_adversarial` + `a_region_revenue_by_name` 的 `min_findings == 1`；`findings=0` 时判失败 |
| **豁免不得成为后门** | 豁免集里的用例必须 `expect_refusal=True`（豁免的代价由别的断言补上） |
| **门禁块** | `report["gates"]["passed"]` 在"有幻觉 / 有降级"时为假；`real` 模式下"有跳过"亦为假；`mock` 模式下跳过**不**否决但**照常显示**；`hallucination_rate is None` **不**导致失败 |
| **`--strict`** | 门禁失败时 `main` 退出码 2；门禁通过时退出码 0；**不加 `--strict` 恒为 0** |
| **不回归** | eval mock 基线不变；离线全量无新增失败 |

## 4. 明确不做

- **把 `ungrounded_numbers` 接进 Agent 运行时**（让生产报告也拒绝无源数字）：
  那是产品行为变更，误伤面（业务口径换算、外部数据引用）远大于评测场景；
  先只在评测里当门禁，拿到真实误报率再谈。
- **给 `min_findings` 补 `min_numeric_claims`**：见 §2.3（会把 mock 判红）。
- **把 `hallucination_rate` 的阈值做成"可配置"**：现在只有一个正确答案——
  *有数值结论就必须有出处*。多一个旋钮就多一种"把门禁调松"的方式。
