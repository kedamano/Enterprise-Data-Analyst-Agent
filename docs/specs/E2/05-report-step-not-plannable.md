# E2-05 · `generate_report` 不能作为计划步骤（一个**按构造做不到**的僵尸步骤）

> 2026-09-15（D57）· 动因：D54 真实基线里 `generate_report` 计划步骤 13/18 FAILED
> **本卡有两处自我纠正**：§1.0（对**缺陷**的断言是错的）与 §1.1 第 3 条
> （对**事实**的断言是错的，见 §5.1）。两处都在实施前/实施中被查出并改正。

---

## 1. 断在哪

### 1.0 原先的描述，以及它错在哪

我在 D56 收官清点里写的原话是：

> **`generate_report` 13/14 次 FAILED 全是依赖级联：用户拿到的是自由回答原文而不是模板报告。**

前半句**是对的**（在 §1.2 用真实产物复核：13 FAILED，全部"依赖步骤未完成"）。
**后半句是错的**——用户拿到的是报告，是 `run_reporter` 在**分析之后**正常产出的那份。
把它错判成"没有报告"，是因为**交付物在任何落盘产物里都看不见**（§1.1 第 3 条），
而我拿"计划步骤 FAILED"当成了"没出报告"。**这正是本项目反复栽的那个坑：
把"我能看到的信号"等同于"事实"。**

### 1.1 交付物其实在（三条证据）

| # | 证据 | 出处 |
|---|---|---|
| 1 | eval 读的报告文本**就是** `state.report`；基线里 `平均报告长度 1622.1` | `app/eval/runner.py:384`（`out.report_text = state.report or ""`）；`docs/progress/eval-real-20260915-d54.md` |
| 2 | **每条终态路径都写 `state.report`**：`run_reporter`（REPORT / FAILED / ERROR / 无进展 stall 四种都走它，`graph.py:174-185`，注释写着 "produce a best-effort report even on failure"）、`modes.terminal_sql_only`/`terminal_quick`、`pycode.deliver_python_code`、`iteration.py`、`rigor.ensure_dq_disclosure` 追加披露 | `nodes.py:1589/1603`、`modes.py:81/103/248`、`pycode.py:75`、`iteration.py:359`、`rigor.py:175` |
| 3 | **`state.report` 在 checkpoint 里**（D57 更正，见 §5.1）：`save()` 落的是 `state.model_dump(mode="json")`——**整个 state**，`report` 字段一直都在 | `checkpoint.py:30`；实测 `data/checkpoints/real_997708f610.json` 的 `report` = **2345 字符**全文 |

**第 3 条原来是错的，已更正。** 我当时的依据是 `grep report checkpoint.py` **零命中**，
于是断定"交付物不落盘"——**那个推理无效**：该文件**从不点名任何字段**，它 dump 整个 state。
交付物一直可以事后核对，**我从来没打开过那个字段**。于是：

* §1.0 那次误判的**真实成因**不是"看不见交付物"，而是**"我只读了步骤状态，没读交付物"**；
* §4 里那张"候选卡：持久化 `state.report`"**随之取消**——它已经持久化了；
* 本卡后来最硬的证据（§5.3 的 53 → 1）正是**从这批被我以为看不见的文件里查出来的**。

### 1.2 真正的缺陷：这个步骤**按构造**做不出报告

`generate_report` 的**唯一输入**是 `state.analysis`（`report_tool.run` 只读
`analysis` / `reflection` / `objective`，见 `app/core/tools/report_tool.py:15-21`）。

但流水线顺序是 **Executor → Analyst**（`graph.py:161` 执行、`graph.py:172` 才分析），
所以在执行器阶段，`state.analysis` 还是默认空值
（`state.py:508` `analysis: AnalysisResult = Field(default_factory=AnalysisResult)`）。

**用真实产物回放（D54 那轮，`data/checkpoints/eval_*.json`，14:40–15:40 窗口 15 个用例）**：

| 结果 | 条数 | 说明 |
|---|---|---|
| 依赖级联 → 跳过 | **13** | 全部 `error="依赖步骤未完成"` |
| **SUCCESS** | **1** | `eval_r_join_amplification_7cafe1.json` 的 `step_4`，`execution_time_ms: 0` |
| 用例压根没有报告步骤 | 4 | `a_dq_override_not_silent` / `a_region_revenue_by_name`（轻模式）与两条 CLARIFY |

