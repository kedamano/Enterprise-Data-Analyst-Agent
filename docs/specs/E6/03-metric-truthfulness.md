# E6-03 · 指标说实话：分母、跳过、以及"未配"≠"免费"

> 2026-09-15（D55）· 动因：真实基线（`eval-real-20260915-d54.md`）读数的两处误读风险

---

## 1. 断在哪

### 1.1 `工具成功率` 把"根本没执行"算成了失败

真实基线 `工具成功率 0.228`（18/79）。逐条核对 `state.tool_results` 的失败原因：

| 错误 | 条数 | 性质 |
|---|---|---|
| `依赖步骤未完成` | **44** | **从未执行**（上游失败被跳过） |
| 模型自写 SQL 报错 | ~15 | 真失败 |
| 其它 | ~2 | |

与 `data/audit/tool_audit.jsonl` 交叉验证：本次会话审计 **35** 条，
而 `state.tool_results` **79** 条，**差值恰好 44** —— 审计只记"真的进了执行器"的调用
（**这是对的**），而 `state` 把"依赖未满足、没跑"也记成一条 `FAILED`。

于是 `tool_success_rate = 成功 /(成功+失败)`：
- D53 那版**虚高**（每步都"成功"地查了同一张 3 行维表 → 0.986）；
- D54 这版**虚低**（大半"失败"压根没执行 → 0.228）。

**两个方向都不能直接当质量读**，而报告里它只是一个裸数字。

### 1.2 「成本 USD 0.0」既可能是"免费"也可能是"没配单价"

`compute_cost_usd` 的语义**早就分开了**（单价 `None` → `None` = 不知道；显式 `0` → `0.0` = 确实免费），
但两处把它抹平了：

- `.env` 里写着 `COST_INPUT_PER_MTOK=0` —— 于是"没配"被表达成了"免费"；
- `render_markdown` 直接打印 `m["cost_estimate_usd"]`，`None` 会印成 **`None`**，
  既不叫"未计"也不叫"免费"，读的人只能猜。

真实基线报告里那行 `成本 USD | 0.0` 因此**是未计，不是零成本**——
这条已在 D54 附加里如实写明，但**报告本身没写**。**要让人不看文档也不会读错。**

---

## 2. 契约

### 2.1 `ToolResult.skipped`：把"没执行"标出来

`state.ToolResult` 新增字段：

```python
skipped: bool = False   # True = 因上游未完成而**未执行**（不是执行失败）
```

**只标在"依赖步骤未完成"的四个产出点**（`nodes.py:1106/1153/1233` 与批处理路径）：

```python
ToolResult(step_id=step.id, tool=step.tool, status="FAILED",
           skipped=True, error="依赖步骤未完成")
```

**`status` 保持 `FAILED` 不变** —— Reflection 的 REPLAN 判定与 `_dep_done`
都看 `status`，改状态会连带改调度行为（超出本卡范围）。

### 2.2 指标：分母只算"执行过的"

`CaseOutcome` 新增 `tool_skipped: int`；`evaluate()` 汇总：

| 指标 | 定义 | 说明 |
|---|---|---|
| `tool_success_rate` | `success / (success + fail)`，**`fail` 只数 `not skipped`** | 语义收紧为"**执行过的**调用里成功的比例" |
| `tool_skipped_total` | **新增**，`Σ skipped` | **必须报出来**——不然分母变小而读者不知为什么 |
| `tool_calls_total` | 不变（含 skipped） | 与 `avg_tool_calls` 口径一致 |

`skipped` 用例**不计入** `tool_success_rate` 的任一方向：它既不是成功，也不是失败，
**是一次没发生的调用**。三个数一起看才能还原真相。

> 若执行过的调用数为 0 → `tool_success_rate` 为 **`None`**（**未定义**，不是 0），
> 与 `hallucination_rate` 零 claim 的处理**同一条纪律**。

### 2.3 成本：`None` 在报告里是"未计"，`0.0` 是"免费"

`render_markdown` 的成本行改为：

| 值 | 渲染 |
|---|---|
| `None` | `未计（未配单价）` |
| `0.0` | `0.0（单价为 0 = 已知免费）` |
| `>0` | 数字 |

并：`.env` 里那两行 `COST_*_PER_MTOK=0` **注释掉**（恢复成"未配"的诚实状态），
`.env.example` 已写明用法，补一句"填真实单价后成本才有意义"。

