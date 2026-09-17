# E2-04 · 把「引擎是什么」在**写之前**告诉 planner

> 2026-09-15（D56）· 动因：D55 只做了**检测**侧，没做**预防**侧

---

## 1. 断在哪

D54 §四 当时提了两个抓手：

| # | 抓手 | 状态 |
|---|---|---|
| ① | 执行器在 SQL 失败时把**方言提示 + 真实列清单**回灌给 replan | ✅ D55 做了（`E2/03`） |
| ② | **planner 提示词里明确「执行引擎是 SQLite，禁止 `DATE_TRUNC`/`DATE_FORMAT`/`INTERVAL`」** | ❌ **没做** |

现状核实：`grep -ni "sqlite\|DATE_TRUNC\|方言" app/core/prompts/data_analyst/planner.md`
→ **零命中**。planner **完全不知道**自己要写的是哪个引擎的 SQL。

于是真实基线里的链路是：

```
模型按 Postgres 习惯写出 DATE_TRUNC('month', sale_date)
  → 执行器预检抓下 → 该步 FAILED（未执行）
  → REPLAN（模型看到提示，重写）
  → 再执行
```

**每一轮都要先失败一次。** 这不是"预检没用"——预检保证了**不会静默跑错**；
问题是它**只治已发生的**。D55 的修法是"错了就拦住并说清楚"，
缺的是"**别一上来就写错**"。

而 prompt 侧的成本极低：模型**不是**不会写 SQLite 方言，
它是**默认按训练语料里最常见的方言写**（Postgres/MySQL）。
一句先验就能把绝大多数第一版 SQL 拉到正确方言上。

### 1.1 但**绝不能**在提示词里硬编码 "SQLite"

这是本卡**最关键的一条**，也是 D55 已经踩过并明确防住的那条：

> `E2/03` 的设计纪律：**方言提示必须看引擎**。E7 多源下 `input.source` 可能指向真 PG 库，
> 那里 `DATE_TRUNC` 是**原生**写法——不分引擎地"纠错"会**拦掉正确的 SQL**。

提示词是与预检**同一个判断**的另一个出口。如果提示词里写死"禁止 `DATE_TRUNC`"，
那么在 PG 源上：

* 模型**被 prompt 禁止**用原生写法（明明能用）→ 写出别扭的替代品或干脆放弃日期聚合；
* 而预检**不会拦**（它按引擎分级、对 PG 返回 `[]`）→ **两边口径相反**。

**提示词与预检必须是同一条知识的两面。** 因此：

* 引擎信息**从配置读**（`core/tools/datasource.sources()` → `dialect`），不写死；
* 引擎**判不出来就不说**（而不是猜一个）。

---

## 2. 契约

### 2.1 `sql_precheck.dialect_brief(engine) -> str`：先验，与 `dialect_hints` 同源

```python
def dialect_brief(engine: str | None) -> str:
    """该引擎的**方言要点**（给 planner 的先验）。未知引擎 → `""`。"""
```

* 与 `dialect_hints` **放同一个模块**：那里已经存着「SQLite 没有 `DATE_TRUNC`，
  改用 `strftime(...)`」这条知识。写第二份文本 = 将来必然漂移（改了一处忘了另一处）。
* `unknown` → `""`（**不猜**）。调用方按"键不存在"处理。

### 2.2 措辞必须**分方向**，不能只说"禁止"

| 引擎 | 要点 |
|---|---|
| `sqlite` | 日期用 `strftime()`/`date()`；**不要** `DATE_TRUNC`/`DATE_FORMAT`/`INTERVAL`；标识符用**双引号**（不用反引号）；类型转换 `CAST(x AS type)`（不用 `x::type`）；当天用 `date('now')` |
| `postgres` | `DATE_TRUNC`/`INTERVAL`/`x::type` **是原生写法，可以用**；标识符双引号 |
| `mysql` | `DATE_FORMAT`/`INTERVAL` **是原生写法，可以用**；标识符反引号 |
| `unknown` | `""` |

**PG/MySQL 也要给**：只给 SQLite 的"禁令清单"等于**换个方向**犯同一个错
（在 PG 源上把原生写法当错误）。

