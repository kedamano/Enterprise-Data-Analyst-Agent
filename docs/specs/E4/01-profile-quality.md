# E4/01 dataset_profile 质量基元 — 规格 v1.0（D19 定稿）

> 痛点：分析师拿到一张表/一个结果集，最先要问的四件事是——
> **主键唯不唯一？join 有没有放大？一行代表什么粒度？日期连不连续？**
> 现状 `dataset_profile` 只给 `row_count` + 每列 `null_count/null_ratio/distinct`，
> 这四问全部答不了；而它们恰恰是"分析结论翻车"的最常见来源（重复计数、放大后求和、粒度误读、稀疏日期当连续）。
> 另修一个既有隐患：当前实现把 `table` 直接 f-string 拼进 SQL。

## 1. 参数契约

| 参数 | 必需 | 说明 |
|---|---|---|
| `table` | 二选一 | 单表画像。标识符白名单校验（`^[A-Za-z_][A-Za-z0-9_]*$`） |
| `sql` | 二选一 | **只读**查询结果画像（复用 `sql_tool` 的只读守卫；结果作为子查询包一层） |
| `key` | 否 | 声明键：字符串或列表（支持联合键），用于唯一性校验 |
| `base_table` | 否 | 与 `sql` 搭配：计算 join 放大倍数 |
| `date_column` | 否 | 日期列；缺省时自动探测（列名含 `date`/`time`/`日期`，或值可解析为日期） |

`table` 与 `sql` 同时给 → 以 `sql` 为准。均缺省 → `ok=false, error="缺少 table 或 sql 参数"`。

## 2. 输出契约（在现有 `row_count` / `columns` 之上**新增**，保持向后兼容）

```jsonc
{
  "ok": true, "table": "fact_sales", "row_count": 3120,
  "columns": { "sale_id": {"null_count": 0, "null_ratio": 0.0, "distinct": 3120}, ... },

  "key_uniqueness": {
    "candidate_keys": ["sale_id", "revenue"],  // 单列满足 nulls==0 且 distinct==row_count（按列序）
    "likely_key": "sale_id",         // 候选里更像业务键者（见 §3），否则 null
    "declared_key":   ["sale_id"],   // 入参 key，否则 [likely_key]，否则 []
    "is_unique": true,               // 无键可判 → null（不是 false）
    "duplicate_rows": 0,             // row_count - COUNT(DISTINCT key…)
    "duplicate_ratio": 0.0,
    "grain": "row"                   // row | aggregated | unknown
  },

  "join_amplification": {            // 仅当 sql + base_table
    "base_table": "fact_sales", "base_rows": 3120,
    "result_rows": 1946880, "factor": 624.0,
    "amplified": true, "threshold": 1.5
  },

  "date_continuity": {               // 仅当探测到日期列
    "column": "sale_date",
    "min": "2024-01-01", "max": "2024-12-23",
    "distinct_days": 52, "expected_days": 358, "missing_days": 306,
    "coverage_ratio": 0.145, "sparse": true,
    "gap_samples": ["2024-01-02", "2024-01-03", "2024-01-04"]   // ≤3 个
  }
}
```

## 3. 判定规则（确定性，可测）

- **候选键**：单列 `null_count == 0` **且** `distinct == row_count`。不做多列组合穷举（组合键由 `key` 显式声明）。
  - 注意"唯一"是**弱信号**：样例库的 `revenue` 是浮点度量，也恰好 3120/3120 唯一。
    故候选按**列序**全量列出（透明、无魔法），另给 `likely_key` 供下游优先使用：
    候选里第一个"名字像键"（`^[a-z_]*id$` / `_key$` / `_no$`）且**非浮点度量**的列；没有则 `null`。
  - `likely_key` 只是**启发式排序**，不改变 `candidate_keys` 的内容。
- **`grain`**（描述**结果集**粒度）：**看能否指出一个键**，而非"有没有唯一列"——
  `declared_key` 非空 → `row`；否则 `row_count > 0` → `aggregated`；`row_count == 0` → `unknown`。
  > 实现修正（D20）：原规则"有任一候选键 → row"会误报——实测
  > `GROUP BY region_id, product_id` 的 20 组里 `SUM(revenue)` 恰好 20 个不同值，
  > 于是被当成"行粒度"，而它明明是聚合结果。改为以 `declared_key`（= 显式 `key`，或
  > `likely_key`）为准后语义正确：该例 `declared_key == []` → `aggregated`。