那唯一一条 SUCCESS 的产出（**原文，163 字符**）：

```markdown
# 数据分析报告：统计各品类营收

## Executive Summary

（基于工具获取的真实数据形成结论）

## Key Metrics

_未显式计算指标，详见发现。_

## Key Findings

## Hypotheses

## Recommendations

## Limitations
```

**空壳**：零 findings、零指标、零建议——因为吃到的是空 `analysis`。

所以两条路**都是坏的**：

* 被依赖级联跳过 → 该步**从未执行**（13 条）；
* 侥幸执行（某轮 REPLAN 后依赖恰好满足）→ **产出一份空壳报告**（1 条）。

**没有一条路能产出真报告。** 而且它的输出**没有任何消费者**——交付物由
`run_reporter` 在之后写（`nodes.py:1589/1603`）。**它是个僵尸步骤：排得出来、做不到、产出的东西没人要。**

### 1.3 三个连带伤害

1. **污染真话指标**：18 条 `generate_report` 记录里 13 条 skipped + 1 条空壳 SUCCESS，
   全在 `工具成功率` 与 `tool_skipped_total` 里。D55 刚把分母修诚实（E6/03），
   这里却还站着一个**永远做不到**的步骤——**分母诚实 ≠ 分母里的东西都是合理的**。
2. **顶替真实数据步骤**（最有害）：D54 产物里 `q_channel_trend` 的失败信息是
   `期望调用工具 schema_search，实际执行 ['generate_report', ...]`——
   模型拿"出报告"顶了"取数"，用例因此判红。而 `routing.py:36` 给它挂了
   高判别力的关键词（`报告 周报 月报 汇报 结论 建议 总结`），`planner.md:236/256`
   还明确教它"只在最后一步用"——**我们把它推到了模型眼前**。
3. 浪费一轮工具调用与一个 REPLAN 回合。

> **一条声明不一致（同一病根）**：`specs.py:212` 的 ToolSpec 声明的入参是
> `analysis_result` / `format`（还有 `html`/`pdf`/`docx`），而执行器传的是
> `analysis`/`reflection`/`objective`、`report_tool.run` 读的也是 `analysis`。
> **三份描述互不相同**——因为这份 ToolSpec 描述的是"一个从分析结果出报告的工具"，
> 而流水线里**不存在**这样一个执行位置。

---

## 2. 契约

### 2.1 预防：`generate_report` 不再进入 planner 的可见工具集

* `app/core/tools/specs.py` 新增唯一常量 **`NOT_PLANNABLE_TOOLS = frozenset({"generate_report"})`**，
  注释写明理由是**流水线顺序**（它的输入 `state.analysis` 在 Executor 阶段还不存在）。
* `select_tools_for_planner`（`routing.py:123`）**过滤**该集合。
  **必须在这里过滤**：工具数 ≤ `threshold_count`（默认 12）时它返回的是**全部**名字
  （`routing.py:134-135`），而当前工具数没超过阈值 → 实际**永远是全给**，
  所以"少给一个工具"只能靠显式过滤。
* `planner.md`：从工具清单里去掉 `generate_report`，并**删掉**"only as the final step"
  那句诱导；改为一句明确的反向说明（报告由 Reporter 阶段产出，**不要**排报告步骤）。

### 2.2 检测：真出现了就**响亮拒绝**，绝不再渲染空壳

在**步骤级**守卫（`_run_one_step`）：

* 条件：步骤的 `tool` 在 `NOT_PLANNABLE_TOOLS` 内 → **不执行**，返回
  `status="FAILED"` + 可照做的错误信息
  （如"`generate_report` 不能作为计划步骤：它需要 `state.analysis`，而分析阶段在本阶段之后；
  报告由 Reporter 阶段产出"）。
* **不标 `skipped`**：D55 的 `skipped` 专指"依赖未满足、根本没轮到"（E6/03）。
  这里是**计划本身排错了一步**，与"SQL 写错"同类——属于**计划质量**，
  **必须留在失败分母里**，否则"排错步骤"这件事会从指标里消失。
* **不做**"自动帮它执行"或"顺延到 Reporter"：**静默补救比响亮失败更坏**——
  它会把"planner 排了一个做不到的步骤"这件事藏起来。

