# E9/01 多跳检索 + Query 子问题拆分

> 版本 v1 · 2026-09-15 · D60
> 依据：`docs/对标企业级Gap.md` §四 ❌（query 改写、多跳拆分、字面重合同语义误判）

---

## 1. 背景与动机

当前 RAG 路径把用户 query 当作单一 query 去求助 `store.search(query, top_k=4)`——
对于复合问句「退货流程与退款流程的异同」「营收口径是否包含退款以及跨月退款如何冲减」
这类多主题查询，单一向量/BM25 召回会把几个子主题的语义**平均**掉：
每个子主题在 top_k=4 里只能分到 1-2 个 chunk，关键细节被其它子主题的文本稀释。

**D60 的目标**：复合查询拆成多个子 query，分别召回、合并、去重、按**全查询**重新打分，
最终仍然产出 ≤ top_k 个 chunk 给到 D53 置信门——**接口契约不变**，但对多主题查询的覆盖密度显著提升。

---

## 2. SDD 契约

### 2.1 模块拆分

新增模块 `app/core/rag/multihop.py`，向 `run()` 原有的单一 `store.search` 调用点提供多跳能力。

| 条目 | 契约 |
|---|---|
| 输入 | `query: str`, `top_k: int`, `tenant: str | None` |
| 查询拆分 | 规则拆分：按枚举符（`,，、`）+ 并列连词（`和/与/及/以及/跟`）+ 选择连词（`或/或者`）+ 英文 `and/or` 切分；每段 ≥ `rag_multi_hop_min_piece_len`（默认 4）字符；纯标点段丢弃 |
| 触发条件 | 拆出 ≥ 2 个有效段**且** query 长度 ≥ `rag_multi_hop_min_query_len`（默认 20） → 进入多跳；否则单跳直通 |
| 拆段上限 | 每查询至多 `rag_multi_hop_max_splits`（默认 3）段；超过的丢弃 |
| Fan-out | 每个子 query 独立调 `store.search(sub_q, sub_budget, tenant=tenant)`，`sub_budget = max(top_k, int(top_k*1.5)+1)` |
| 容错 | 单个子 query 抛异常**不影响其它子 query**（fail-open per sub-query），对应段返回空列表 |
| 去重 | 多个子 query 命中同一 chunk → 按 `id` 去重，保留第一次出现 |
| 重新打分 | 所有去重后的候选 chunk 调用 `reranker.rerank(full_query, candidates, top_k)`——用**全查询**重排，消除跨子 query 的 rerank_score 不可比问题 |
| 输出 | `MultiHopResult(chunks, sub_queries, splits, single_hop)`：`chunks` 已截断到 ≤ `top_k`，可直接喂给 D53 置信门 |
| 配置开关 | `rag_multi_hop_enabled: bool = True`（总开关，关闭即单跳直通）|

### 2.2 关键结构

```python
@dataclass
class MultiHopResult:
    chunks: list[dict[str, Any]]      # 最终 ≤ top_k（已 rerank）
    sub_queries: list[str]            # 实际展开的子查询列表
    splits: int                       # len(sub_queries)
    single_hop: bool                  # 是否跳过多跳直接走单跳
```

### 2.3 插入点

- `app/core/tools/knowledge_tool.py:run(params)`：把 `chunks = get_store().search(query, top_k, tenant=tenant)`（line 1223）替换为 `MultiHopRetriever(store=get_store(), tenant=tenant).retrieve(query, top_k).chunks`
- 不改变 `run()` 的**对外 JSON 契约**（仍返回 `{ok, chunks, confidence, low_confidence, note}`），上游的 `nodes.py` / 报告流 / 评测流**不受影响**
- D53 置信门（`confidence_of(query, chunks)`）**不修改**——只看合并后 top_k 的首条；换 query 只换 chunks 集合，逻辑等价
- 失败-关闭：`MultiHopRetriever.retrieve` 内部任何未捕获异常 → 退回 `store.search` 单跳，**绝不因多跳挂掉整条 RAG 路径**

### 2.4 可观测

新增 Prometheus 计数器（挂在既有 `Metrics` 上）：
- `rag_multi_hop_splits_total{enabled="true|false"}`：多跳查询次数（split 数 ≥ 2）
- `rag_multi_hop_fanout_n`（histogram）：每查询展开子 query 数的分布

---

## 3. 方法契约

### 3.1 `QuerySplitter`

```python
class QuerySplitter:
    def __init__(self, *,
                 enabled: bool = True,
                 max_splits: int = 3,
                 min_query_len: int = 20,
                 min_piece_len: int = 4) -> None: ...

    def split(self, query: str) -> list[str]:
        """返回子查询列表；不可拆 / 不值得拆 → [query]（单跳直通）。"""
```

