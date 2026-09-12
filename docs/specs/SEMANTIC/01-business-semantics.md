# SEMANTIC/01 业务语义层 — 规格 v1.0

> 动因：Agent 只看得见**列名**，看不见**含义**。`schema_search` 只返回 `{name, type}`，
> 于是 `region_id=1` 就是数字 1，不是"华东"；`revenue` 是 56789.5，不是"元还是万元"。
> 基准里 53/300 要多表 join、38/300 涉口径澄清——**没有语义，join 只能靠猜键**，
> 而猜错的代价我们已经量过（拿 `region_id` 当 `product_id` 会 5 倍放大，E4/04 会拦，但本不该发生）。
>
> 另：`planner` 现在**完全看不到 schema**（`run_planner` 的 task_context 只有 context + mode）——
> 它凭 LLM 自己从问题里猜维度，而不是看着真实表结构规划。本规格第一次把"数据长什么样"喂给它。

## 1. 采集（确定性、只读、有界、可缓存）

新模块 `app/core/semantics.py`（纯函数 + 有界查询，**不花 LLM**）：

```python
@dataclass
class Semantics:
    relationships: list[dict]   # {from_table, from_col, to_table, to_col, kind, confidence}
    dimensions: dict            # {"region_id": {"table": "dim_region", "key_col": "region_id",
                                #                 "label_col": "region_name",
                                #                 "values": {"1": "华东", ...}}}
    skipped_pii: list[str]      # 因疑似 PII 而跳过的列（审计用）

def is_pii_column(name: str) -> bool
def infer_relationships(tables: list[dict]) -> list[dict]
def collect_semantics(session_id: str = "") -> Semantics
def describe_semantics(sem: Semantics, *, max_dims=6, max_values=8) -> str
```

### 1.1 键与关系推断（按命名约定，因为样例库**没有真实 FK 约束**）
- 维表：表名匹配 `dim_*` **或**含 `*_name`/`name`/`title`/`名称` 列 → 视为维表；
  其 `*_id` 列（或首列）为 `key_col`，首个 name-ish 列为 `label_col`。
- 关系：任意表的列 `x_id` ↔ 维表的键列同名 → `{kind: "many_to_one", confidence: "naming_convention"}`。
  **只按命名约定，不猜语义**；`confidence` 如实标注，报告里不得当作事实。
- 采不到维表（无 `dim_*`、无 name 列）→ `dimensions` 为空，**不报错**。

### 1.2 取值采集（有界 + 脱敏前置）
- 每个维表 `SELECT key_col, label_col FROM <t> LIMIT profile_enum_max_values`（默认 20）。
- **跳过疑似 PII 列**（`phone|mobile|tel|email|mail|id_card|idcard|name|address|birth|姓名|手机|电话|邮箱|身份证|地址`）：
  维表枚举**本身是数据**，是 E4/02（脱敏）之外的新出口，而 E4/02 尚未实现 → 这里必须自带跳过 + 审计。
- 单表查询失败 → 跳过该表，不影响其余（绝不因元数据查询失败而让分析失败）。

### 1.3 缓存
- `short_term` 键 `semantics:<sha1(db_url)[:12]>`，值 `{ts, payload}`，TTL `semantics_ttl_s`（默认 3600）。
- 缓存命中且未过期 → 直接返回；`REDIS_URL` 为空时是进程内 dict（现有降级不变）。

## 2. 注入（三处，长度有界）
| 位置 | 内容 | 理由 |
|---|---|---|
| `run_context` payload | `business_semantics`（紧凑文本） | context 负责"业务词 → 列"的映射（"华东"→`region_id`） |
| `run_planner` task_context | `business_semantics`（紧凑文本） | planner 首次获得真实表结构；规划 join 时不再凭猜 |
| `run_analyst` payload | 同上（紧凑） | 结论里要写"华东"而不是"区域 1" |

`describe_semantics()` 输出示例（**有界**：最多 6 个维度 × 8 个取值）：

```
[业务语义]（由表结构+维表取值自动采集，关系按命名约定推断，需与实际口径核对）
- dim_region: region_id → region_name（华东/华北/华南/西部/境外）
- dim_channel: channel_id → channel_name（直销/合作伙伴/线上）
- 关系(fk-naming)：fact_sales.region_id → dim_region.region_id
```

## 3. 与知识库的关系（**不在分析期间写库**）
用户要求"自动采集 + 写入知识库"。写入走**离线脚本** `scripts/build_semantics.py`：
采集 → 确定性文本 → `knowledge_store.add(text, source=f"semantics:{sha1}")`（source 哈希幂等）。
**理由**：分析请求全程只读（§22 `NO WRITE ACCESS`）。把写库放进请求路径等于"只读分析"在运行中改状态，
且会引入并发写与租户隔离问题。离线脚本同时满足"进知识库"与"只读分析"两个约束。

## 4. `dataset_profile` 增 `enums`（顺手补齐）
- `enums: {col: [values]}`：仅低基数文本列（`distinct <= profile_enum_max_cardinality`，默认 20），
  且**跳过 PII 列**；跳过的列记入 `enums_skipped`。
- 与 §1 独立：profile 是"临时画像某人给的表"，semantics 是"内置库的长期语义"。

## 5. 校正既有漂移
`schema_search` 的 ToolSpec 声明 `required: ["query"]`，而实现只读 `keyword`（`query`/`entities` 是死参数）。
校正为 `required: []`，并把 `query` 作为 `keyword` 的别名兼容，避免"契约说谎"被后续工作固化。

## 6. 边界
- 语义层**只提供线索，不替代校验**：关系 `confidence="naming_convention"`，报告引用时不得当事实陈述。
- 采集是**有界**的（表数 × 取值数都有上限），大库不失衡；采集失败静默跳过并记 `metadata["semantics_error"]`。
- 与 E4/02 的关系：本模块自带 PII 跳过；E4/02 落地后应改为共用同一份模式表（记 TODO）。
- **不**做指标语义层（口径定义、血缘）——那是 C 里程碑。

## 7. TDD
| 用例 | 断言 |
|---|---|
| PII 列识别 | `phone/email/id_card/姓名/身份证` → True；`region_id/revenue` → False |
| 关系推断 | `dim_region` + `fact_sales.region_id` → 一条 `many_to_one`，`confidence="naming_convention"` |
| 维表识别 | 名字列缺失的表不被当维表；`dim_channel` 可识别 |
| 取值采集 | 样例库 → `dimensions["region_id"]["values"]["1"] == "华东"` |
| PII 跳过 | 造一张含 `email` 的维表 → 不入 `dimensions`，且记入 `skipped_pii` |
| 缓存 | 第二次采集命中缓存（不重复查询：计数器断言） |
| 失败不致命 | 数据源缺失 → 返回空 Semantics + 错误码，不抛 |
| 描述有界 | 维度多时输出 ≤ `max_dims`，取值 ≤ `max_values` |
| 注入 | `planner`/`context` payload 含 `business_semantics`；`describe` 里出现"华东" |
| profile enums | `dim_region` → `enums["region_name"]` 含"华东"；`enums_skipped` 存在 |
| ToolSpec 校正 | `schema_search` 的 `required` 不再强制 `query`，且 `query` 仍可用作别名 |