> **NOTE:** 当前实现已对齐——
> - `.env`（第 52-53 行）与 `.env.example`（第 49-50 行）中 `COST_INPUT_PER_MTOK` / `COST_OUTPUT_PER_MTOK` 已被注释掉，成本行恢复为"未计（未配单价）"的诚实状态；
> - 渲染逻辑在 `app/eval/runner.py` 第 672-679 行：`None` → `未计（未配单价）`、`0.0` → `0.0（单价为 0 = 已知免费）`、正值直接出数；
> - `compute_cost_usd` 语义已区分：单价 `None` → 返回 `None`、显式 `0` → 返回 `0.0`，契约不可回退；
> - `.env.prod.example` 中留有 `COST_INPUT_PER_MTOK=0.27` / `COST_OUTPUT_PER_MTOK=1.10`（生产计费示例，非硬编码）。

**不猜单价**：`deepseek-v4-flash-w8a8` 在 matrix 网关上的单价**只有使用者知道**，
代码里硬编码一个数是**编数据**。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| 跳过被标记 | `依赖步骤未完成` 的 `ToolResult.skipped is True`；真正的执行失败（如 `no such column`）为 `False` |
| 分母正确 | 5 成功 / 3 失败 / 10 跳过 → `tool_success_rate == 5/8 == 0.625`，`tool_skipped_total == 10`，`tool_calls_total == 18` |
| 跳过多但没有失败 | 5 成功 / 0 失败 / 10 跳过 → **`1.0`**（不是 0.33） |
| 全跳过 | 0 成功 / 0 失败 / 3 跳过 → **`None`**（未定义），不是 0.0 |
| 跳过可见 | 报告指标表**同时**出现成功率与 `跳过（依赖未满足）` 两个数 |
| 成本渲染 | `None` → 含"未计"；`0.0` → 含"免费"；`12.5` → 含 `12.5` |
| 成本语义不混 | `compute_cost_usd(None, None, 100, 100) is None` 且 `compute_cost_usd(0, 0, 100, 100) == 0.0`（**既有契约，不得回退**） |
| 不回归 | mock 基线**若**因分母变化而变，必须**逐项解释**（哪些用例有 skipped）；用例数/断言通过率不受影响 |

## 4. 明确不做

- **不把 skipped 从 `tool_results` 里删掉**。它是**事实**（计划里有这一步、它没跑）；
  把事实删掉才是掩盖。改的是**指标的分母**。
- **不改 `status` 枚举**（不加 `SKIPPED` 状态）。调度、`_dep_done`、Reflection
  都依赖现有三态；加状态是另一张卡的事。
- **不硬编码模型单价**。

---

## 5. 回填（2026-09-15 实施）

- **用例 11 条**（`tests/test_eval_tool_metrics.py`）全绿，覆盖规格 §3 的 8 条验收；
  全量离线回归 **1217 passed / 32 skipped / 0 failed**。
- **用 D54 那轮数据重算（不重跑模型）**：`18/(18+17) = 0.514`（原报 `0.228`）。
  **两个独立口径互证**：新分母 `35` **恰好等于** `data/audit/tool_audit.jsonl`
  里本次会话的 `35` 条 —— 审计一直只记"真的进了执行器"的调用，它是对的。
  **这是重算值，不是新基线**；`eval-real-20260915-d54.md` 是生成物，不改写历史读数。
- **mock 基线逐项解释**（规格 §3 要求）：mock 下 `tool_skipped_total = 0`
  （8 条计分用例全部 FINISH，无依赖级联）→ **分母不变、`tool_success_rate` 仍为 `1.0`**；
  **唯一可见变化是成本行** `0.0` → `未计（未配单价）`——**这一处正是修好了**。
- **顺带修掉渲染器的一处脆性**：`render_markdown` 改用 `.get()` + `_verdict()`，
  缺一个门禁键不再 `KeyError` 把**整篇报告**打掉；**缺项渲染 `—` 而不是 `PASS`**
  （缺项不等于通过）。
- **顺带修掉一处自伤**：本卡新写的 `test_session_scope.py` fixture 把开关写成 `MOCK_LM`
  （**不存在的变量**，真名 `MOCK_LLM`）→ 三条 `/chat/analyze` 用例**真去打了线上模型**
  （`210.07s` vs `6.12s`）。**离线用例偷偷联机**比"慢"严重得多，已全仓扫过（仅此一处）。
