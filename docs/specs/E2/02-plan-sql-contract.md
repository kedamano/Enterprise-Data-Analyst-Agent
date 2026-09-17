# E2-02 · 计划步骤必须自带 SQL（执行器不得从目标臆造查询）

> 2026-09-15（D54）· 动因：`eval --mode real` 首次全量基线的解剖
> （`docs/progress/pending-real.md`「D53 附加」）

---

## 1. 断在哪（**逐条有审计实据，不是推测**）

真实基线 15 用例、1,062,713 tokens，跑出 `工具成功率 0.986`。核对
`data/audit/tool_audit.jsonl` 与 `data/checkpoints/*.json` 后发现：

| # | 事实 | 证据 |
|---|---|---|
| 1 | planner 出的 8 步计划**目标写得很好**（"按产品/渠道下钻华北营收"） | `eval_q_revenue_diag_7c5cd8.json` 的 `plan` |
| 2 | 但**每一步的 `input` 都是 `{}`** | 同上 |
| 3 | 执行器无 `input.sql` → 走 `build_executor_params` 最终兜底 `SELECT * FROM {table} LIMIT 100` | `nodes.py:482` |
| 4 | `table` 来自 `_first_table()` → `schema_search.tables[0]` | `nodes.py:204-207` |
| 5 | `tables[0]` 是 **`insp.get_table_names()` 的字典序第一张** = `dim_channel`（3 行维表） | `schema_tool.py:30/44-48`；两个样例库实测均如此 |
| 6 | 于是**每个 SQL 步骤都执行同一条 `SELECT * FROM dim_channel LIMIT 100`** | 审计：`q_revenue_diag` 6/6、`q_channel_trend` 2/2、`q_region_top` 1/1 |
| 7 | 它**返回 3 行、不报错、记 SUCCESS** | 同上 |

**后果有两层，第二层更严重：**

- **浅层**：分析拿不到任何真证据（`findings=0` 的用例 4 条）——
  `q_revenue_diag` 的报告自己写着「本次分析未能完成……6 次查询全部返回 `dim_channel` 维表数据」。
- **深层**：`q_region_top` **凭空造出一整张区域营收表**（华东 1,245,000 / +18.5% / 32%），
  实际只跑了一条 dim_channel 查询，**却判 ✅**。

`工具成功率` 只证明"SQL 执行了"，**从不证明"这一步的目标达成了"**——
这是 D38 的形态（响亮失败 → 无声错误答案）**高了一层**：
D38 是"报错了却记成功"，这里是"没报错、但答的不是被问的问题"。

**为什么 planner 不给 SQL**：`planner.md` 的 Plan Step JSON schema
（第 120-130 行）**根本没有 `input` 字段**——模型没被要求过，也没被告知
可以这么做。且规划时 planner **看不到 schema**（`business_semantics` 只给维表取值），
所以它**想写也无从写起**。

---

## 2. 契约

### 2.1 选表：`_pick_table`（确定性，唯一口径）

新增 `_pick_table(state, extra_text="") -> tuple[str, list[str]]`，
`_first_table` 改为**委托**它（保留旧签名与调用点，行为随之改善）。

候选 = 最近一次**成功**的 `schema_search` 的 `tables`（ATTACH/01 已把上传表排在前面）；
无 `schema_search` 结果时退回上传表；两者皆无 → `("", [])`。

**上传优先是结构性的，不是加分项**：候选里只要存在 `match == "user_upload"` 的表，
候选集就**收缩到上传表**——与 `run_planner` 已有的
`data_source_priority`（"必须优先且只使用 `upload.<table>`"）同一条政策。
且此时**不适用**下面的 fail-closed：用户就传了一份数据，"该查哪张表"不成为选择，
全 0 分时取候选序第一张（确定性），**绝不因为打分失败就不查用户自己的数据**。

对每个候选表按**加减分**排序（分数相同取候选序在前者——列表序已含"上传优先"语义）：