### 2.3 检测/预防是一对，不是替代（沿用 D56 的既有纪律）

只做 §2.1（不offer）不够：模型可能凭提示词残留或自己的习惯仍然排出来；
只做 §2.2（拒绝）不够：那等于**每一轮都用一次失败来教它**。
两个都要，且**知识只有一个来源**（同一个 `NOT_PLANNABLE_TOOLS` 常量），
不许在提示词里另写一份名单。

### 2.4 交付物不变量：**产出交付物**的终态必须有非空 `state.report`

这是让 §2.1 "从菜单里删掉一个工具"**安全**的前提——必须先证明
**删掉它不会让任何人拿不到报告**。

* 用 `run_analysis` + mock 跑一个**计划里含报告步骤**的用例，
  断言 `state.report` **非空**、且**含真实发现**（而不是 §1.2 那种 163 字符空壳）。
* 这条同时补上 §1.0 那次误判的根：**"用户到底拿到了什么"必须有测试直接钉住**，
  不能靠"我能看到的步骤状态"去推断。

**"终态"要写准**（实测 3726 个 eval checkpoint 的状态分布）：
`FINISH` **3692 / 3692** 全部有非空 `report`；为空只出现在 `CLARIFY`（**33**，
反问时还没有报告，本来就不该有）与 `ERROR`（**1**，规划就失败，没有东西可报）。
所以契约是"**FINISH 必有非空 `state.report`**"——**不是**"任何 status 都非空"：
把 CLARIFY 跟 ERROR 一起要求出报告，等于**又把澄清当成失败**（CLARIFY/01 刚纠正过）。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| **不再可见** | `select_tools_for_planner(...)` 的返回**不含** `generate_report`；`planner.md` 里不再出现 `generate_report` |
| **不是"少给一个工具"的副作用** | 工具数 ≤ 阈值这条路径（`routing.py:134`）下同样不含它——**直接测全给分支** |
| **仍然存在（别误删）** | 执行器注册表里仍有它（`report_tool.run` 是 Reporter 的模板兜底，`run_reporter` 必须继续能用）；`run_reporter` 的产出不回归 |
| **响亮拒绝** | 构造一个含 `generate_report` 步骤的计划 → 该步 `FAILED`、**`skipped is False`**、`error` 含"不能作为计划步骤"与"Reporter" |
| **不再产空壳** | 同一构造下**不存在** `status==SUCCESS` 且 `output["report"]` 为空心模板的结果（即 §1.2 那条 163 字符产物**不可能再出现**） |
| **进失败分母** | 该步骤计入 `tool_fail`（不因它被排除在成功之外就悄悄消失） |
| **交付物不变量** | mock 跑一个含报告步骤的用例 → `state.report` 非空且长度 ≫ 163 / 含 findings 文本；**且该用例先断言"计划里真有这个步骤"**（否则是空转） |
| **知识只有一个来源** | `NOT_PLANNABLE_TOOLS` 是唯一名单；用一个用例钉住"executor 中不可计划集合 == 该常量"（防两处漂移） |
| **不回归** | 离线全量无新增失败；`eval --mode mock --strict` 基线不变 |

## 4. 明确不做

- **不动 `report_tool.run` 对空 `analysis` 的行为**：它是 `run_reporter` 的模板兜底，
  且 mock / iteration 路径可能依赖"总能渲染出东西"。本卡把知识放在**步骤层**
  （那里才知道流水线顺序），不在渲染函数里加"拒绝渲染"。
- **不动 `run_reporter` 的渲染链路**（REP/01 的 LLM 报告、REP/02 的净化、E1 溯源、D51 嵌图）：
  本卡只**移除一个做不到的入口**，交付物本身的形态与质量不变。
- ~~**不持久化 `state.report`**：这是真缺口，另开一张卡~~ ——
  **【D57 更正】不是缺口，候选卡取消**：`save()` 落的就是整个 state，
  `report` 一直在 checkpoint 里（§1.1 第 3 条 + §5.1）。
- **不改 `tools/specs.py` 里那份不一致的 `input_schema`**：先记录（§1.3 末），
  等 Reports/导出那一摊要动时一并对齐——**改它要连带确认 MCP 与导出路径**，本卡不顺手改。