### 2.3 `nodes._dialect_text(state) -> str`：把**配置里真实的**引擎交给 planner

输出**每源一行**（源名 + 方言 + 要点）：

```
- default（主源）: sqlite — 用 strftime()/date() ...
- pg_warehouse: postgres — DATE_TRUNC/INTERVAL/:: 是原生写法 ...
```

**三条硬约束**：

1. **绝不回 DSN**。`sources()` 返回 `{"url": ..., "dialect": ...}`——
   只取 `dialect`，名字取自 `available_sources()`。
   （与 `available_sources()` 的既有约定一致："只有名字，绝不回 DSN"。）
2. **绝不抛**。读配置失败 → 返回 `""` → 调用方不注入键，planner 照常工作。
3. **有界**。源数量与每行长度都设上限（prompt 预算子系统存在，不能让它无限膨胀）。

### 2.4 `run_planner` 注入 `task_context["sql_dialect"]`

与 `discovered_schema` **同一个模式**：拿不到就**不注入空壳键**（E2/02 的既有纪律——
给了空内容模型反而会照着编）。

### 2.5 `planner.md` 新增一节：写之前就对准引擎

位于 `# Step Input`（讲 `input.sql`）之后——**同一件事的两个时刻**：
先对准引擎，再写语句。内容要求：

* 指认 `<task_context>.sql_dialect`；按 `input.source` 选源，未指定则按主源；
* **键不存在时**写**最保守**的 SQL（避开 `DATE_TRUNC`/`DATE_FORMAT`/`INTERVAL`/`x::type`，
  用双引号标识符 + `CAST`）；
* **方言不是编造表列名的借口**——列名仍只能来自 `discovered_schema`
  （防止模型把"我在写 SQLite"理解成"我可以自己发挥"）；
* 只对 **SQL** 步骤生效（`python_analysis` 与方言无关）。

---

## 3. 验收

| 项 | 判据 |
|---|---|
| 分引擎 | `dialect_brief("sqlite")` 含 `strftime`、`双引号`、`CAST`；`dialect_brief("postgres")` **明确说** `DATE_TRUNC` 可用；`dialect_brief("mysql")` 说 `DATE_FORMAT` 可用 |
| 未知不猜 | `dialect_brief("unknown")` == `""`；`dialect_brief(None)` == `""` |
| 与预检同源 | 预检抓得到的每个构造（`DATE_TRUNC`/`DATE_FORMAT`/`INTERVAL`/`CURRENT_DATE`/`::`），`dialect_brief("sqlite")` **都点名**（单向漂移守卫） |
| 建议不自相矛盾 | `dialect_brief("sqlite")` 推荐的写法（`strftime`/`CAST`）**不被自己的预检拦下**：`dialect_hints("SELECT strftime('%Y-%m', d) FROM t", engine="sqlite") == []` |
| 注入 | `run_planner` 的 payload 含 `sql_dialect`，且含 `sqlite` |
| **不泄 DSN** | payload **不含** `sqlite:///`、`data/sample_enterprise.db`（只给源名与方言） |
| 多源 | 配置里加一个 pg 源 → payload 同时列出两源、且各自的要点**方向相反**（PG 那行说 `DATE_TRUNC` 可用） |
| 读配置失败 | `sources()` 抛异常 → 不注入 `sql_dialect`，**planner 不崩** |
| 无引擎可用 | 引擎为 `unknown` → 不注入键（**不是**注入空串） |
| 不回归 | D54 的 `discovered_schema` 注入与 E2/02 的断言全绿 |

## 4. 明确不做

- **不在提示词里硬编码 "SQLite"**（§1.1，本卡的核心纪律）。
- **不改 `dialect_hints` / 预检的拦截行为**：本卡只**加先验**，不动**门**。
  两者是"预防"与"检测"，替代关系不成立——预检必须留着（模型仍可能不遵守，
  且它现在还负责**真实列清单回灌**）。
- **不做自动方言改写**（`E2/03` §4 已明确排除，本卡不动）。
- **不给 `python_analysis` 注入方言**：Python 里连的是哪个库由它自己管，
  与 SQL 方言先验不是一回事（要动是另一张卡）。

