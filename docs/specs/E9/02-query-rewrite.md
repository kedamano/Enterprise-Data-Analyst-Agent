# E9/02 Query 改写

> 版本 v1 · 2026-09-15 · D61
> 依据：`docs/对标企业级Gap.md` §四、§九（行业词表 / query 改写 = E9/02 规划）

---

## 1. 背景与动机

D53 residual 指出「字面重合同语义误判」：`「华东区域的年会在哪里办」` 这类问句，
BM25 抽到的首条 chunk 恰好包含完全相同的 4-gram（`「华东区域的年会在哪里办」`），
但 chunk 的内容其实是华东区域 **年会地点列表** 的用户问句，而非文档中具有回答意义的片段。
短问句 + 字面重合 → 短语亲和判级始终 high → 误判。

更宽的问题：**用户用口语检索企业知识库**——`「GMV 趋势」、「帮我查一下营收口径包含退款吗」、
「cross-month revenue netting 怎么处理的」`——知识库文档里的用词是「商品交易总额」「营收确认口径」「跨月冲减」。
query 与 doc 的 **vocabulary gap** 导致单跳向量 / BM25 同时漏召回。

**D61 的目标**：在检索前跑一个轻量、**规则化**的 query 改写层：

* 剔除口语填充（`「帮我查一下」「请问」「呢」「吗」`→ 空），把问句压成关键词短语；
* 全/半角 ASCII 归一（`ＧＭＶ` → `GMV`，`（营收）` → `营收`）；
* 可配置的同义词文件（`行业词表`）：把高频口语 → 企业文档用词映射，
  映射结果作为 **alternatives** 注入 D60 多跳的 fan-out（即原 query + 若干同义 phrasing 各召回一次、合并、去重、并用原 query 重排）。
* 全链路 **fail-open**：改写抛异常 / 拆空 → 退回原始 query，绝不拖挂检索。

---

## 2. SDD 契约

### 2.1 模块位置

新增 `app/core/rag/rewrite.py`，不动既有模块的签名。

新增配置字段（`app/config.py` D61 段）：

| 字段 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `rag_query_rewrite_enabled` | `bool` | `True` | 总开关。`False` → 整个改写层短路，与 D60 行为一致 |
| `rag_query_rewrite_synonym_path` | `str` | `""` | 行业词表文件路径（UTF-8，每行 `k = v` 或 `k,v`；空 = 不解词） |
| `rag_query_rewrite_max_alternatives` | `int` | `2` | 改写后额外保留的同义 phrasing 上限 |
| `rag_query_rewrite_min_len` | `int` | `4` | rewrite 后低于此长度 → 结果作废，退回原 query |

### 2.2 关键结构

```python
@dataclass
class RewriteResult:
    original: str              # 入参原 query
    rewritten: str             # 主改写结果（直接替换原 query 送检索）
    alternatives: list[str]    # 改写变体 / 同义 phrasing（注入 fan-out）
    changed: bool              # 是否与原 query 不同
    mode: str                  # "rule"（当前实现；LLM 模式留作后续卡）
    synonym_hits: list[str]    # 命中的同义词键（metrics / trace 用）


class QueryRewriter:
    """规则化 query 改写器。

    仅做**确定性**文本变换（LLM-free）：全半角、口语剔除、同义 fan-out。
    任何异常都应被吞掉并返回 ``RewriteResult(original, original, [], False, "rule", [])``——
    上层调用默认 fail-open。
    """
    def __init__(self,
                 *,
                 enabled: bool | None = None,
                 synonym_path: str | None = None,
                 max_alternatives: int | None = None,
                 min_len: int | None = None) -> None: ...

    def rewrite(self, query: str) -> RewriteResult: ...
```

### 2.3 `rewrite` 的处理顺序