## 5. 回填（2026-09-15 实施）

### 5.1 改动清单（4 处）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `app/core/tools/specs.py` | 新增**唯一**常量 `NOT_PLANNABLE_TOOLS = frozenset({"generate_report"})`（附 14 行理由：**流水线顺序**，不是"工具不好"） |
| 2 | `app/core/tools/routing.py` | `select_tools_for_planner` **在计数与打分之前**摘掉该集合（`plannable` 再喂 `route_tools`）；`_ALIASES` 那条注释写明它**对 planner 已失效**（保留是因为 `route_tools` 仍是通用原语——**故意不动它的词表**：删词会改 IDF，进而**悄悄挪动其它工具的命中**，那是本卡之外的改动） |
| 3 | `app/core/agents/data_analyst/nodes.py` | `_NOT_PLANNABLE_MSG` + `_not_plannable_error(step)`；`_run_one_step` 里**排在依赖检查之前**（否则又被记成"依赖步骤未完成"——D54 那 13 条假象就是这么来的） |
| 4 | `app/core/prompts/data_analyst/planner.md` | 工具清单里删掉 `Final Report → generate_report`；`# Tool Selection` 下补**正向**说明"没有报告步骤，报告由 Reporter 阶段在你跑完之后产出"；删掉 `only as the final step` 那句诱导 |

### 5.2 实施中被**用例**挡下的两处（都是真的会误导模型）

**(1) "不点名"比"写个负例"更强。** 我先在 misrule guardrails 里补了一条
`- generate_report — **never plan it**…`，被自己的用例红在 `assert "generate_report" not in text`。

判据是对的，而且**正是 D54 的教训**：`routing.py:36` 的别名 + `planner.md:256` 那句
"只在最后一步用"就是把它**推到了模型眼前**；**点名一个工具（哪怕是否定句）都会让它进入候选**。
现在改成：整份提示词里**不出现这个名字**，只留"报告由 Reporter 阶段产出"这条**正向**说明；
模型若凭自己的习惯排出这一步，**执行器侧的响亮拒绝**就是答案（§2.3 的"两个都要"）。

**(2) "任何终态"写强了。** 原文是"任何终态都必须有非空 `state.report`"，
实测 3726 个 eval checkpoint：`FINISH` **3692/3692** 非空，为空的全是
`CLARIFY`（33）与 `ERROR`（1）。契约收窄为 **FINISH 必有**（§2.4）——
把澄清也要求出报告，等于**又把 CLARIFY 当失败**。

### 5.3 语料证据：53 → 1（真实路径上的预防是否生效）

对 `data/checkpoints/*.json` 全量扫描"计划里是否含 `generate_report` 步骤"：

| 窗口 | checkpoint 数 | 含报告步骤的 |
|---|---|---|
| 改动前（< 17:37） | 3738 | **53** |
| 改动后（≥ 17:37） | 143 | **1** |

那 **1** 个是 `e2_05_report_step`——**我自己的用例**（§2.4 故意让 mock planner 排一个报告步骤）。
**真实模型与 mock 的跑里 0 个。** 同一批文件还给出交付物的对照：
该用例 `report` = **819 字符**（§1.2 的空壳是 163）；`test_full_pipeline_reaches_finish`
那次真实重跑 `report` = **2345 字符**。

### 5.4 回归与门禁

* 新增 `tests/test_report_step_not_plannable.py` **12** 条。
  红→绿的对应：写完后 **10 failed / 2 passed**，那 2 条是"别误删"（注册表与直接调用——
  实施前就该是绿的）；实施后 **12 passed**。
  红的时候失败信息**当场复现了 §1.2**：`status='SUCCESS', execution_time_ms=0`，
  输出里带着 `## Limitations` 的空壳模板——**诊断与产物在同一个断言里对上**。
* **离线全量**（不含真模型套件，1289 条）：**1256 passed / 32 skipped / 1 failed**——
  唯一的红是 `test_hallucination_rate.py::test_runtime_wiring_exists_in_reporter`，
  **不是 D57 引入的**，是**我自己编辑 nodes.py 与那次 import 撞车**造成的假红（§5.5）。
  紧接着的一次**干净重跑**（全程不碰文件）：该条通过，改由
  `tests/test_agent_real.py::test_full_pipeline_reaches_finish` 红 —— 见 §5.5。