| 信号 | 分 | 理由 |
|---|---|---|
| **词面**：intent 词（ASCII、长度 ≥3）出现在表名或任一列名中 | **+3** | 问题里说了 `region`，就该优先含 `region` 的表 |
| **度量**：存在列命中 `_is_numeric_col` | **+2** | 没有可聚合的度量列，**根本答不了分析问题**——这是维表与事实表的本质差别 |
| **列清单缺失**（空 `columns`） | **+1** | 「无从判断」≠「判断为否」。可用，但排在"已知可用"之后；`+1` 盖不过 `+2`/`+3`，故不会复活"盲取 `tables[0]`" |
| **规模**：`row_count` 已知且 `>= 100` | **+1** | 3 行的维表支撑不了"趋势/对比" |
| **维表**：表名形如 `dim_*` / `*_dim` / `*_lookup` / `*_ref`，**且**存在其它得分为正的候选 | **−3** | 命名是**加分项的补充**，不是唯一依据（见 §2.2） |

- intent 词 = `extra_text`（步骤的 `objective` + `action`）+ `user_query`
  + `context.objective` + `context.metrics` + `context.dimensions`；
  只取 ASCII 词（中文词对英文 schema 零信息量，且会把中文表名/列名的库误伤）。
- **胜者分 `<= 0` 且候选 `>= 2` → `("", [])`** → 调用方按"合成不出真实 SQL"
  **响亮失败**（`{"sql": ""}`，沿用 D38 后的既有约定）。

**边界一：候选只有一张时照用（不 fail-closed）**——没有第二个选项，
就没有"选错"这回事，与"上传优先"是同一条道理（只给了一份数据）。
这一条是**全量回归逼出来的、不是设计时的推演**：`tests/test_export.py` 的夹具
建的是单表 `c(id INTEGER, phone TEXT)`（**无度量列**，全是 ID/文本列），
而"导出脱敏值"这条链路本就不需要聚合列。一刀切 fail-closed 会让它整条断掉
（实测：`test_export` `IndexError` + `test_dlp` `AssertionError`）。
而本卡要堵的 bug **必须有 ≥2 个候选**才复现（字典序第一张 vs 真正该查的那张），
故收窄 fail-closed 的适用面**不削弱**修复。
"一个结果都没有"（无 `schema_search`、无上传表）仍然响亮失败——那是另一回事。

**边界二：`dataset_profile` 与 `sql_query` 共用这一套打分**，
但"没有度量列"只对**取数**是致命的——数据**质量**画像（候选键、重复行、
日期连续性）在维表上同样有意义。本卡不为此引入第二套打分：`+1` 的"列清单缺失"
分支已经保证极端情况下判不死，而真实 schema 一律带列清单，
`dim_*` 照样因"无度量列 + 词面不命中 + 行数不足"拿 0 分——**行为正确，
不是靠给 profile 开后门**。

**为什么用"度量列"当硬信号**：两个样例库实测，`dim_*` 全部无度量列、
`fact_*` 全部有——它干净地把"能算的表"与"只能当连接目标的表"分开，
且**不依赖行数**（行数是会变的数据属性，度量列是结构属性）。

**一处必须说清的边界**：`dataset_profile` 与 `sql_query` 共用这一套打分，
但"没有度量列"只对**取数**是致命的——数据**质量**画像（候选键、重复行、
日期连续性）在维表上同样有意义。本卡不为此引入第二套打分：`+1` 的"列清单缺失"
分支已经保证极端情况下判不死，而真实 schema 一律带列清单，
`dim_*` 照样因"无度量列 + 词面不命中 + 行数不足"拿 0 分——**行为正确，
不是靠给 profile 开后门**。

### 2.2 为什么"维表惩罚"要带条件

单看命名会把**名为 `dim_x` 的事实表**压掉。故命名只作**相对**信号：
**只有当另一个候选已经得正分时才扣**。没有别的候选可挑时，
`dim_x` 仍是唯一的可用表，照用不误——**宁可给出真实的维表数据，
也不虚构一个"更合适"的表**。

### 2.3 维度列匹配放宽（同一处修复）

