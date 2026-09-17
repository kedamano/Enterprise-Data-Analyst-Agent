# E2-03 · SQL 预检：让"写错了"在**执行前**变成一句人话

> 2026-09-15（D55）· 动因：D54 修完后的真实基线（`eval-real-20260915-d54.md`）

---

## 1. 断在哪

D54 让 planner **真的开始写 SQL** 了（35/35 步自带 `input.sql`，实测），
于是第一次看到了"自由写码"的真实水平：**审计里 20 次 `sql_query` 只有 3 次 SUCCESS**。失败分两类：

| 类 | 实据（真实基线原样） | 现在发生什么 |
|---|---|---|
| **方言错** | `no such function: DATE_TRUNC`、`near "'1 month'": syntax error`、`near "3": syntax error`（`DATE_FORMAT(s.order_date, '%Y-%m')`） | 模型默认在写 **Postgres / MySQL**，而引擎是 **SQLite** |
| **编造 schema** | `no such column: f.order_id` / `fs.customer_id` / `order_date` / `s.visitor_uv`、`no such table: fact_traffic` | 库里没有这些表列，**而 `fact_sales` 的真实列就在上一轮的 `schema_search` 结果里躺着** |

**关键点：planner 提示词里早就写了「use exactly those table and column names. Never invent」**
（`planner.md` §Step Input），而模型**已经知道真实列**——`context.assumptions` 里它自己写着
"`fact_sales` 表的 `orders` 和 `customers` 字段为预聚合固定值 1"。它**看过真 schema，生成 SQL 时仍然编**。

→ **这条不能靠提示词修**（D54 已经证明"不是不听、是没被要求过"；这次是"要求了也不稳定遵守"）。
要在**执行器**上做确定性的事。

### 1.1 两个附带发现（同一条链上的真缺陷）

- **失败后的重试是空转**：`run_executor` 有 `is_schema_error → schema_search → __retry` 分支，
  但 `build_executor_params` 对带 `input.sql` 的步骤**原样返回同一条 SQL**（`nodes.py:641-650`），
  于是 `__retry` 执行的是**逐字相同**的错误语句 → 必然再失败一次。
  这个分支只对"合成 SQL"路径有效（那里 `_first_table` 会因新 schema 而重选表）。
- **错误信息不可操作**：模型拿到的是 `(sqlite3.OperationalError) near "'1 month'": syntax error`。
  它得自己猜到是方言问题、且得自己回忆起真实列名——**两条都要求它已经知道答案**。

---

## 2. 契约

新增纯模块 `app/core/agents/data_analyst/sql_precheck.py`。

### 2.1 方言预检 `dialect_hints(sql, *, engine) -> list[str]`

**只在 `engine == "sqlite"` 时生效**（E7 支持按 `input.source` 指定 PG/MySQL 源，
对真 PG 源提示"别用 `DATE_TRUNC`"是**错的**——那是它的原生函数）。

识别（**精确构造**，不做泛化解析，宁少不误）：

| 构造 | SQLite 等价 |
|---|---|
| `DATE_TRUNC('unit', x)` | `strftime(<fmt>, x)`，`unit` 映射见下 |
| `DATE_FORMAT(x, '%Y-%m')` | `strftime('%Y-%m', x)` |
| `x - INTERVAL '3 months'` / `INTERVAL '1 month'` | `date(x, '-3 months')` |
| `CURRENT_DATE` / `CURRENT_TIMESTAMP` | `date('now')` / `datetime('now')` |
| `SLEEP(` / `BENCHMARK(` | 无关方言，直接点名（工具侧已拒） |
| `x::type` | `CAST(x AS type)` |
| 反引号 `` `col` `` | 双引号 `"col"` |

单位映射：`year|month|week|day|hour|minute|second` → `%Y|%m|%W|%d|%H|%M|%S`
（`week` 用 `%W`——**与 Postgres 的 ISO 周不严格等价**，
规格明写：这是**提示**不是保证，见 §4）。

### 2.2 schema 预检 `unknown_references(sql, schema) -> (tables, columns)`

`schema: dict[str, list[str]]`（表名 → 真实列名），由 `known_schema(state)` 从
**最近一次成功的 `schema_search`** 结果构造（复用 `_last_result(state, "schema_search")` 的口径）。

- **表**：只取 `FROM` / `JOIN` 之后紧跟的标识符（**含 `upload.` 前缀**）；
  与 `schema` 的键比对（大小写不敏感）。子查询 `FROM (` 不计。
- **列**：只取**带限定符**的 `alias.column` 形式，且该 `alias` 能在同一语句里解析成
  一张**已知表**（`FROM/JOIN x [AS] alias`）。**裸列名一律不判**
  （无法区分列、别名、函数名、字符串）——**这条是防假红的核心**，
  见 §4「明确不做」。

### 2.3 接线：预检**只在执行前拦方言**，schema 只在**失败后**补消息