判定单跳（直接返回 `[query]`）：
- 未启用
- `len(query) < min_query_len`
- split 后有效段数 ≤ 1
- query 为空 / 纯标点

### 3.2 `MultiHopRetriever`

```python
class MultiHopRetriever:
    def __init__(self, store, tenant: str | None = None, *,
                 splitter: QuerySplitter | None = None) -> None: ...

    def retrieve(self, query: str, top_k: int = 4) -> MultiHopResult:
        """主入口：拆分 → fan-out → 合并去重 → rerank 全查询 → 返回 ≤ top_k chunks。"""
```

### 3.3 `run(params)` 现有一跳行为保持对照

单跳直通路径必须与改动前**输出一致**——用同一个 `store.search(query, top_k, tenant=tenant)`；
不做任何截断、重排、去重。这是回归基线最硬的一条。

---

## 4. 测试矩阵（RED 先行）

| ID | 用例 | 期望 |
|---|---|---|
| T1 | 短 query （< 20 字符）「退货地址」 | 单跳直通，`[query]`，splits=1 |
| T2 | 复合 query「退货流程与退款流程的区别」 | 多跳，拆出 ≥ 2 子 query |
| T3 | 枚举 query「营收口径是否包含退款、跨月退款如何冲减以及退货流程」 | 拆出 3 子 query，splits == 3 |
| T4 | 上限 query（> 3 段） | 截断到 max_splits=3 |
| T5 | 带纯标点子段的「华东区域营收、」 | 丢弃空段，不进入多跳 |
| T6 | splitter 关闭（`rag_multi_hop_enabled=False`） | 都单跳直通 |
| T7 | E2E 单跳等价（改动前后召回集一致） | 召回结果集合 == `store.search(q, top_k, tenant)` |
| T8 | E2E 多跳合并 ≤ top_k | result.chunks 长度 ≤ top_k |
| T9 | 去重：同 chunk 被 2 个子 query 命中 | result.chunks 内仅一次 |
| T10 | 子 query 抛异常不阻断其它 | 一段抛异常 → 该段空，其它段合并仍然产出 |
| T11 | final rerank 使用全 query 重排 | 第 0 条的 rerank_score 与 `reranker.rerank(query, candidates, top_k)[0]` 一致 |
| T12 | tenant 传递到每个子 query | 跨 tenant 查询互不污染 |
| T13 | kb_id 传递到 fan-out | /admin/search 路径仍按 KB 隔离 |
| T14 | D53 置信门保持 | `run(params)` 不抛错，confidence.level 仍可为 high/low/none |
| T15 | Milvus stub 兼容 | `MultiHopRetriever(store=MilvusKnowledgeStore(...))` 不抛 AttributeError |

---

## 5. DoD（收工门禁）

- [ ] T1–T15 全部 **GREEN**（含 Milvus skip）
- [ ] E8/01、E8/02、E8/03 回归全绿（`tests/test_e8_*.py`）
- [ ] 离线全量 `pytest tests/ -q --ignore=tests/test_agent_real.py` 不产生**新的** failure（pre-existing 单条 auth 不算）
- [ ] `python -m app.eval.runner --mode mock` 退出码 0，Gates PASS
- [ ] `docs/progress/DailyLog.md` 追加 Day 60 节
- [ ] `docs/对标企业级Gap.md` §四 RAG 行把「多跳拆分」升级为「✅ D60 已闭环」，「query 改写」标为「⚠️ 部分（E9/02 规划）」

---

## 6. SDD 边界（**不做**）

- **Query 改写（rephrasing）**：D60 只做**切分**，不做「用户话 → 规范 query」的同义改写。
  query 改写是 E9/02 的独立卡（需要 LLM + prompt 工程 + golden set），不在 D60 内。
- **真正的跨 chunk 推理（Graph RAG / 多跳 QA merge）**：D60 只负责把多个子 query 各自召回的
  chunk 集合 stable merge + 送到 LLM；不假设 LLM 需要看到"推理链"——那是后续独立卡片。
- **缓存跨 query 中间结果**：fan-out 每次都 fresh 召回；子 query 缓存留后续。
- **调整可信门阈值 / rerank 策略**：D53/D60 不动置信门。多跳只换了首条 chunk 的内容；如果
  首条不够相似 → D53 按设计清除 chunks，行为正确。
- **Milvus 后端重写**：`search` 路径沿用 MilvusKnowledgeStore 已实现的真实方法；新增方法才走 stub。

---

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 多跳 E2E 时延增加 | 子 query 数 ≤ 3；embed 在 mock 模式下极短；单跳路径零开销 |
| merge 后 top_k 首条被稀烂主题占位 | 多跳后首条命中率反而**高于**单跳；若清空 → D53 兜底，不引入假阳性 |
| 未来 query 改写需要不同 API | D60 只新增 `MultiHopRetriever`，不改 `run()` 对外契约 |
