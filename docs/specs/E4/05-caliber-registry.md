# E4/05 口径注册表 + 同环比基线自动判定 — 规格 v1.0（D48 定稿）

> 前情：E4/03 `caliber.py` 只做**报告内**结构性口径检查（期间长度/分母/单位/限定词极性/迭代漂移）。
> 它能发现"两期长度不等"，但回答不了两个更基本的问题：
> 1. **环比/同比的基线期间到底应该是哪段？** —— `comparison.period` 经常是空的，
>    `period_mismatch` 只在"两期都解析得出"时才报，基线缺失时反而**静默放过**。
> 2. **这个指标的口径"应该"是什么？** —— 没有一份登记过的基准口径，"偏离了"无从谈起。
>
> D48 补这两件：**口径注册表（CRUD）** + **同环比基线自动判定**，两者都**只收紧**地联动进 `caliber_check`。

---

## 1. 口径注册表（Caliber Registry · CRUD）

### 1.1 模型 `CaliberSpec`
登记一条指标的**基准口径**（人工录入，非 LLM 产出）：

| 字段 | 类型 | 含义 | 例 |
|---|---|---|---|
| `metric` | `str` | 指标名（主键，唯一） | `"营收"` |
| `filters` | `list[str]` | 限定口径 | `["不含退货"]` |
| `unit` | `str` | 标准数量级单位 | `"万元"` |
| `period_type` | `str` | 标准期间粒度 | `"月"` |
| `denominator` | `str` | 比率类分母口径 | `"活跃用户数"` |
| `grain` | `str` | 标准分析粒度 | `"品类"` |
| `notes` | `str` | 口径说明文本 | `"含税、不含退款、按下单时间"` |

### 1.2 后端
- **默认 JSON 文件** `data/caliber_registry.json`（注册表是小而少写的参照数据，不是 append-only 审计流，
  JSONL 不适合 CRUD；JSON 文件 + 原子写（tmp→rename）足够，与项目"宁缺勿滥"一致）。
- **可选 sqlite**（`CALIBER_REGISTRY_DB_URL` 配置时）：多副本共享、可 SQL 查询。
- **不做双写**（沿用 D46 审计落库的纪律——写两处会有"以哪份为准"）。
- CRUD 按 `metric` 主键：`register` 已存在则**覆盖**（幂等），`remove` 不存在不报错。

### 1.3 故障纪律
注册表**读故障不得打断 `caliber_check`**（与 gate/caliber 既有 try/except 同口径）：
读不到注册表 → 跳过 `caliber_deviation` 检查、`logger.warning`、`metadata["caliber_registry_error"]` 留痕。
CRUD 写故障对调用方抛出（CRUD 是显式管理动作，不该吞）。

---

## 2. 同环比基线自动判定

### 2.1 `infer_baseline_period(current_start, current_end, comparison_type)`
**确定性日期算术**，不调 LLM。返回 `(baseline_start, baseline_end, baseline_label, days)` 或 `None`。

- 输入：`time_range.start/end`（ISO `YYYY-MM-DD` 或 `YYYY/MM/DD`）+ `comparison.type`。
- `环比`/`MoM`/`上期`：基线 = 上一段**等长**期间（按 start/end 宽度回平移）。
  例：`2024-03-01 ~ 2024-03-31`（环比）→ `2024-02-01 ~ 2024-02-29`。
- `同比`/`YoY`/`去年`：基线 = 去年**同月/同季**。
  例：`2024-03-01 ~ 2024-03-31`（同比）→ `2023-03-01 ~ 2023-03-31`。
- **解析不出**（start/end 缺失或非日期、type 空）→ `None`（宁缺勿滥，不猜）。
- 期间宽度以 `end - start` 天计；跨年/跨月不规则（如 `2024-01-15 ~ 2024-03-20`）仍按天数平移，
  标注 `label` 为"近 {n} 天的前一段"，**不强求对齐自然月**。

### 2.2 联动 `caliber_check`（新增 issue kind `baseline_mismatch`）

触发条件（**双条件**，与 gate.py 口径一致）：
1. `comparison.type` 命中环比/同比；**且**
2. `comparison.period` 为空，**或** `parse_period_days(period)` 与推断基线天数差 > 20%。