- **`declared_key` 为空 ≠ 没有唯一列**：`candidate_keys` 仍如实列出（透明），
  只是不把它当作可用的行键——这正是"唯一是弱信号"的落地方式。
- **declared 唯一性**：`COUNT(DISTINCT key…)` vs `row_count`；`duplicate_rows = row_count - distinct_key`；
  `is_unique = duplicate_rows == 0`。联合键用子查询 `COUNT(*)` vs `COUNT(*) FROM (SELECT DISTINCT k1,k2 …)`（跨方言安全，避免 `||` 拼接的类型陷阱）。未给 `key` 且无候选键 → `is_unique = null`。
- **join 放大**：`factor = result_rows / base_rows`；`base_rows == 0` → `factor = null`；
  `amplified = factor >= threshold`，阈值取 `settings.profile_join_amp_threshold`（默认 **1.5**）。
- **日期连续性**：`expected_days = (max - min).days + 1`；`missing_days = expected_days - distinct_days`；
  `coverage_ratio = distinct_days / expected_days`；`sparse = coverage_ratio < 0.9`；
  `gap_samples` 取区间内前 ≤3 个缺失日期。
- 全部查询**只读**，复用 `sql_tool` 的 `_FORBIDDEN_RE` / `_MULTI_STMT_RE`（E4 抽出共享 `guard_readonly_sql()`）。

## 4. 边界与失败

| 情形 | 行为 |
|---|---|
| 非法标识符（`"fact_sales; DROP TABLE x"`） | `ok=false`，`error` 含"非法标识符"；**绝不拼进 SQL** |
| 表不存在 / SQL 报错 | `ok=false`，`error` 含表名或原始错误（现有行为保留） |
| 空表（`row_count == 0`） | `ok=true`；`candidate_keys=[]`、`grain="unknown"`、`is_unique=null`、日期/放大给 `null` |
| 无日期列 / 日期不可解析 | `date_continuity = null`（不报错） |
| `base_rows == 0` | `factor = null`，`amplified = false` |
| 数据源缺失 | 复用 `dbguard.data_source_error()`，响亮失败 |
| 非 sqlite 方言 | 列表走 `information_schema`（现状保留）；日期函数按方言分支 |
| 大表性能 | 每列一次 `COUNT(DISTINCT)`（与现状同阶）；**不做**组合键穷举；`key` 校验多加 1–2 次聚合查询 |

## 5. 可观测
- `columns` 保留原字段（下游 reflection/报告不破坏）。
- 新增字段缺省 `null` 而非缺键，便于消费方稳定解析。

## 6. TDD（D19 红 → D20 绿）

| 用例 | 断言 |
|---|---|
| 主键唯一（正） | `fact_sales` → `candidate_keys == ["sale_id", "revenue"]`（列序）、`likely_key == "sale_id"`、`declared_key == ["sale_id"]`、`is_unique is True`、`grain == "row"`；`region_id` **不在**候选 |
| 主键唯一（负） | `key=["region_id"]` → `is_unique is False`、`duplicate_rows == 3115`、`duplicate_ratio > 0.99`；`grain` 仍为 `"row"`（结果集粒度，与声明键无关） |
| 联合键 | `key=["sale_date","region_id","product_id","channel_id"]` → `is_unique is True`、`duplicate_rows == 0` |
| 聚合结果粒度 | `sql=GROUP BY region_id, product_id`（20 行，无单列唯一）→ `candidate_keys == []`、`grain == "aggregated"`、`is_unique is None` |
| join 放大 | `sql=<self join on region_id>`, `base_table="fact_sales"` → `factor == 624.0`、`amplified is True` |
| 日期连续性 | `date_column="sale_date"` → `distinct_days == 52`、`missing_days == 306`、`sparse is True`、`gap_samples == ["2024-01-02","2024-01-03","2024-01-04"]` |
| 负：非法标识符 | `table="fact_sales; DROP TABLE dim_region"` → `ok is False` 且 `"非法标识符" in error` |
| 负：表不存在 | `table="no_such_table"` → `ok is False` 且 error 可读 |
| 兼容 | 原有 `row_count` / `columns[c]["null_ratio"]` 字段仍在 |