1. 空 / 纯空白 → `RewriteResult(q, q, [], False, "rule", [])`（no-op）
2. 半角化 ASCII + 标点剥离（全角字母数字→半角；中英文括号→空格；空白归一）
3. 口语填充词剔除（前后缀）：`帮我、查一下、请问、麻烦、请教、想知道、看一下、请、呢、吗、啊、呀` ——白名单）
4. **synonym 扫描**（如配置了 synonym_path）：从左到右对 synonym 词典里的每个 `k`，
   若 query 含 `k`，把 `v` 作为 alternative phrasing（把 k 替换为 v 后的 query）。
   避免同一次 rewrite 重复同义词：命中集合 + 按命中顺序保留前 `max_alternatives`。
   synonym 文件格式：
   ```
   # 注释行
   GMV = 商品交易总额
   营收 = 营业收入
   cross-month revenue netting = 跨月冲减
   ```
   分隔符：第一个出现的 `=` 或 `,`; 首尾空白 strip。
5. 「主改写结果」= step 2+3 的结果；若 synonym 命中，主改写**不**做 synonym 替换
   （主改写只看口语剔除 / 归一；同义 phrasing 由 alternatives 承载，走 fan-out）。
6. 长度 < `min_len` 视为主改写失败 → `changed=False`, `alternatives=[]`, `rewritten=original`。

### 2.4 与 D60 MultiHop 的协作

在 `knowledge_tool.run(params)` 里把 `MultiHopRetriever.retrieve(q, top_k)` 替换为：

```python
from ..rag.rewrite import QueryRewriter, RewriteResult

rw: RewriteResult = rewriter.rewrite(query)
seeds = [rw.rewritten, *rw.alternatives]
# 多 fan-out 入口，合并去重用原 query 重排
result = mh.retrieve_many(seeds, top_k)
```

`MultiHopRetriever.retrieve_many(queries, top_k)` 在既有 `retrieve` 之上：

* 逐个调 `retrieve(q, sub_budget)`，跨 query 按 chunk.id 去重（保留第一次）；
* 合并后的 candidate pool 用**第一个 seed**（= 原 query 改写主结果）rerank 到 top_k；
* `single_hop` 语义：**只要任一 seed 命中多跳，整趟算多跳**（reported `single_hop=False`）；
* 所有 seed 都是单跳 → `single_hop=True`；
* 返回 `MultiHopResult` 兼容既有字段，外加 `seed_queries`（给 metrics / trace）。

新增字段放在 `MultiHopResult` 而不是新 dataclass，保持 D53 置信门零改造：

```python
@dataclass
class MultiHopResult:
    chunks: list[dict[str, Any]]
    sub_queries: list[str]
    splits: int
    single_hop: bool
    seed_queries: list[str] = field(default_factory=list)   # D61 新增：改写注入的种子
```

### 2.5 可观测（metrics）

在既有 `Metrics` 上新增 counter：

* `rag_query_rewrite_total{changed="true|false"}`：改写调用次数 / 是否实际改写
* `rag_query_rewrite_synonym_hits_total`：同义词命中总次数
* `rag_query_rewrite_fallback_total`：改写失败退回次数

挂在既有 `/metrics` 暴露。

---

## 3. 方法契约

### 3.1 `QueryRewriter`

```python
class QueryRewriter:
    def __init__(self,
                 *,
                 enabled: bool | None = None,     # None → 读 settings.rag_query_rewrite_enabled
                 synonym_path: str | None = None, # None → 读 settings.rag_query_rewrite_synonym_path
                 max_alternatives: int | None = None,
                 min_len: int | None = None) -> None:
        ...

    def rewrite(self, query: str) -> RewriteResult:
        """确定性改写；任何异常 → 返回原始 query 未改写的 RewriteResult（fail-open）。"""
```

内部 helper：

* `_to_halfwidth(s: str) -> str`：全角 ASCII → 半角；括号/全角空格替换。
* `_strip_conversational(s: str) -> str`：前后缀口语词白名单 strip。
* `_load_synonyms(path: str) -> dict[str, str]`：读文件 → {k: v}；不存在 / 解析错 → {}（吞异常、不影响上层）。

### 3.2 `MultiHopRetriever.retrieve_many`

```python
def retrieve_many(self, queries: list[str], top_k: int, *,
                  kb_id: str | None = None,
                  include_stale_versions: bool = False) -> MultiHopResult:
    """对多个 query（改写种子）分别做 fan-out，合并去重，用首 query rerank。

    任何单个 seed 异常 → 跳过该 seed（fail-open per seed）；全部 seed 都异常 →
    返回空 MultiHopResult + single_hop=False（让上层 D53 走到 none 兜底）。
    """
```

