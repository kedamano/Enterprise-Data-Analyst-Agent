# Enterprise Data Analyst Agent

企业级生产落地的数据分析 Agent：把一份「需求设计说明书」落地为可运行的代码。
架构对齐规格说明书的 **Context → Planner → Executor → Analyst → Reflection → Reporter**
自适应编排，并以两个开源项目为工程参照：

- **[DeepAnalyze](https://github.com/ruc-datalab/DeepAnalyze)**（人大/清华）：自主数据科学 Agent，
  「思考-写码-执行-回答」闭环、受限沙箱执行、多界面（API/WebUI）——本项目的 *工具真正执行* 与
  *Python 沙箱* 思路来源于此。
- **[ai-agent-interview-guide / project-python](https://github.com/bcefghj/ai-agent-interview-guide)**：
  企业级 AI Agent 骨架（api / core / infrastructure / etl / models 分层）。
  本项目借鉴其 **目录分层思想与依赖选型**（core 与 infrastructure 分离、基础设施可选降级）；
  `app/core/prompts/data_analyst/*` 与 `app/core/agents/data_analyst/state.py` 等路径为本项目
  依据规格说明书自行设计（project-python 中并无对应目录，其编排为手写 ReAct / Plan-and-Execute，亦未使用 LangGraph）。

---

## 架构

```text
User Request
   │
   ▼
┌────────────────┐  解析业务意图 → 结构化 Context（JSON）
│ Context Resolver│
└────────┬───────┘
         ▼
┌────────────────┐  最小化可执行计划（steps / tools / 依赖）
│    Planner      │
└────────┬───────┘
         ▼
┌────────────────┐  逐步骤调用工具，获取真实数据
│    Executor     │◄── sql_query / python_analysis(sandbox) / schema_search /
└────────┬───────┘     dataset_profile / knowledge_search / visualization
         ▼
┌────────────────┐  把工具结果转成「有证据的分析发现」
│    Analyst      │
└────────┬───────┘
         ▼
┌────────────────┐  质量门禁：数据/指标/证据/逻辑/完整性
│   Reflection    │── PASS → Reporter
└───────┬────────┘    REPLAN → Planner（最多 max_replans 次）
        │ FAIL → 终止
        ▼
┌────────────────┐  结构化业务报告（摘要/指标/发现/根因/建议/局限）
│    Reporter     │
└────────────────┘
```

四大原则贯穿始终：**Understand before Execute · Evidence before Conclusion ·
Validation before Recommendation · Business Value over Technical Complexity**。

---

## 快速开始

### 1. 准备依赖（推荐隔离环境）

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_sample.py        # 生成 data/sample_enterprise.db（基础星型样例库）
python scripts/generate_analyst_sample.py # 可选：生成 data/sample_analyst.db（评测专用超集库）
```

> `sample_analyst.db` 是**评测专用**的超集演示库：在基础星型模型之上补齐
> `fact_orders`(订单/GMV)、`fact_traffic`(流量/转化率)、8 个渠道、4 个品类、跨年数据，
> 并刻意植入**可判定的分析陷阱**（辛普森悖论、GMV 同比驱动分化）。
> 跑「分析师能力」真实评测时用它：`DATA_DB_URL=sqlite:///./data/sample_analyst.db`。
> 详见 `scripts/generate_analyst_sample.py` 顶部说明与 `docs/progress/pending-real.md` §C。

### 2. 离线 Mock 模式（无需任何 API Key 即可端到端跑通）

```bash
python tests/run_demo.py
```

管道会用真实的 `sql_query / python_analysis` 工具读取样例库，Mock LLM 负责
理解/规划/分析/反思/报告，最终打印结构化业务报告与工具执行轨迹。

### 3. 接入真实 LLM（OpenAI 兼容端点）

```bash
cp .env.example .env
# 编辑 .env：填写 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（DeepSeek / Mistral / 本地 vLLM 均可）
uvicorn app.main:app --reload --port 8000
```

- 健康检查：`GET /api/v1/health`（含**观测到的** LLM 降级与可用数据源名）
- 同步分析：`POST /api/v1/chat/analyze`  `{ "query": "..." }`
- 流式分析：`POST /api/v1/chat/analyze/stream`（SSE，每个节点一次事件）
- 增量迭代（E3）：同一 `session_id` 下「基于上一结果，下钻/改期/换粒度」只跑目标阶段
  （分类→守卫→沙箱执行；改期越界等不安全请求自动回退全链）；`{"force_full_rerun": true}` 强制全链。
- **澄清回路（CLARIFY）**：问题太模糊时 Agent 会**反问**（最多 3 个具体问题）而非报错终止；
  `status="CLARIFY"` 时读 `clarification.questions`，作答后带 `clarification_answer` 或直接发下一轮即可续跑。
- **质量门禁**：`quality_issues[]` 给出确定性的数据质量问题
  （join 放大 BLOCK / 主键不唯一 REPLAN / 日期稀疏、粒度误读、未检验对比、多重比较等 ANNOTATE），
  报告相应出现「数据质量与限制」「口径说明」段落。
- **交付物导出**：`GET /api/v1/chat/analyze/export/{session_id}?format=zip|sql|report|csv`
  —— 一次拿走报告 + SQL + 数据 + 溯源（zip）。⚠ 导出物**保留原始值**（脱敏只约束进 LLM 上下文的那份）。
- 溯源：`GET /api/v1/chat/analyze/trace/{session_id}`（claim → sql_id → SQL → 行样本）
- 产物：`GET /api/v1/chat/analyze/artifacts/{session_id}`
- 知识入库：`POST /api/v1/documents/ingest`  `{ "text": "..." }` 或 `{ "file_path": "..." }`
- 交互文档：`http://127.0.0.1:8000/docs`

**记忆检索**：长期记忆按**三因子**（相关性 0.6 / 时效 0.25 / 重要性 0.15）排序召回——
"三个月前的高相关结论"不会输给"昨天的无关摘要"；`long_term_path` 可配。

**工具路由**：工具数超过 `TOOL_ROUTING_THRESHOLD`（默认 12）时按 TF-IDF 余弦只把相关工具给 planner
（命中不足回退全量）；`metadata["routed_tools"]` 可观测。

**请求级缓存**：同会话同问直接命中（省下整条链的 LLM 调用），响应带 `cache_hit` 标记；
**降级结果不入缓存**（避免模板报告被复用）；`force_full_rerun=true` 绕过并刷新。

**RAG 评估**：`python -m app.eval.rag_eval --top-k 4` → hit@k / recall@k / MRR / faithfulness（确定性下界），
用独立知识库 `data/eval_rag.db`。

**多数据源**（E7）：`DATA_SOURCES=[{"name":"crm","url":"..."}]`，工具/计划步骤用 `source` 寻址；
不填 = 主源。**不支持跨源 JOIN**（需要跨源就在各源取数后用 `python_analysis` 合并）。

**用户级鉴权与数据权限**（AUTH/01，**默认关**）：`AUTH_ENABLED=true` + `AUTH_KEYS` 后，
业务端点需 `X-API-Key`；支持**表级白名单 / 列级黑名单 / 行级过滤**（`row_filters` 追加 WHERE 谓词，
配置片段仍过白名单）+ 每用户配额 + 会话归属校验（越权读别人的导出/溯源 → 403）；
ALLOW/DENY 都写 `data/audit/auth.jsonl`。**开了但没配 key → 503**（不静默放开）。

**性能基线**：`docs/progress/perf-baseline.md`；可复跑 `scripts/bench_scale.py`（规模）、
`scripts/bench_concurrency.py`（并发 P50/P95/吞吐）。已知瓶颈：**`dataset_profile` 随行数×列数线性**
（100 万行 9 列 ≈ 2s；宽表需改为"只画像被引用列/采样/近似去重"）。

**默认开的安全边界**（E4/02）：进 LLM 上下文的样本默认脱敏（`MASK_LEVEL=sample`，
`strict` 让敏感列彻底不出现）；关闭会写审计；脱敏自身出错**失败即关闭**。

### 4. Docker 一键起（含 Redis / Milvus 中间件）

```bash
docker compose up -d --build
```

---

## 目录结构

```text
app/
├── main.py                      # FastAPI 入口
├── config.py                    # pydantic-settings 配置（所有中间件均可选、可降级）
├── api/routes/                  # health / chat(analyze) / documents(ingest)
├── core/
│   ├── prompts/data_analyst/    # ★ 7 个 Prompt（system/context/planner/executor/
│   │                            #   analyst/reflection/reporter），与规格说明书一致
│   ├── agents/data_analyst/     # state.py(AgentState+AgentStatus) / nodes.py / graph.py(编排驱动+LangGraph)
│   ├── tools/                   # sql_query(只读+白名单) / python_analysis(受限沙箱) /
│   │                            #   schema_search / dataset_profile / knowledge_search /
│   │                            #   visualization / generate_report + 注册表
│   ├── memory/                  # 短期(会话) / 长期(跨会话，禁写敏感信息)
│   └── rag/                     # retriever / reranker
├── infrastructure/              # llm(网关+熔断+Mock降级) / vectorstore / cache /
│                                #   database / observability(trace)
├── etl/                         # parser / chunker / pipeline（文档→向量库）
└── models/                      # API schemas
data/                            # sample_enterprise.db（星型样例库）+ artifacts（工具产物）
scripts/generate_sample.py      # 生成样例库
tests/run_demo.py                # 离线端到端演示
```

---

## 设计要点 / 安全

- **证据优先**：所有结论必须来自工具真实返回；LLM 只做理解/规划/分析/报告，工具参数由
  `build_executor_params` 依据运行时 schema 确定性合成（可复现、可审计）。
- **SQL 只读**：默认禁止 DML/DDL，单语句，行数上限（`SQL_READONLY` / `SQL_MAX_ROWS`）。
- **Python 沙箱**：AST 预扫屏蔽 `os/subprocess/socket/...`；子进程 `-I` 隔离 + 超时；
  产物通过 `DA_WORKDIR` 在步骤间传递（参考 DeepAnalyze 的沙箱执行）。
- **质检闭环**：Reflection 作为质量门禁，证据不足自动 REPLAN，上限 `MAX_REPLANS`。
- **降级优先**：Redis / Milvus / PostgreSQL 全部可选，缺失时退化为内存/本地 SQLite，
  服务本地即可启动；LLM 不可用时退化为 Mock，保证可用性。

---

## 扩展点

1. **接自己的数仓**：改 `DATA_DB_URL`（postgres/mysql/warehouse DSN），`sql_query` 自动适配方言。
2. **换/增强 LLM**：在 `infrastructure/llm/router.py` 调整网关或新增供应商；
   用 `langgraph` 部署：`from app.core.agents.data_analyst.graph import build_graph`。
3. **增强工具**：在 `core/tools/` 增加工具并在 `REGISTRY` 注册，Planner 即可调度。
4. **知识库**：`POST /documents/ingest` 入库；配置 Milvus 后自动切换到向量检索 + 可选重排。
5. **可视化/报告**：`visualization` 用 matplotlib 出图，`generate_report` 提供确定性模板，
   Reporter 节点在真实 LLM 模式下用模型润色。