`nodes.py:469` 现为 `dim = next((d for d in ctx.dimensions if d in cols), None)`
——**精确列表成员**匹配，于是 `dimensions=["region"]` 与列 `region_id`
**永不相等** → `dim=None` → 退化成 `SELECT *`（真实基线的 `SELECT *` 正是这么来的）。

改为**两段**：先精确命中（既有行为），否则按"列名以 `{d}_` 开头 / 含 `{d}`"
匹配。合成 SQL 从 `SELECT *` 变成真正的分组聚合。

### 2.4 planner：把 `input` 写进 schema，并在重规划时喂 schema

1. **`planner.md` 的 Plan Step schema 增加 `input` 字段**，并写明：
   - `sql_query` / `freeform` 步骤**必须**给出 `input.sql`（完整、只读、可执行）；
   - 上下文里**有** `discovered_schema` 时，必须**照它**写 SQL（表名/列名不得臆造）；
   - 上下文里**没有** schema 时，先排 `schema_search`，**不要**凭空写表名。
2. **`run_planner` 注入 `discovered_schema`**：从 `state.tool_results` 里最近一次
   成功的 `schema_search` 取紧凑 schema 摘要（表名 + 列名 + 行数，有界），
   放进 `task_context`。重规划时 planner 第一次真正看得见 schema。

> **边界（不夸大）**：这一条是**提示纪律**，只提高"模型写对 SQL"的概率，
> **不构成保证**。保证在 §2.1——**选错表也不会再落到任意表上**。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| **不再盲取 tables[0]** | schema_search 返回 `[dim_channel, dim_product, dim_region, fact_sales]` 时，选中的**不是** `dim_channel` |
| **度量优先** | 同为候选时，有度量列的表胜出（构造"维表在前、事实表在后"的序，仍选事实表） |
| **上传优先不被破坏** | 上传表（`match=user_upload`）存在时**候选收缩到上传表**，即使它得分为 0（用户只有这一份数据）；ATTACH/01 不回归 |
| **fail-closed** | 候选 ≥2 且全为 0 分 → 返回 `("", [])` → `build_executor_params` 产出 `{"sql": ""}` → 该步 **FAILED**，绝不 `SELECT 1`、绝不 `SELECT *` |
| **单候选照用** | 候选**恰好一张**（哪怕 0 分）→ 照用它；`test_export` / `test_dlp` 的单表夹具不回归 |
| **无候选仍失败** | 无 `schema_search`、无上传表 → `("", [])`（与"单候选"是两回事） |
| **命名不误伤** | 只有一张 `dim_x` 候选时**仍然选它**（条件性惩罚） |
| **维度列放宽** | `dimensions=["region"]` + 列 `region_id` → 合成 SQL 含 `GROUP BY`（不再是 `SELECT *`） |
| **prompt 契约** | `planner.md` 的步骤 schema 含 `input`；含"必须给 `input.sql`"的硬要求（源码级断言） |
| **schema 注入** | 有成功 `schema_search` 时 `run_planner` 的 `task_context` 含 `discovered_schema`，且**只含表名/列名/行数**（不夹带数据行——脱敏纪律）；无 schema_search 时**不出现**该键 |
| **不回归** | `test_no_placeholder_sql` / `test_schema_fallback` / `test_executor_autodiscover` / `test_attachment_routing` / `test_e2_freeform_exec` / `test_e4_profile_quality` 全绿；离线全量无新增失败 |

## 4. 明确不做

- **让执行器再调一次 LLM 写 SQL**：那是新增一次生成式调用，且把"计划与执行不符"
  藏进又一次不可测的模型输出里。本卡只做确定性选表。
- **强制重规划直到 planner 给出 SQL**：会把"模型没听提示"放大成整跑失败，
  且与 `MAX_REPLANS` 的既有语义冲突。
- **改 `schema_search` 的返回顺序**：`tables[0]` 的语义被其它调用点依赖，
  且在 schema_tool 里排序会让"发现"与"选择"职责混淆。选择留在执行器。
