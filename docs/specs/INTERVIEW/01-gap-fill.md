# INTERVIEW/01 八股文补齐 — 规格 v1.0

> 动因：拿 `references/.../01-面试八股文/` 9 篇目录逐条对照项目实现，找出 5 处
> **"八股文会问、项目答不实"** 的缺口。本规格逐条补齐，每条都要求可测 + 可观测。
>
> 对照结论（约 70% 已落地）与本规格覆盖的 5 项：
> ① `long_term.search` 只有子串匹配，无「相关性/时效/重要性」三因子（八股文 05.6）
> ② `build_graph()` 声明了 LangGraph 支持但**零测试覆盖**（"说了没做"）
> ③ 无工具路由（八股文 04.4：工具多时按语义选工具）
> ④ 无请求级缓存（八股文 08.2 Token 成本控制）
> ⑤ 无 RAG 评估（八股文 03.9：命中率/忠实度/RAGAS）

## 1. 记忆三因子打分（05.6）

```python
def score_entry(entry: dict, query: str, *, now: float | None = None) -> float
```
- **相关性**：query 与 entry 文本的**词元重合度**（中文按 2-gram + 英文按词切；`matched / query_terms`）。
- **时效**：`exp(-age_days / half_life)`，`half_life` = `memory_recency_half_life_days`（默认 **30**）。
  **无 `ts` 的旧条目 → 时效给中性值 0.5**（不因"没时间戳"被判过期，也不占便宜）。
- **重要性**：`importance` 字段优先；否则按 `type` 映射（`lesson` > `analysis_summary`）+ `confidence` 缩放。
- 加权：`0.6*rel + 0.25*rec + 0.15*imp`（权重可配）。**排序稳定**：分数相同按新→旧。
- `search()` 签名与**返回结构不变**（`list[dict]`），只改排序；租户隔离与 PG/JSONL 双后端保持。
- **写入补 `ts`**：`append()` 落 ISO 时间戳（PG 走列已有，JSONL 走字段）。

## 2. `build_graph()` 测试覆盖

- `langgraph` 可用时：断言节点齐全（context/planner/executor/analyst/reflection/reporter）、
  入口是 `context`、能 `compile()`；用 mock LLM **真跑一次**到 `FINISH`。
- 不可用时：**显式 skip**（带原因），不静默通过。

## 3. 工具路由（04.4）

```python
def route_tools(query: str, *, top_k: int = 6, threshold: float = 0.05) -> list[str]
```
- **TF-IDF 向量 + 余弦**（确定性、零依赖、离线可跑）——这就是"基于向量检索的工具路由"。
  文档 = `name + description + input_schema 属性名`（含中文别名）。
- 自适应注入：工具数 ≤ `tool_routing_threshold`（默认 12）→ **全给**（8 个工具时行为不变）；
  超过 → 只把 top-k 路由结果注入 planner，并记 `metadata["routed_tools"]`。
- 边界：命中不足 threshold 时**回退全量**（宁可多给，不能因路由失误让 planner 无工具可用）。

## 4. 请求级缓存（08.2）

- 键：`sha256(session_id + 归一化 query + mode)`；值：`AgentState.model_dump()`。
  **默认按会话隔离**（跨会话复用会泄漏数据）；TTL `response_cache_ttl_s`（默认 3600）。
- 命中：重建 state 返回，置 `metadata["cache_hit"]=True`（**绝不假装是新一轮**），并从响应透出。
- 不缓存：`CLARIFY`/`ERROR`/`FAILED`（没结论可复用）、`force_full_rerun=true`（用户显式要求重跑）。
- 开关 `response_cache_enabled`（默认 true）；缓存层不可用时静默降级（不影响主流程）。

## 5. RAG 评估（03.9）

`python -m app.eval.rag_eval --mode mock`，输出 markdown 表：
- **检索命中率**：golden `(query, relevant_source_ids)` → `recall@k` / `hit@k`（`knowledge_search` 返回的 source 对齐）。
- **忠实度（faithfulness）**：确定性近似——抽取答案中的**数字与实词**，检查能被检索文档覆盖的比例。
  **真实 faithfulness 需 LLM judge → 标 `[待真实验证]`**；这里给的是**可离线复现的下界**。
- 边界：知识库为空 → 明确报"无检索结果"而非 0 分通过。

## 6. TDD 汇总
| 项 | 用例 |
|---|---|
| 1 | 三因子：相关性高者胜、新的胜旧的、lesson>summary、无 ts 中性、租户隔离不变 |
| 2 | 节点齐全 + mock 跑通 FINISH；无 langgraph 时显式 skip |
| 3 | 30 个工具里按语义选对 + 差查询回退全量 + 小工具集不变 + 确定性 |
| 4 | 二次相同查询命中（LLM 调用数为 0）+ 不同查询不命中 + 失败不缓存 + force 绕过 + cache_hit 可见 |
| 5 | 指标函数（全中=1.0 / 全不中=0.0）+ 空库明确报错 + 端到端出报告 |


---

## 7. 实现说明（落地记录）

1. **记忆三因子**：`app/core/memory/scoring.py`（纯函数）+ `long_term.append` 补 `ts` +
   `search` 换用 `rank()`。**踩到的两个真问题**：
   - 同分 tie-break 不能靠 `sort` 的稳定性——后端候选顺序不同（PG 按 ts 倒序 / JSONL 扫描顺序），
     结果就不可复现 → 加"标识降序"兜底。
   - `_PATH` 是模块常量 → 改为配置项 `long_term_path`（可测试 + 可迁移），4 处调用点同步更新。
   - 一处用例**碰巧通过**（旧实现的整句子串匹配刚好排除了干扰项）→ 改成"两条都含查询子串、
     只有三因子能分胜负"的判别式用例。
2. **LangGraph**：`tests/test_langgraph_path.py` —— 节点齐全 + **真跑到 FINISH** + 缺依赖时可读报错
   （`importorskip` 显式跳过，不静默放行）。
3. **工具路由**：`app/core/tools/routing.py`（TF-IDF + 余弦，零依赖）。
   **两个实测教训**：① 中文单字会造假命中（"业务知识"因「识别」的「识」被路由到 `image_analyze`）→ 只用 2-gram；
   ② **工具描述原本是英文的**，中文查询零命中 → 加 `_ALIASES` 中文别名表（企业部署里这份表应随工具注册维护）。
   自适应：≤12 个工具全给，超过才路由；命中不足**回退全量**。
4. **请求级缓存**：`response_cache.py`；按会话隔离、只缓存 FINISH、`force_full_rerun` 绕过并刷新、
   命中置 `cache_hit` 透出。
   **与 DEGRADE/01 的交互（重要）**：初版把**降级（mock 兜底）**的 FINISH 也缓存了——
   等于"LLM 恢复了也照旧返回模板报告，TTL 内一直骗人"。已改为**降级结果不入缓存**。
5. **RAG 评估**：`app/eval/rag_eval.py` + `rag_golden.py`；`hit@k / recall@k / MRR / faithfulness`。
   忠实度是**确定性下界**（数字 grounding 0.6 + 实词覆盖 0.4），真实语义判读需 LLM judge → `[待真实验证]`。
   评估用**独立知识库** `data/eval_rag.db`（用应用库会被历史数据污染，实测检索结果里混进过 `guard_test`）。
   实测：**hit@4 = recall@4 = MRR = 1.0**，avg faithfulness 0.747；含伪造数字的那条 faithfulness 0.30（最低）。