## 5. 回填（2026-09-15 实施）

**改了四处，全部离线可证**：

| # | 位置 | 内容 |
|---|---|---|
| 1 | `sql_precheck.py` | `dialect_brief(engine)` + `_SQLITE_BRIEF`/`_PG_BRIEF`/`_MYSQL_BRIEF`/`_DIALECT_BRIEFS` |
| 2 | `sql_precheck.py` | `_ENGINE_NOTE` 补 `CURRENT_DATE`（原先漏了，而预检**抓得到**它——见下） |
| 3 | `nodes.py` | `_dialect_text(state)` + `_MAX_SOURCES_IN_PROMPT = 8`；`run_planner` 注入 `task_context["sql_dialect"]` |
| 4 | `planner.md` | 新增 `# SQL Dialect — write the SQL your engine actually runs`（置于 `# Step Input` 之后） |

**`_ENGINE_NOTE` 漏了 `CURRENT_DATE` 是本卡顺手挖出来的既有漂移**：
预检的 `_CURRENT_RE` 一直**抓得到** `CURRENT_DATE`，但错误提示里从没提过它——
模型只能在"被拦"和"被引导"之间反复。这正是 §2.1 "同源"要防的那类问题，
只是**它已经发生在检测侧内部**。现在两处共用同一句文本，不可能再分叉。

**新增用例 25 条**（`tests/test_sql_dialect_prior.py`）：分引擎措辞 3 条、
未知引擎不猜 6 条（参数化）、**单向漂移守卫 5 条**（预检抓得到的构造先验必须点名）、
"建议不自相矛盾" 1 条、`_dialect_text` 5 条（读配置 / 不泄 DSN / fail-open / 多源反向 / 有界）、
接线 5 条。

**回归**：离线全量 **1242 passed / 32 skipped / 0 failed**（D55 是 1217，差值 **25** = 新增用例数，
一条不多一条不少）。`eval --mode mock --strict` **退出码 0**，各项指标与 D55 基线**逐项相同**
（`pass_rate 1.0`、`tool_success_rate 1.0`、`tool_skipped_total 0`、成本 `未计（未配单价）`）——
mock 的 planner 不读 payload，**先验本来就不该在这里产生差异**。

### 5.1 实施中被用例挡下的两处（都是真的会误导模型）

**(1) 先验必须是单行。** 第一版 `dialect_brief` 返回**多行块**，`_dialect_text` 拼出来是
"源名单独一行 + 下面几条缩进要点"。用例红在
`assert 'DATE_TRUNC' in pg_line`（`pg_warehouse` 那一行只有块标题）。
`text.count("\n") < 30` 也红（8 源 × 4 行 = 31）。

教训不只是"超预算"：**多行块在一行一条的列表里会把「归属」丢掉**。
模型读到的是一串**看不出归哪个源**的要点，而 SQLite 与 PG 的要点**恰恰相反**——
读错归属比不读更糟。§2.3 写的"每源一行"是契约，不是格式偏好。

**(2) 否定式提及仍是提及。** 我第一版在 PG 那行写了
"…**都是原生写法，可以直接用**，不要绕成 `strftime()`"。用例红在
`assert "strftime" not in pg_line`。

这条用例的判据是对的：在 **PG 的行里出现 `strftime`，就是在教模型对 PG 用 `strftime`**——
"不要"这个前缀管不住它的适用范围。**禁令要少、要具体，且绝不在错误的上下文里出现那个词。**
（删掉那半句后，PG 一行的信息量没有减少：说清"原生可用"就够了。）

---

## 6. 待真实验证

**"给了先验之后，模型第一版 SQL 的正确率是否真的提高"**——
这是一次**模型行为**，离线只能钉到"prompt 里确实带了这条先验、且方向正确"。
判据（下次 `eval --mode real`）：

* 审计里 `sql_query` 的 SUCCESS 率（D54 是 **3/20**）是否上升；
* 失败原因里**方言错**的条数是否下降（若"编造 schema"占比上升，说明先验生效但列纪律仍弱）；
* 计划仍 **35/35** 自带 `input.sql`（D54 的成果不得回退）。

**`[待真实验证]`**