- `period` 空 → `detail`："声明了 {type} 但未给基线期间，应约为 {baseline_label}"。
- `period` 与推断不符 → `detail`："基线期间约 {actual} 天，{type} 应约为 {baseline_label}（{days} 天）"。
- **披露级**（不抬 REPLAN）：与 `period_mismatch`/`denominator_missing` 同级——基线缺失应让人看见，
  但不等于结论错（可能是措辞漏写）。

> 与既有 `period_mismatch` 的边界：`period_mismatch` 判"两期都给了但长度不等"；
> `baseline_mismatch` 判"声明了对比类型但基线缺/错"。两者不重叠（都给了且等长 → 都不报）。

### 2.3 联动 `caliber_check`（新增 issue kind `caliber_deviation`）

触发条件：
1. 报告里出现的某指标**在注册表里登记过**（`metric` 命中）；**且**
2. 从报告文本里**确定性抽出**的该指标口径（单位 / 限定词极性）与登记口径**冲突**：
   - 单位：报告用 `亿元` 但登记 `万元`（数量级不同）；
   - 限定词：报告 `含退货` 但登记 `不含退货`（极性相反）。
- `detail`："指标「营收」报告口径（{actual}）与登记口径（{registered}）冲突"。
- **披露级**。抽不出口径 → 不判（宁缺勿滥，复用 `_amounts_by_metric` / `_QUALIFIER_RE`）。
- 注册表不可达 / 指标未登记 → 跳过，不报。

---

## 3. API

`/api/v1/chat/analyze/caliber`（CRUD 端点，沿用 export/debug 路由范式）：

| 方法 | 路径 | 行为 |
|---|---|---|
| `GET` | `/caliber` | 列出全部登记口径 |
| `GET` | `/caliber?metric=营收` | 取单条（无则 404） |
| `POST` | `/caliber` | 登记/覆盖一条（校验 `metric` 非空）→ 201 |
| `DELETE` | `/caliber?metric=营收` | 删除一条（不存在也 200） |

- 鉴权沿用既有路由层（当前项目路由零鉴权是已知项，本卡不动）。
- 写入是显式管理动作 → 故障对调用方抛 HTTP 500（不吞）。

---

## 4. 边界与诚实纪律

- **只收紧，不放松**：两个新检查均**披露级**，不改 Reflection 决策（`iteration_drift` 仍是唯一抬 REPLAN 的）。
- **解析不出 = None，不报**：基线推断与口径抽取都遵循"宁缺勿滥"，绝不"借"一个来源或猜一个基线。
- **登记口径 ≠ 事实口径**：注册表是人工录入的**期望**，用它发现"报告与期望冲突"，
  不用它证明"报告是对的"——与 lineage 的 `confidence="parsed_from_sql"` 同一条纪律。
- **零 claim / 无对比 → 不判**：与 E1 溯源、D41 幻觉率的"零 → None 不报 0"一致。
- `[待真实验证]`：限定词语义识别（"含退款"这类）仍留 LLM 维度；本卡只做**确定性子集**。

---

## 5. TDD（先红后绿）

`tests/test_caliber_registry.py`（CRUD）：
1. 注册一条 → `get` 命中、`list` 含之。
2. 同名再注册 → 覆盖（幂等，不翻倍）。
3. `remove` → 不在列表；再 `remove` 不报错。
4. 读故障不影响 `caliber_check`（注册表文件不可达 → 跳过 caliber_deviation，不抛）。
5. CRUD 落盘后进程间可见（写→读对账）。

`tests/test_caliber_baseline.py`（基线 + 联动）：
1. `infer_baseline_period` 环比：`2024-03-01~2024-03-31` → `2024-02-01~2024-02-29`。
2. `infer_baseline_period` 同比：→ `2023-03-01~2023-03-31`。
3. `infer_baseline_period` 解析不出 None（start/end 缺 / type 空）。
4. `caliber_check`：声明环比但 `comparison.period` 空 → `baseline_mismatch`。
5. `caliber_check`：环比 + period 与推断基线天数差 >20% → `baseline_mismatch`。
6. `caliber_check`：环比 + period 与推断基线等长 → 不报。
7. `caliber_check`：登记口径 `营收/万元/不含退货`，报告 `营收 1.2 亿元含退货` → `caliber_deviation`。
8. `caliber_check`：登记口径与报告一致 → 不报。
9. 畸形输入不抛（None context / 空 analysis）。