| 时机 | 动作 | 理由 |
|---|---|---|
| 执行前 | 方言预检**非空** → **不执行**，返回 `FAILED`，消息 = 方言提示 + 原始 SQL | 明知 SQLite 不认，白跑一次还只得一句 `syntax error` |
| 执行前 | schema 预检 → **只记录，不拦** | 表名提取有误判风险；把有效 SQL 拦掉（假红）比多跑一次贵得多 |
| 执行后失败且 `is_schema_error` | **保留 `schema_search` 恢复**，**跳过逐字重跑** | 恢复搜索结果喂给 planner 的 `discovered_schema`（有用）；重跑同一句（无用） |
| 执行后失败 | 把 schema 预检结果**追加**到 `error` 末尾 | 模型看到的是 `no such column: f.order_id；fact_sales 的真实列: sale_id, sale_date, …` |

**错误消息格式**（`format_error`）：

```text
列 `order_id` 不存在（引擎 SQLite）。
`fact_sales` 的真实列：sale_id, sale_date, region_id, product_id, channel_id, revenue, orders, customers
提示：本库是 SQLite；日期请用 strftime/date，不要用 DATE_TRUNC/DATE_FORMAT/INTERVAL。
```

—— 一句话里给全**三件它缺的事**：哪个标识符错、真实列是什么、引擎是什么。

### 2.4 复用面

`precheck` 是**纯函数**（不碰 state、不碰 DB、不抛异常），
故可被 `run_executor` / `_execute_one_step`（并发路径）同时调用，且可离线单测。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| **方言：`DATE_TRUNC`** | `dialect_hints("SELECT DATE_TRUNC('month', d) FROM t", engine="sqlite")` 非空且提示含 `strftime` |
| **方言：`INTERVAL`** | `- INTERVAL '3 months'` → 提示含 `date(`；`DATE_FORMAT(x,'%Y-%m')` → 含 `strftime` |
| **方言：引擎正确时不误报** | 同一句 SQL，`engine="postgres"` → **返回 `[]`**（那是 PG 的原生写法） |
| **方言：正常 SQL 不误报** | `SELECT region_id, SUM(revenue) FROM fact_sales GROUP BY 1` → `[]` |
| **方言：真拦** | 带 `DATE_TRUNC` 的步骤**不执行**（工具调用计数不增），`status=FAILED` 且 `error` 含方言提示 |
| **schema：未知表/列被点名** | `f.order_id`（`f` = `fact_sales`）→ columns 含 `order_id`；`FROM fact_traffic` → tables 含 `fact_traffic` |
| **schema：不误报** | 裸列 `order_id`（无别名）**不判**；`upload.t` 前缀表名按边车库表判定；CTE 名 `WITH x AS (...)` 不当未知表 |
| **schema：别名解析** | `FROM fact_sales s` 与 `FROM fact_sales AS s` 都能把 `s.` 解析到 `fact_sales` |
| **schema：未知时不拦** | schema 为空（还没发现过）→ 预检**通过**，SQL 照常执行（不因"不知道"而拒） |
| **失败后补消息** | 触发 `no such column` 的步骤，其 `error` 末尾含真实列清单 |
| **失败后不空转** | 带 `input.sql` 的步骤不做逐字 `__retry`（无 `*__retry` 结果），但**仍然**做 `schema_search` 恢复 |
| **合成路径不受影响** | 无 `input.sql` 的步骤仍走原 `__retry` 重选表路径 |
| **纯函数** | 空 SQL / `None` / 坏 schema → 不抛，返回空结果 |

## 4. 明确不做

- **不做自动方言改写后重试**。改写要动语义（`DATE_TRUNC('week')` 的周边界、`DATE_FORMAT`
  的 locale），而 SQLite 与 PG 的等价性**不是逐字可判的**；悄悄改一条 SQL 再去跑，
  等于把"模型写错了"变成"系统替它猜"——**宁可响亮失败一次，让它自己重写**。
  （候选：若真实基线显示"给了提示仍写不对"，再谈改写。）
- **不做通用 SQL 解析**（不引 `sqlglot`）。只做**能证明其正确性**的窄识别；
  裸列名/子查询/`USING` 连接一律不判——**误报的代价是拦掉正确 SQL**，
  远高于漏报（漏报只是退化回现在的行为）。
- **不把预检接进 `sql_tool` 本身**。工具只管"能不能安全地执行"；
  "这条 SQL 是否可能写错了"是**编排层**的判断（它还依赖 state 里的 schema）。

---

## 5. 回填（2026-09-15 实施）

- **用例 26 条全绿**；全量离线回归 **1217 passed / 32 skipped / 0 failed**。
- **接线顺带合掉了两份逐字复制的分支**：`run_executor` 与 `_execute_one_step`
  原先各自维护一份"依赖检查 → 执行 → 恢复/重试"，D54 的改动只落在其中一份上。
  现在共用 `_run_one_step`，**结构上不可能再漂移**（这是"改一处忘另一处"的根因，不只是本次的便利）。
- **`_preflight_sql_error` 失败不算 `skipped`**：那不是"没轮到"，是这一步自己写错了——
  它必须留在工具成功率的分母里（见 `E6/03`）。
- **`[待真实验证]`**：预检能拦（离线可证），但"回灌方言提示与真实列清单之后模型是否改对"
  是一次**模型行为**。离线只能钉到"错误消息里确实带了这些内容"
  （`test_failure_message_carries_the_real_columns`）。
- **本卡没有消耗真模型**：全部结论来自既有真实产物 + 离线用例。