* `eval --mode mock --strict` **退出码 0**，与存档基线 `docs/progress/eval-mock-d55.md`
  **除耗时外逐项相同**（工具成功率 1.0 / 跳过 0 / 报告长度 405.5 / 溯源 13-13 /
  judge 0.881 / 成本 `未计（未配单价）`）；本次结果已存为 `docs/progress/eval-mock-d57.md`，
  下次可直接对。

### 5.5 两处红的定性（都**不是**本卡引入）

**(1) `test_runtime_wiring_exists_in_reporter`（只在第一次全量跑里红）。**
它断言 `inspect.getsource(nodes.run_reporter)` 里有 `record_trace_coverage`，
而失败时 `src` **只有一行**：`nodes.py:1617` 的 `if (r.input or {}).get("sql"):`。

`inspect.getsource` 是**按行号**取块的：代码对象说 `@trace("reporter")` 在第 N 行，
文件内容却与之错位 → 取到隔壁函数的一行。**症状即结论**：那次进程里的
**代码对象与 `linecache` 读到的文件来自 nodes.py 的两个版本**——
我的 Edit 与 pytest 的 import 撞了（工具当时也确实提示了"nodes.py 在磁盘上变了"）。
单跑该用例 **0.57s 通过**；干净重跑亦通过。**离线用例对"边改边跑"敏感，这是环境假红。**

**(2) `test_full_pipeline_reaches_finish`（只有第二次全量跑里红）。**
**两次全量跑之间我一个字都没改**，而同一条用例第一次**通过**、第二次**失败**——
**这就是非确定的证据**，不需要再猜。红的**阶段**也印证：失败断言是
`len(s.analysis.findings) >= 1`（实际 0），链条是

```
step_1 的 SQL 里写了 CURRENT_DATE
  → D55 的方言预检拦下（日志原文：步骤 step_1 的 SQL 在 sqlite 上跑不通（未执行）：CURRENT_DATE/CURRENT_TIMESTAMP → date('now') / datetime('now')）
  → step_1 FAILED → 下游"依赖步骤未完成" → 无数据
  → findings=[]，limitations 老实写着"所有依赖步骤均未执行"
  → status=FINISH / report=2345 字符
```

**全在 D55/D56 的方言区**（planner 写错 → 预检拦住 → 重规划），D57 碰的是
**计划步骤菜单**，与之不相交。它同时是 D56 那条 `[待真实验证]` 的**新证据**：
模型这一轮**没有**遵守方言先验（`CURRENT_DATE` 正是 D56 补进 `_ENGINE_NOTE` 的那个构造）。
**判据已经写好，缺的是一次真实基线**（见 §6）。

### 5.6 一条留给下一张卡的观察

`state.report` **在 checkpoint 里**这件事实，本卡是靠**打开文件看**才纠正过来的（§5.1）。
落盘产物里能直接看到的至少还有：`plan.steps[].input.sql`（D54 的 35/35）与
`report`（交付物）。**"某个文件里 grep 不到"永远不构成"产物里没有"**——
这是本卡两次误判的共同形状（§1.0 看信号不看事实、§1.1 grep 一处当全貌）。

---

## 6. 待真实验证

**`[待真实验证]`**

下次 `eval --mode real`：

* 产出的计划里**不再出现** `generate_report` 步骤（D54 是 18 条记录 / 14 个用例）——
  **注意本卡已给出离线全量语料的对照，但真实基线仍未跑**（§5.3 的 53 → 1 里，
  改动后那 143 个 checkpoint **只有 1 个真实跑**）；
* 因此 `工具成功率` 的分母里不再有它（D54 重算值 `18/35 = 0.514` 应上升——**不是变好，是分母里少了一个不该有的东西**）；
* `q_channel_trend` 那类"**拿报告步骤顶替取数步骤**"的失败是否消失；
* 计划仍 **35/35** 自带 `input.sql`（D54 的成果不得回退）；
* 交付物仍非空（`平均报告长度` 不应掉到 163 量级；`FINISH` 用例的 `report` 字段可直接抽查）；
* **顺带（D56 欠的）**：`CURRENT_DATE` 这类方言错是否下降（§5.5(2) 是反例）。