---

## 4. 测试矩阵（RED 先行）

| ID | 用例 | 期望 |
|---|---|---|
| T1 | 含口语助词「帮我查一下 GMV 趋势吧」| rewritten=`GMV 趋势`, changed=True |
| T2 | 全角 ＋ 括号「（test）数据」| rewritten=`test 数据` 且全半角归一 |
| T3 | synonym 命中（`GMV=商品交易总额`）| rewritten=`GMV 趋势`, alternatives 包含 `商品交易总额 趋势` |
| T4 | **多 synonym 命中但 `max_alternatives=2`**| alternatives 长度 ≤ 2，顺序按命中 |
| T5 | synonym_path 指向不存在文件 | 不改写，alternatives=[]，不抛错 |
| T6 | query 全被口语清空「请帮我查一下」→ 空 | changed=False，rewritten=原 query（fallback） |
| T7 | rewrite 层关闭（`rag_query_rewrite_enabled=False`） | rewrite 返回原 query，alternatives=[] |
| T8 | E2E 单跳等价 | rewrite 后单跳结果与原 query 单跳结果**集合相等**（同一条 store.search 路径） |
| T9 | E2E `retrieve_many`：2 种子各召回若干 → 合并 ≤ top_k，命中 chunk 不重复 | `len(result.set_ids) == len(result.chunks)` |
| T10 | E2E `retrieve_many`：一个 seed 抛异常不影响其余 | 异常 seed → 跳过；其余 seed 仍产出 |
| T11 | D53 置信门不因 run() 改造行为偏移 | low=`「华东区域的年会在哪里办」`→ level=none（首条仍字面重合 ≠ high） |
| T12 | Milvus 兼容 | `retrieve_many` 不在 Milvus stub 抛 AttributeError |

---

## 5. DoD（收工门禁）

- [ ] T1–T12 全部 **GREEN**（Milvus skip 除外）
- [ ] E9/01 回归绿（D60 15 条不改行为）
- [ ] E8/01、02、03 回归绿
- [ ] 离线全量 `pytest tests/ --ignore=tests/test_agent_real.py` 不产生新的 failure
- [ ] `python -m app.eval.runner --mode mock` 退出码 0 / Gates PASS
- [ ] `docs/progress/DailyLog.md` 追加 Day 61 节
- [ ] `docs/对标企业级Gap.md` §四 / §九 把「query 改写(E9/02 规划)」升级为「✅ D61」并去「残留」列

---

## 6. SDD 边界（**不做**）

* **LLM-based 改写**（prompt 工程 + golden set）：D61 只实现规则化改写；LLM 入站是后续卡片（且必须保留 fail-open + counter 失败回退）。
* **词干 / 分词**：CJK 用 bigram 处理；不做分词器。synonym 仅在词级精确匹配 / 子串匹配；不保序匹配。
* **query 改写结果直接给 LLM 上下文**：改写只影响 **检索路径**；planner / analyst 看到的还是原 query。
* **synonym 文件的热更新**：重启时重新加载；不实现文件监听（后续运维卡处理）。
* **跨 KB 的 synonym**：synonym 文件全局一份，不按 KB 区分。
* **Milvus 改动**：`retrieve_many` 仅转发给 `retrieve`（后者已有 Milvus stub 路径）；stub 直接走 `retrieve` 分支。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| fan-out 增加 → 时延增加 | `max_alternatives` ≤ 2；种子数 ≤ 3；单跳路径零开销 |
| synonym 词表误伤（低频歧义）| synonym 词表只放了高频映射；不在主改写替换 synonym，仅 fan-out 注入（用户仍看到原 query 的检索位置） |
| 改写抛异常拖挂检索 | `rewrite()` 内部 try/except 兜底；`retrieve_many` 单 seed fail-open |
| D53 误判未解决字面重合 | D61 只缩小 vocabulary gap；字面重合语义误判留作 D62（后续独立卡，需要 rerank 引入 context-based 判定） |
