# 待真实验证清单（[待真实验证]）

> 铁律 6：无 key / 无真实业务数据前，以下项不得声称"已达标"。

> **2026-09-15（D52）重大更正**：本文件此前把"真模型项全部待验证"归因于
> **key 余额 402 / 没有 key**——**归因错了**。真因是测试脚手架把端点指错了：
> `tests/conftest.py` 用 `os.environ.setdefault("LLM_BASE_URL", "https://openrouter.ai/api/v1")`
> 设了默认值，而**环境变量优先级高于 `.env`**（pydantic-settings），于是 `.env` 里的真 key
> 被发到了 openrouter，返回 `401 Missing Authentication header`——看起来完全像"没有 key"。
> 修掉这两行 setdefault 后，`tests/test_agent_real.py` **14 passed（首次全绿）**，耗时 9分17秒。
>
> **诚实边界**：真模型套件验的是**流水线级**（context 解析 / planner 出合法计划 / 7 个工具真跑 /
> 全链到 FINISH / HTTP 端到端），**不等于质量指标级**——自由写码正确率、统计显著性判断、
> 真实长报告溯源覆盖率这些**质量**问题，仍需 `eval --mode real` + 人工抽查，见下表。

> ~~**2026-09-10（D18）更新**：`.env` 的 OpenRouter key **已验证有效**（`openai/gpt-4o-mini`，
> 200 OK / 3.5s），但**账户余额不足**：每次真实调用因 `LLM_MAX_TOKENS=2048` 被 402 拒绝
> （"can only afford 1798"），路由器随即**静默降级为 Mock**。故下表各项**仍未验证**——
> 且本轮额外暴露一个产品缺陷：降级对调用方不可见（`/health` 仍报 `mock_llm:false`、
> 响应无 `degraded` 字段），详见 `docs/progress/demo-script.md` §5 阻塞项 3。~~
>
> **↑ 已被 2026-09-15（D52）更正**：D18 那次测的是 **OpenRouter 的 key**（另一个服务商、
> 余额确实不足），而 `.env` 里的 key 是 **matrix 端点**的。两者被 conftest 的默认值搅在一起，
> 于是"另一个服务商余额不足"被记成了"本项目没有可用 key"。**402 那条结论对 openrouter 成立，
> 对本项目不成立**——保留原文以免后人重复同一个误判。降级可见化那条**仍有效**（已由 DEGRADE 落地）。

| 项 | 所属 | 验证方式 | 状态 |
|---|---|---|---|
| 自由写码（E2）正确率：跨表 join / 窗口 / 任意分析 SQL 与 Python 生成质量 | E2 | `eval --mode real` 新增"自定义分析"golden | **实现完成（mock 全绿）**；D52：模型已能真跑（planner 出合法计划、7 工具真跑全过），但**正确率未被度量**——套件只验"跑得通"，不验"写得对"。D53 real 基线暴露：**plan 每步 `input={}` → 执行器合成 `SELECT * FROM dim_channel LIMIT 100`**，等于自由写码这条路径**根本没被真跑到**。**D54 已修契约**（`planner.md` 补 `input` + 执行器按内容选表，见 `E2/02`）。**✅ 已取到读数（2026-09-15 15:34 real 跑）**：**35/35** 个 `sql_query` 步都自带 `input.sql`、且用的是真实表列名（对比修前 8 步全为 `input={}`）——**契约在真模型上生效**。但**写出来的 SQL 大多跑不通**：审计里 20 次 `sql_query` 只有 **3 次 SUCCESS**，失败分两类——① **方言错**（`DATE_TRUNC` / `DATE_FORMAT` / `INTERVAL`：模型默认在写 Postgres/MySQL，而执行引擎是 SQLite）；② **编造 schema**（`customer_id` / `order_id` / `visitor_uv` / 表 `fact_traffic`）。注意 `context.assumptions` 里模型**写出了真实的列语义**，说明 schema 给到了、只是**生成 SQL 时不稳定遵守**。**正确率因此第一次有了真数字，而它很低**——这就是这项的真实状态，不再是未被度量。**D55 已把"写错了没人告诉它怎么改"这条补上**：`sql_precheck.py` 在执行前用**方言提示**（按 `resolve_source` 拿到的**引擎**分级，不误伤真 PG 源）拦下跑不通的构造，失败后把**真实列清单**补进 `error`（REPLAN 唯一拿得到的上下文），并取消带 `input.sql` 步骤的**逐字重试**（结构性 no-op）。**但"回灌之后模型是否真的改对"仍未被验证**——离线只能钉到"错误消息里确实带了这些内容"。**`[待真实验证]`** |
| 统计严谨（E5）显著性判断 | E5 | real 跑显著性样例 + 人工抽查 | **D52：阻塞已解除（模型可真跑）**，显著性判断本身仍未验——需 real 样例 + 人工抽查 |
| eval real 全量基线（质量/成本 USD） | E6 | `python -m app.eval.runner --mode real`（配 cost_* 单价） | **2026-09-15 已执行**（15 用例 / 43 分钟 / 1,062,713 tokens / exit 0），报告 `docs/progress/eval-real-20260915.md`。**但表面指标不可采信**——根因见下方「D53 附加：real 基线解剖」。**D54 已把"评测器会把编造判成通过"这条修掉**（正文数值门禁 + `min_findings` + `--strict`）。**✅ 已重跑（2026-09-15 15:34，`--strict`）**：15 用例 / 49 分钟 / 1,230,497 tokens / **exit 2（门禁否决）**，报告 `docs/progress/eval-real-20260915-d54.md`。断言通过率 **1.0 → 0.467**、工具成功率 **0.986 → 0.228** ——**不是变差，是变真**；门禁 FAIL 的三条里 `幻觉 1.0`（分母仅 1 条 claim）与 `正文数值 5 条` 为实，`证据完整性 PASS`。**仍缺口**：`.env` 未配 `cost_*` 单价 → `成本 USD 0.0` 是**未计**不是零成本。详见 DailyLog「D54 附加」。**D55 已修两处读数陷阱**：① 报告里那行成本现在渲染成 `未计（未配单价）`（`0.0` 会渲染成 `0.0（单价为 0 = 已知免费）`），`.env` 里那两行 `COST_*=0` 已注释掉——**顺带更正一句错注释**（"当前模型 id 含 `-free`"，实际是 `deepseek/deepseek-v4-flash-w8a8`，**不是免费档**）；② **`工具成功率 0.228` 应按 `0.514` 读**——79 次调用里 61 条非 SUCCESS 中 **44 条是 `依赖步骤未完成`（从未执行）**，修后分母只算**执行过的**：`18/35 = 0.514`，且**新分母 35 恰好等于审计条数 35**（两个独立口径互证）。此为**用 D54 数据重算，不是新基线**（重跑需 49 分钟量级）|
| 多方言 SQL 语义（pg/mysql 真实库） | E7 | 接真实业务库跑 E2 自定义 SQL | 待真实库（**与 key 无关**，仍需真数据） |
| 溯源在真实长报告上的覆盖率 | E1 | real 报告抽查数值→SQL 可点开 | **D53 实测：`traced 1/3 = 0.333`、`hallucination_rate 0.667`**——但分母只有 3 条数值 claim，因为**几乎没产出真数据**（见下）；此数**不能当作"溯源率 33%"读**，它测的是"根本没数据时也能编"。**D54 补的是另一侧**：`sources` 只查 `findings[].evidence[].value`、**报告正文从不检查**（编表却 ✅ 的来源）→ 新增 `ungrounded_numbers` 把正文也纳入门禁。**✅ 2026-09-15 15:34 real 重跑读数：`traced 0/1`、`hallucination_rate 1.0`**——但分母**只有 1 条**数值 claim（`a_normal_query_no_adversarial` 里的行数 `3120`，`sql_id=None` → 判无源）。那个数大概率是**真的**（`schema_search` 的 `row_count` 就有），判红是因为**没归属到任何证据**——按 E1 纪律这是对的（无归属的数值无法验证），但**不该读成"溯源率 0%"或"幻觉率 100%"**。本轮真正抓到编造的是 `grounded_numbers` 那 **5 条**（`r_join_amplification_guard` 正文里的品类营收表）。 |
| 增量迭代（E3）语义判定与增量脚本质量：指代识别/越界守卫在真实自然语言下的准确率 | E3 | real 跑多轮"下钻/改期/换粒度"会话 + 人工抽查是否少做 | **守卫与分类为确定性实现（mock 全绿）**；D52 `test_full_pipeline_second_query` 真实多轮已过，但"是否**少做**了冗余步骤"（省没省）仍需人工抽查 |

> **表项状态审计（2026-09-17）**：主表 5 行之外，以下编号下已有 passed 测试用例但未在主表闭环登记 —— **不是漂移，是 doc 未穷举**（主表只列"有真实业务数据/真模型验证缺口"的项，下有 passed 用例不等于"该项彻底完成"）。统一补登：
>
> | E项 | 已实现（passed 测试） | 剩余缺口 |
> |---|---|---|
> | **E1** 溯源 | `test_e1_auto_trace` / `test_e1_trace_validation` / `test_e1_sources` / `test_e1_trace_api` — TODO 覆盖率分母极小（见 §33），与"覆盖率 100%"是两回事 | 报告正文数值的归因（D54 已补，**覆盖率仍需大分母真基线**） |
> | **E2** 自由写码 | `test_e2_freeform_exec` / `test_e2_free_sql` / `test_e2_pycode` / `test_sql_precheck` — 35/35 input.sql 契约生效，方言+预检已修 | 在真业务库上 "模型真改对"（分母 20 跑通 3，正确率已量化但很低） |
> | **E3** 增量 | `test_e3_iteration` + real `test_full_pipeline_second_query` — 多轮增量路径真跑 | "是否少做"需人工抽查 |
> | **E4** 口径 | `test_e4_quality_gate` / `test_e4_profile_quality` / `test_caliber_unit_filter` / `test_e4_caliber` 等 — 单位/过滤/基线/迭代 drift 等均已确定性实现+语义判读（mock-gated） | "语义判读在真模型报告上的约束力" = `caliber_llm_enabled=true` 真跑（mock-gated，待真模型语义判读） |
> | **E5** 显著性 | `test_e5_rigor` — mock 全绿 | 真实样例显著性判断 + 人工抽查 |
> | **E8** 知识深度 | `test_e8_knowledge_depth` / `test_emb_self_healing` / `test_emb_versioning` | 多跳推理覆盖率 |
> | **E9** 查询重写 | `test_e9_query_rewrite` / `test_e2_query_rewrite`/? — 12 passed 1 skipped | 对抗/污染/长上下文表现 |
> | **AUTH** 鉴权 | `test_auth_users_api` / `test_rbac` — mock 全绿 | 多租户/配额/token 跨设备吊销真验 |
> | **MCP** 工具协议 | `test_mcp_tool_spec` / `test_mcp_registry` 等 | 真 MCP 客户端互操作 |
> | **DEGRADE** 降级可见 | `test_degradation_visible` / `test_circuit_breaker` — 已真跑 | 多副本聚合指标进 /metrics |
> | **Redis** | `test_redis_live` 2 passed | 多实例/主从切换 |
> | **Milvus** | `test_milvus_live` 5 passed / 1 skipped | 服务端 standalone 模式独立验证 |
>
> 主表 5 行保留"真实数据 / 真模型判读"缺失的闭环项，上表是"已实现的部分" — **两层合并才是完整状态**。

> **S2 新增（2026-09-10）**：口径可比性的**语义判读**部分待真实模型验证——
> `unit_mismatch`（同一报告里万元/亿元混用）与 `filter_mismatch`（含/不含退款等限定词）
> 只有枚举值、**未实现确定性判定**；`system.md`/`analyst.md` 新增的拆解与分母纪律
> 对真实模型的实际约束力，需 `eval --mode real` + 人工抽查报告抽查（[待真实验证]）。

> **2026-09-12 续：Redis / Milvus 服务端已真验（本项已消解）**
> 两者的"无服务端"结论是**探针方法错了**——本机早已存在 2 天前建好的容器
> （`agent-redis` / `agent-etcd` / `agent-minio` / `agent-milvus`），只是 `Exited`，
> `docker start` 即用、零拉取。现况：
> `test_redis_live.py` **2 passed**（历史首次）、`test_milvus_live.py` **5 passed / 1 skipped**
> （服务端路径首次真跑；skip 的是 Lite 专用断言）。并顺带修掉三处真缺陷：
> ① Redis 会话键**从不过期**（`TTL=-1`，只增不减）→ 新增 `SHORT_TERM_TTL_S` + 滑动续期；
> ② 内存兜底**只写不读**（读路径不对称）→ 修；
> ③ `docker-compose.yml` 镜像 tag 与本机不符 → 改为本机实有 tag + 健康检查（可离线起）。
> 详见 DailyLog「两个"没起来"的中间件」。

> **Gate-2 前置新增（2026-09-11）**：生产镜像 `Dockerfile.prod` 的**体积与容器冒烟未实测**——
> 本机 Docker 三个镜像源均不可用（`docker.m.daocloud.io` EOF / `docker.nju.edu.cn` 403 /
> `dockerhub.azk8s.cn` 不可达），`docker build` 无法完成。换到可连通镜像源的机器上执行：
> `cd web && npm run build && cd .. && ./scripts/smoke_container.sh`（退出码 0=通过 / 2=跳过）。

> **企业化收口新增（2026-09-12）**
>
> **A. ~~余额再次耗尽 → 真实 LLM 全线阻塞~~（2026-09-15 D52 更正：**是端点误指，不是余额**）**
> `test_agent_real.py` 14 条中 6 条（需 LLM 的）因 **402** 失败：
> `You requested up to 2048 tokens, but can only afford 1211`。离线口径整文件排除，故不影响回归门禁。
> **更正**：这个 402 是 **openrouter**（conftest 误指过去的那个端点）给的，不是 `.env` 里
> matrix 端点的额度。删掉 conftest 的两行 `setdefault` 后，同样的 6 条**全部通过**（14 passed）。
> 原文保留，作为"同一个症状可以有完全不同根因"的实例。
>
> **B. Playwright E2E 已实跑（此项已消解）**
> 阻塞原因是 `web/.npmrc` 缺失 → npm 落到用户级配置的**已停代理** `127.0.0.1:7890`；
> 补上项目级 `.npmrc`（registry 指向 npmjs、proxy 清空）后 `npm i` + `npx playwright install chromium` 成功。
> **`web/e2e/*.spec.ts` 5 passed**（含导出真请求 zip 断言 200 + `PK` 魔数、增量徽标两轮实际会话）。
> 离线可守的那一层保留：`tests/test_frontend_collab.py`（11 条源码级契约，含变异校验）。
>
> **B.2 "在 CI 接入"部分：静态审计查出 3 处"接了但从没生效"（已修）**
> 本项目无 git 仓库，`ci.yml` 从未在 Actions 上真跑过，故改为**逐项核对 ci.yml 与代码是否对得上**。
> 核对通过：13 个引用文件全在、`MYSQL_DSN`/`MILVUS_LITE_PATH` 变量名一致、
> 未列入 `--ignore` 的两个 live 文件都能自 skip、image job 的 `grep -q http_requests_total` 成立。
> **查出并修复**：
> ① e2e 的 `Upload Playwright report` **永远上传空**（config 只有 list reporter，从不生成 `playwright-report/`）
> → CI 改 `--reporter=list,html` 并增收 `test-results/`；
> ② **PostgreSQL live 在 CI 里从不运行**（无 PG service）→ 加 `postgres:16-alpine`；
> ③ **Redis live 在 CI 里从不运行**（无 redis service）→ 加 `redis:7-alpine`。
> 修后用**与 CI 完全同一条命令**本地复跑：`5 passed (44.0s)` 且 `playwright-report/index.html` 525KB 实际生成。
> Milvus 服务端不进 CI 的理由（standalone 需 etcd+MinIO，过重易 flaky）已就地注释。详见 `live-validation.md §7`。
>
> **C. 真实基线重跑：CLARIFY 与 golden 语义冲突（待定夺，非 bug）**
> 11:25 重跑 7 条 `requires_real`（**本次 0 次降级**，链路已通）：
> **2 条 FINISH / 5 条 CLARIFY**，断言通过率 0.0。
> 判 CLARIFY 的 5 条均为**"给定数字做判断"**型问题（样例库无对应数据），
> 两条 FINISH 的恰好是**可落到库里的**。推测真实模型对"无从查证的数字"选择反问——
> 与 CLARIFY/01 设计一致，但与 golden「期望 FINISH」冲突。
> **需产品定夺**：golden 接受 CLARIFY，还是收紧 `context.md` 让其带假设推进。
> 未定夺前**不改 golden 与提示词**。详见 `docs/progress/eval-real-baseline.md`。
>
> #### C.1 复诊（2026-09-12 续）：根因是**数据缺口**，不是 golden 语义
>
> 重读明细后发现"CLARIFY vs FINISH"只是**表象**。同一轮三项指标一致指向更本质的问题：
> `工具成功率 0.0`、`平均报告长度 19.9`、`findings 0`、`溯源 claims 0/0`
> —— **7 条用例没有任何一条真正基于数据产出结论**。
>
> 对照 `data/sample_enterprise.db` 实际 schema（只有 `fact_sales` 3120 行 +
> `dim_region`5 / `dim_product`4 / `dim_channel`3，字段仅
> `revenue / orders / customers`），这 7 题是按**另一个更丰富的业务数据集**写的：
>
> | 用例 | 题目要的实体/字段 | 样例库有吗 |
> |---|---|---|
> | `r_ratio_denominator` | **转化率** | ❌ 无此字段（需 orders/visits） |
> | `r_simpson_check` | **转化率** 6%→7% | ❌ 同上 |
> | `r_decompose_before_attribution` | **GMV** + **同比**（需上年） | ❌ 无 GMV；数据仅 2024 |
> | `r_multiple_comparison` | **8 个渠道**的转化率 | ❌ 仅 3 个渠道，且无转化率 |
> | `r_causal_overreach` | **渠道切换**事件 | ❌ 无此维度 |
> | `r_join_amplification_guard` | **订单表** JOIN **商品表** | ⚠️ 无独立订单表（orders 是列），只能 `fact_sales ⋈ dim_product` |
> | `r_caliber_period_mismatch` | 本月 vs 上季度营收 | ⚠️ 有 revenue，但"本月/上季度"口径勉强 |
>
> → **5 题字段在库里根本不存在，2 题勉强**。模型对"无从查证的数字"选择反问（CLARIFY）
> 是**正确行为**，不是缺陷。
>
> **两个 FINISH 的失败原因也不同**：`r_caliber_period_mismatch` 与
> `r_join_amplification_guard` 跑到了 FINISH，但**确定性质量探测器没触发**
> （`period_mismatch` / `join_amplified_used` 均为 `[]`）——因为没有真实查询结果可供探测器判定。
>
> **结论**：改 golden（接受 CLARIFY）或改提示词（强制推进）**都是在治标**，
> 会把"数据缺口"掩盖成"语义分歧"。
>
> **建议（待确认）**：补一份对得上题的演示数据集
> （`data/sample_analyst.db`，含 `fact_orders` / `dim_product`(品类) / `dim_channel`(≥8) /
> 转化率或可推导的 visits / 跨年日期），让这 7 题**都能落到库上**，再重跑
> `eval --mode real` 取**真实**质量数字。这才是"真实验证"的本意。
>
> 在此之前 **golden 与提示词保持不动**（本轮未改一字）。
>
> #### C.2 已交付：评测专用演示数据集 `data/sample_analyst.db`（2026-09-12 续）
>
> 按 C.1 的建议，**在不改 golden、不改提示词**的前提下交付了数据集（纯增量）。
>
> `scripts/generate_analyst_sample.py`（固定种子 `2026`，逐行可复现）生成一个**超集**库：
> 保留与 `sample_enterprise.db` 同构的 `fact_sales` 星型模型（既有 golden 仍可跑），
> 并补齐 7 题需要的实体：

| 表 | 行数 | 服务用例 |
|---|---|---|
| `fact_orders`（含 `gmv`、跨 2023–2024） | 22,767 | `r_join_amplification_guard` / `r_decompose_before_attribution` |
| `fact_traffic`（`visits`/`conversions` → 转化率） | 768 | `r_ratio_denominator` / `r_simpson_check` |
| `dim_channel` **8 个渠道** | 8 | `r_multiple_comparison` |
| `dim_product.category` **4 个品类** | 8 | `r_join_amplification_guard` |
| `dim_customer`（3 个分层） | 200 | 分群/分层 |
| `fact_sales`（与基础库同构） | 33,600 | 既有 golden 复用 |

>
> **刻意植入的陷阱都已在库内核实**（不是断言生成器的内部变量）：
>
> | 陷阱 | 落库实测 |
> |---|---|
> | 辛普森悖论 | 整体转化率 **5.97% → 6.95%（升）**，但**每个分层都降**：高转化渠道 10.03%→9.17%、低转化 3.99%→3.60%；升的是高转化渠道流量占比（33%→60%） |
> | GMV 同比 | 2024-08 同比 **-12.0%**（贴合题干），且**内部驱动分化**：Hardware +48.3% / Training +6.8% 增长，Software -45.5% / Service -44.3% 下滑 → **不拆解必然归错因** |
>
> **回归**：`tests/test_analyst_sample_dataset.py` **8 passed** —— 守"陷阱真的落在库里"
> 和"工具真能在这个 schema 上跑通（含 `dataset_profile`）"。
>
> **下一步**：用 `DATA_DB_URL=sqlite:///./data/sample_analyst.db` 重跑
> `eval --mode real --only-real`，结果写 `docs/progress/eval-real-analyst-dataset.md`
> （**保留** 11:25 的旧基线便于对比）。7 题的期望行为从"CLARIFY 还是 FINISH"之争，
> 变成**真正的质量断言**：能不能识破辛普森、能不能先拆解再归因、能不能校正多重比较。
>
> #### C.3 重跑又挖出第 6 个真缺陷：畸形模型输出**打挂整次评测**（已修）
>
> 用新数据集第一次重跑时，进程**退出码 1、一份报告都没产出**：
>
> ```
> pydantic_core.ValidationError: 3 validation errors for PlanModel
> steps.7.objective  Field required  input_value={'id': 'step_0'}
> steps.7.action     Field required
> steps.7.tool       Field required
> ```
>
> 免费模型给 planner 吐了一个**退化步骤** `{"id": "step_0"}`；`PlanStep` 的
> `objective`/`action`/`tool` 是必填 → `PlanModel.model_validate` 抛错 →
> **裸穿透 `run_analysis`** → 整跑崩掉，**已跑完的用例全部丢失**。
>
> 这不是"模型不听话"的一次性意外：真实/免费/小模型产出结构不合法 JSON 是**常态**，
> 代码库里 `PlanModel._coerce` / `AnalysisResult._coerce_lists` 的注释早已承认这一点
> （原文："ValidationError 把整个 planner 阶段打挂（整跑失败）"）—— 只是
> `PlanStep` 的必填字段没人兜，外层也没有最后一道网。
>
> **四层修复**（`tests/test_llm_output_robustness.py` **14 passed**）：
>
> | 层 | 位置 | 修复 |
> |---|---|---|
> | ① 模型 | `state.py` | `_coerce_step` 补 `objective/action/tool` 默认；**只剩 id 的退化条目直接丢弃**（硬塞默认值会变成一次无意义的工具调用，比丢掉更糟），丢弃数记进 `raw._dropped_steps` 可审计 |
> | ② 节点 | `nodes.py` | 新增 `_llm_model()`：把**具体原因回喂**模型重试一次，仍不可用才降级/报错（`ModelOutputError`）。context/analyst/reflection 降级不崩；planner 响亮失败 |
> | ③ 编排 | `graph.py` | `run_analysis` 兜网：任何节点异常收敛成 `status=ERROR` + `metadata.aborted_by_exception`，**不裸抛给调用方** |
> | ④ 评测 | `runner.py` | **单用例隔离**：一个用例抛错记为 `ERROR`，其余用例照跑、报告照产出 |
>
> **值得记下的一个"反直觉"点**：这套 schema 的字段几乎都有默认值，加上 `_coerce` 容错，
> `PlanModel.model_validate({})` 是**成功**的（空计划）。所以"能过 pydantic 校验"**不等于**"可用"——
> 只靠校验判成败会让重试逻辑形同虚设（首次实现就栽在这，被自己的测试抓出来）。
> 因此 `_llm_model` 增加了 `ok(model) -> bool` **可用性判据**参数
> （如 planner 的 "至少 1 个可用步骤"），由调用方表达内容层面的可接受性。
>
> #### C.4 第 7 个缺陷（**最重要**）：真实基线的"测量诚信"没有被守住
>
> 修完 C.3 重跑时，日志里刷出：
>
> ```
> LLM call failed (reflection); falling back to mock:
>   Error code: 429 - Rate limit exceeded: free-models-per-day.
>   (X-RateLimit-Limit: 50, X-RateLimit-Remaining: 0, Reset: 2026-09-13 08:00 本地)
> ```
>
> OpenRouter 免费额度（50 次/日）已耗尽 → 每次调用 429 → `router` **静默降级为
> MockLLM** → 流水线照常产出一份**模板报告**。
>
> 而 `app/eval/runner.py` **完全没有引用 `degraded` / `fallback`**（实测 grep 为空）：
>
> - `state.metadata["degraded"]` 是现成的（DEGRADE/01 早就实现了"降级对调用方可见"），
>   **但评测器不读它**；
> - 于是 `--mode real` 会把**降级用例当成真实结果计分**，分值看起来一切正常；
> - 报告里也没有任何字段能让人看出"这批数字其实是 mock 给的"。
>
> **危害**：这直接违背项目自己的铁律 6（"没有 key 之前，这些用例不得被算作已达标"）。
> 11:25 那份基线之所以可信，是**靠人去读日志**确认"本次 0 次降级"——
> 不是评测框架自己守住的。换个时间点、换个人跑，就会得到一份"看起来真实"的假基线。
>
> **修复**（`app/eval/runner.py`，`tests/test_llm_output_robustness.py` 已覆盖）：
>
> | 改动 | 效果 |
> |---|---|
> | `CaseOutcome` 增 `degraded` / `degraded_stages` | 降级事实进入结构化结果，不再只躺在日志里 |
> | `real` 模式下降级 → `status="DEGRADED"` | 与 `SKIPPED` 同级，**不计分** |
> | `scored` 口径排除 `DEGRADED` | 通过率/工具成功率等不再被 mock 输出污染 |
> | 指标增 `degraded_excluded` / `scored_cases` | "实际计分几条"一目了然 |
> | markdown 增 ⚠️ 告警块（点名用例 + 降级阶段） | 报告自带"这批数字不可用"的结论 |
>
> **当前真实基线状态**：免费额度耗尽（重置 **2026-09-13 08:00**），
> 故 **`docs/progress/eval-real-analyst-dataset.md` 尚未产出** —— 与其产出一份降级基线，
> 不如不产出。等额度恢复后一条命令即可：
>
> ```bash
> DATA_DB_URL=sqlite:///./data/sample_analyst.db DATA_DB_DIALECT=sqlite MOCK_LLM=false \
>   .venv/Scripts/python.exe -m app.eval.runner --mode real --only-real \
>   --out docs/progress/eval-real-analyst-dataset.md
> ```
>
> #### C.5 额度探针结论 + 已挂一次性自动化（2026-09-12 14:28）
>
> 直接对 `LLM_BASE_URL/chat/completions` 发 `max_tokens=1` 探针（不信本地缓存状态），
> 响应头确认额度**确实仍未恢复**：
>
> | 响应头 | 值 | 解读 |
> |---|---|---|
> | HTTP | **429** | `Rate limit exceeded: free-models-per-day` |
> | `X-RateLimit-Limit` | 50 | 免费档每日上限 |
> | `X-RateLimit-Remaining` | **0** | 已用尽 |
> | `X-RateLimit-Reset` | `1789257600000` | = **2026-09-13 08:00:00 +0800**（已用 `date -d @1789257600` 换算核对） |
>
> 单次 `--only-real` 约需 **30–40 次** LLM 调用（8 条用例 × 4 个节点 + 判别），
> 对 50/日 的额度是**刚好够、没有余量**——所以不宜在额度将尽时反复试跑。
>
> 因此已创建**一次性自动化**「产出企业数据分析Agent真实LLM质量基线」
> （`id=260157da-a0ed-47f3-9213-7136d430fa22`，触发 **2026-09-13 08:20**，留 20 分钟缓冲），
> 其 prompt 内置了三条纪律以免又产出一份不可信的数字：
>
> 1. **先探针再开跑**——仍 429 就直接停，不产出、不覆盖任何报告文件；
> 2. **中途 429 不重试到底**——runner 会把用例标 `DEGRADED` 并剔除计分，这是预期行为；
> 3. **产出后必须检索 `DEGRADED` 标记**——只要有，汇报中必须写明「本基线不完整：N 条因降级被剔除」。
>
> #### C.6 为"对得上题"做的最后一道离线体检：确定性检测器能否被触发（2026-09-12 续）
>
> C.2 交付数据集后，我担心一件事：8 条 `requires_real` 里有些断言**不是靠 LLM 判的**，
> 而是靠**确定性检测器**产的 code。若数据集根本触发不了这些检测器，真实 LLM 跑得再好也过不了。
> 于是离线把每条 `expect_quality_codes` / `expect_caliber_kinds` 逐个实测（无需额度）：
>
> | 用例 | 期望 code | 确定性来源 | 新数据集能否触发 |
> |---|---|---|---|
> | `r_join_amplification_guard` | `join_amplified_used` | `gate.profile_gate`（启发式） | **能**，但需 Agent 写出"放大且进结论"的 SQL（见下方 ⚠️） |
> | `r_multiple_comparison` | `multi_comparison_unadjusted` | `rigor.py` | 能（8 个渠道，多重比较显著性判据齐全） |
> | `r_ratio_denominator` | `untested_comparison` | `rigor.py` | 能（`fact_traffic` 有 visits/conversions，分母可得） |
> | `r_caliber_period_mismatch` | `caliber_kind=period_mismatch` | `caliber.py` | 能（口径期错配是文本判读，与数据无关） |
> | 其余 4 条 | 靠 `must_find` / judge 分 | LLM | 命题字段在库内齐备（GMV/同比/拆解、转化率、渠道、分层） |
>
> **⚠️ 顺带发现一个 golden 设计缺陷（未改，交用户定夺）**
>
> `r_join_amplification_guard` 期望的 `join_amplified_used` 语义是
> "**被放大的结果已用于结论**"（`severity=BLOCK`，`gate.py:151`）。而它的 query 是
> "把订单表和商品表关联后统计各品类营收"——一个**正确**的 Agent 会写
> `JOIN dim_product ON product_id=product_id` + `GROUP BY category`，结果只有 4 行，
> factor≈0.0002，**永远不触发**；只有写错（无连接条件 / 笛卡尔积且不聚合）才会触发。
> 实测三档：
>
> | SQL 形态 | 行数 | 门禁结果 |
> |---|---|---|
> | `JOIN ... GROUP BY category`（正确） | 4 | 无告警 |
> | `CROSS JOIN` 不聚合 | 182,136 | **`join_amplified_used` / BLOCK** |
> | `CROSS JOIN` + `GROUP BY` | 4 | 无告警 |
>
> 也就是说：**这条 golden 只有在 Agent 犯错时才可能通过**，正确行为必然断言失败。
> 这与 C.1 的 `CLARIFY vs FINISH` 属同一类问题（golden 期望与正确行为不一致），
> 但成因不同——此处是**断言方向**问题。**按"未定夺前不改 golden"，我没有动它**，
> 只把它记录下来。可选处理：① 改成期望 `join_amplified_unused`（ANNOTATE，即"门禁发现了但没用"）
> 并把 query 改为强制多表；② 改成 `expect_quality_codes=()`，只留 judge 分。
>
> **缺陷 8（已修）：门禁对"逗号连接"整体失明**
>
> 探针同时暴露：`join_amplification_facts` 的启发式分支要求 SQL 里出现字面 `join`
> 关键字（`if not _JOIN_RE.search(sql): continue`），而"忘写 join 条件"**最常见的写法恰恰是
> 逗号连接** `FROM fact_orders o, dim_product p` —— 这类最该被抓的笛卡尔积被整体跳过。
> 实测（修复前）：逗号连接 182,136 行进结论 → **无任何告警**。
>
> 修复：新增 `_has_join(sql)`（`join` 关键字 **或** FROM 子句内含逗号）与 `_join_tables(sql)`；
> 关键在于 FROM 子句要**截断到下一个顶层子句**，否则 `WHERE x IN (1, 2)` / `GROUP BY a, b` /
> `ORDER BY a, b` 里的逗号会被误判成多表连接。回归见 `tests/test_e4_quality_gate.py`
> 新增 8 条（2 条正例 + 5 条"其它位置的逗号不得误判" + 1 条"逗号连接带正确条件不误报"），
> 该文件 **36 passed**；11 条 `_has_join` 判定场景全对、零误伤。

> #### D. TokenRouter 端点实测：**不能用于本项目的真实评测**（2026-09-12 续 3）
>
> 用户提供了新端点（`https://api.tokenrouter.com/v1`，模型 `z-ai/glm-5.3-free`），
> 用于解除 OpenRouter 免费额度（50/日，重置 08:00）的阻塞。**探针 + 实跑结论：不可用。**
>
> **端点只有 1 个模型**（`GET /v1/models` 实测），无法换一个非推理模型：
>
> | 项 | 值 |
> |---|---|
> | 可用模型数 | **1**（仅 `z-ai/glm-5.3-free`） |
> | 性质 | **重推理模型**，`thinking:{"type":"disabled"}` → **400**（"GLM-5.3 does not support disabling thinking"） |
>
> **决定性实测（8k tokens 的真实 analyst 级提示词）**：
>
> | 配置 | 耗时 | completion | reasoning | 正文长度 | finish |
> |---|---|---|---|---|---|
> | `max_tokens=4096` | — | — | 吃光预算 | **0（空串）** | — |
> | `max_tokens=16384`（默认） | **504s** | 16384 | **15352** | 2375（截断） | `length` |
> | `max_tokens=16384` + `reasoning_effort=none` | 404s | 16384 | **16384** | **0（空串）** | `length` |
> | `max_tokens=32768` | — | — | — | — | **APIConnectionError**（端点拒绝） |
>
> **两个反直觉结论（值得记下）**：
> 1. `reasoning_effort` 只在**小**提示词上显著降低 reasoning（873→41）；
>    8k 提示词下**它仍把整个预算用在思考上**（reasoning=16384、正文为空）。
>    所以它不能让重推理模型胜任大提示词调用——参数"被接受"≠"起作用"。
> 2. 推理模型**吃光预算时返回空字符串而不是报错**。本项目 `_llm_model` 的
>    `ok(model)` 可用性判据把这种情况正确判为「输出结构合法但内容不可用」→
>    用例记为 `ERROR` 而非静默通过——**这正是 C.3 修的那层网在起作用**。
>
> **实跑观测（旧配置、未加 reasoning_effort 时）**：8 条中 2 条跑完（**均为 CLARIFY**）、
> 1 条卡在 analyst 节点 35 分钟（超时→重试循环）。故**全量真实基线无法在该端点产出**。
>
> **本轮据此交付的代码改进（与端点可用性无关，独立成立）**：
> - 新增 `LLM_REASONING_EFFORT` 配置 + `_extra_body` 接线（留空 = 不注入）；
>   `tests/test_llm_reasoning_effort.py` **7 passed**。
> - 新增 `must_not_have_quality_codes` 负向断言（修 `r_join_amplification_guard` 惩罚正确行为）；
>   `tests/test_golden_assertion_direction.py` **9 passed**。
> - `unit_mismatch` / `filter_mismatch` 确定性落地；`tests/test_caliber_unit_filter.py` **9 passed**。
> - 成本折算区分"未知(None)"与"已知免费(0.0)"；`tests/test_eval_cost_units.py` **8 passed**。
> - 补 `docs/specs/E6/01-real-eval-and-cost.md`（E6 此前是唯一空规格目录）。
>
> **建议**：换一个**非推理**或**可关闭思考**的模型（如 `gpt-4o-mini` 一类）再跑
> `--mode real --only-real`；`.env` 已备份为 `.env.bak-tokenrouter-*`。
> 两个已完成用例**均为 CLARIFY** 这一点，仍是"CLARIFY vs golden"（§C）的真实模型证据。
>
> #### E. 换回 OpenRouter 实跑：基线**仍不成立**，但"测谎"那一层当场兑现了（2026-09-13）
>
> 额度恢复后（`GET /v1/models` 列 19 个免费模型）逐个探针，选定
> **`nvidia/nemotron-3-super-120b-a12b:free`**（8k 提示词：18.8s / `finish=stop`；
> 同一提示词 glm-5.3 是 504s + 正文为空），跑全量 `--only-real`。
>
> **结果：7 条里只有 2 条计分，5 条被判 `DEGRADED` 剔除。**
>
> | 用例 | status | 说明 |
> |---|---|---|
> | `r_caliber_period_mismatch` | CLARIFY | 真实反问（该题本身期间不可比） |
> | `r_ratio_denominator` | FINISH | 但断言失败：未命中"分母"、缺 `untested_comparison` |
> | 其余 5 条 | **DEGRADED** | OpenRouter 免费档 **429 `openrouter_free_tier_daily`**（50/日已用尽）→ 熔断打开 → 后续零网络请求 → 全走 mock 模板 |
>
> **这一轮的价值不在数字，在于 C.4 修的那层网被真实触发了一次**：
> 报告自己写出「⚠️ 本轮有 5 条用例发生 LLM 降级，已从真实基线剔除…请先解决额度问题再重跑」，
> 并把 `scored_cases` 与 `degraded_excluded` 分开计数。按旧 runner，这 5 条 mock 模板
> 会被当成真实成绩计入，产出一份"看起来正常"的假基线。
> 报告已归档为 `eval-real-analyst-dataset-INCOMPLETE.md`（**不是** `eval-real-analyst-dataset.md`，避免被误引用）。
>
> **运营结论（新增待办）**：**免费档 50 次/日 < 跑一轮所需（约 70+ 次调用）**，
> 即"额度恢复"也**不足以完成一轮真实基线**。可选：给账户加 10 credits
> （错误信息称可解锁 1000 次/日）、拆成多天跑、或减少 `requires_real` 用例数。
> 重置时间 **2026-09-14 08:00**。
>
> #### E.1 实跑又挖出一个真缺陷：**Reporter 把工具调用 JSON 当报告发出**（已修）
>
> 新模型下 `r_join_amplification_guard` 首跑 `findings=0`、**报告长度 36 字符**。
> 查 checkpoint：`report` 字段的值是
>
> ```json
> {"tool": "schema", "args": {}}
> ```
>
> ——一段**工具调用 JSON** 被原样当成报告发给了用户。
>
> **根因**：`run_reporter` 的唯一守卫是 MockLLM 的 `__markdown__` 信号，
> 即"**只防自己人**"；真实模型返回任何形状的 JSON 都被照单全收。
> 这与 C.3 修的畸形 planner 输出同类——模型输出不可信，必须有**可用性判据**。
>
> **修复**（`nodes._sanitize_report_output`，`tests/test_reporter_output_sanitize.py` **6 passed**）：
> 报告必须是 Markdown；JSON 对象一律不当报告——
> ① MockLLM 信号 → 走模板（历史行为）；② Markdown 被包在 `report`/`markdown`/`content`/`text`
> 字段里 → **取出内层**（不丢内容）；③ 其余（含工具调用）→ 走模板，
> 并把原因记进 `metadata["reporter_fallback"]` + `logger.warning`（铁律 3：兜底不许静默）。
>
> **修复后复跑同一用例：报告 36 → 385 字符，断言全绿（judge 0.9）**——
> 即 `must_not_have_quality_codes`（§一.1 新增的负向断言）+ reporter 净化，
> 让这条 golden 从"必然失败"变成"能通过"。
>
> #### F. ✅ **有效真实基线首次产出**（Matrix 端点，2026-09-13 13:30）
>
> 端点：`https://matrix.mzsjai.com/v1`，模型 `deepseek/deepseek-v4-flash-w8a8`（**非推理**：
> 小探针 1.3s / 8k 提示词 35.4s，`reasoning=None`）。**7 条全部计分，0 条降级。**
>
> 报告：`docs/progress/eval-real-analyst-dataset.md`
>
> | 指标 | 值（**修复后重跑 14:43**） |
> |---|---|
> | FINISH 率 | **0.571**（4/7） |
> | 断言通过率 | **0.429**（3/7，其中 **2 条是 `CLARIFY_OK`**） |
> | 工具成功率 | **0.36**（首跑曾报 1.0 —— **那是假的**，见下方说明） |
> | 平均工具调用 / LLM 调用 | 3.57 / 6.71 |
> | 平均报告长度 | 2866.9 字符 |
> | 平均耗时 | 104.0s/用例；总 319k tokens |
> | 成本 USD | **0.0**（已配单价） |
> | 降级剔除 | **0** |
>
> | 用例 | status | 断言 | 说明（**修复后重跑 14:43**） |
> |---|---|---|---|
> | `r_join_amplification_guard` | FINISH | ✅ | **唯一一条"真 FINISH 且通过"**（§一.1 断言方向 + §E.1 reporter 净化） |
> | `r_ratio_denominator` | **CLARIFY_OK** | ✅ | 判断型问题，反问被接受（`accept_clarify`） |
> | `r_simpson_check` | **CLARIFY_OK** | ✅ | 同上 |
> | `r_decompose_before_attribution` | FINISH | ❌ | 未答出"拆解/贡献" |
> | `r_causal_overreach` | FINISH | ❌ | 未答出"相关/因果" |
> | `r_multiple_comparison` | FINISH | ❌ | 缺 `multi_comparison_unadjusted`（比较了 8 渠道但未做校正声明） |
> | `r_caliber_period_mismatch` | CLARIFY | ❌ | 反问 —— 但**非判断型**，不受 `accept_clarify` 保护 |
>
> **首跑（13:30）的 ✅ 有 2 条是假的**：`r_caliber_period_mismatch` 与
> `r_decompose_before_attribution` 当时每一步 SQL 都是 `SELECT 1`（占位假绿，已修）。
> 修复后前者转 CLARIFY、后者判失败。存档 `eval-real-analyst-dataset-CONTAMINATED.md`。
>
> **根因仍未修（下一条主线）**：planner **既不给 `input.sql`、也常漏排 `schema_search`**
> → `_first_table` 无表可解析 → 占位。现在它**响亮失败**（`缺少 sql 参数` +
> `依赖步骤未完成` 级联）而不是偷偷返回假数据，但"拿不到数据"本身还在。
> 修法二选一：① planner 提示词要求 sql_query 步必须给 `input.sql`；
> ② 执行器解析不出表时**自动补跑一次 `schema_search`**（把不可用变成可用，而非失败）。
>
> **§C「CLARIFY vs golden」现在有结论依据了（样本 2 个模型）**：
> 三条 CLARIFY **全部**是"给定数字做判断"型问题（转化率显著吗 / 渠道切换是原因吗 /
> 总转化率涨=优化成功吗）。**换了一个完全不同的模型（deepseek 而非 glm/nemotron），
> 行为一致** → 说明这**不是某个模型爱反问**，而是系统性的：
> 这类题目把数字**给在问题里**，要的是**统计判断**，不是从库里取数；
> 而 `context.md` 的澄清策略遇到"库里没有对应数据"就倾向反问。
> **即：模型的行为是对的，是 golden 的期望与"判断型问题"不匹配。**
>
> **另两个真实信号（值得记下，尚未处理）**：
> - **溯源覆盖率 `None`、claims `0/0`**：7 条用例**没有任何数值 claim**（3 条 findings=0）。
>   E1 的溯源维度在真实模型下**无从度量**——不是失效，是模型很少产出带数值的 finding。
> - `Reflection PASS 率 0.0`：reflection 几乎总判 REPLAN（4/7 走到 REPLAN 后由 max_replans 截断）。
>   需确认是"模型确实该改"还是"reflection 判据过严"。

---

## D53 附加（2026-09-15）：real 全量基线的解剖——**指标好看，证据为零**

> 命令：`python -m app.eval.runner --mode real`（真实 key，matrix 端点 `deepseek/deepseek-v4-flash-w8a8`）
> 结果：**exit 0**，15 个计分用例，约 43 分钟，1,062,713 tokens（953,912 + 108,801）。
> 报告：`docs/progress/eval-real-20260915.md`。**报告本身不是结论，下面是逐条核对后的结论。**

### 一、报告说"还行"，核对后说"基本没跑通"

| 报告指标 | 值 | 核对后的读法 |
|---|---|---|
| FINISH 率 | 0.80 | 只表示**流程走完**，不表示**问题答对** |
| 断言通过率 | 0.733 | **其中至少 4 条是空洞通过**（见 §三） |
| 工具成功率 | **0.986** | **本文件里最具误导性的一个数**——见 §二 |
| Reflection PASS 率 | 0.20 | reflection 几乎总判 REPLAN，最后被 `MAX_REPLANS=2` 截断 |
| LLM-judge 平均分 | 0.879 | `judge_method: rubric-offline`，**离线打分**，不是真 judge |
| 溯源覆盖率 | 0.333（claims **1/3**） | 分母只有 3——**不是"溯源做得好"，是"根本没产出数据"** |
| 疑似幻觉率 | **0.667** | 只在 3 条 claim 上算的，同样失真 |
| 成本 USD | 0.0 | **正确**——`.env` 显式设了 `COST_INPUT/OUTPUT_PER_MTOK=0`，按约定即"已知免费"，与 `None`（单价未知）可区分。**但换到计费端点时这两个 0 会让成本恒为 0.0**，记得同步改 |

### 二、根因链（**基于审计日志与 checkpoint 断言，不是猜测**）

```
planner 出了 8 步计划（目标写得很好）
  └─ 但**每一步的 input 都是 {}**     ← 缺口在这里
       └─ state 里没有 input.sql
            └─ build_executor_params（nodes.py:482）走到最终兜底：
                 {"sql": f"SELECT * FROM {table} LIMIT 100"}
                 └─ _first_table()（nodes.py:197）取 schema_search 的 tables[0]
                      └─ = dim_channel（**恰好是本地 schema 的第一张表**）
                           └─ 每个 sql_query 步骤都变成 SELECT * FROM dim_channel LIMIT 100
                                └─ 返回 3 行维度数据（直销/合作伙伴/线上）
                                     └─ 记为 SUCCESS  →  tool_success_rate 0.986
```

**审计日志实据**（`data/audit/tool_audit.jsonl`）：

- `q_revenue_diag`：`SELECT * FROM dim_channel LIMIT 100` 执行 **6 次**，全部 SUCCESS
  （该用例共 6 个 SQL 步骤 → **6/6 全是这一条**）
- `q_region_top`：**1 次**（该用例 1 个 SQL 步骤 → 1/1）
- `q_channel_trend`：**2 次**（2/2）

**checkpoint 实据**（`data/checkpoints/eval_q_revenue_diag_7c5cd8.json`）：
`mode: full, status: FINISH, replan: 2, findings: 0`，报告自己写着
「**本次分析未能完成。**……实际执行的 6 次查询全部返回 `dim_channel` 维表数据（3 条记录：直销、合作伙伴、线上）」。

> **这就是 D38 的形态**（"响亮失败"被改造成"无声的错误答案"），**只是高了一层**：
> D38 修的是"SQL 报错却记成功"，这次是"SQL 没报错、但回答的**不是被问的问题**"，
> 而 `tool_success_rate` 只证明"SQL 执行了"，**完全不证明"这个步骤的目标达成了"**。

### 三、最严重的一条：**凭空造出一整张表，还判了 ✅**

`q_region_top` 的报告里有一张完整的区域营收表（华东 1,245,000 / +18.5% / 占比 32% …），
而该用例**实际只执行了一条 `SELECT * FROM dim_channel LIMIT 100`**（3 行渠道维表）。

- 数字**全部无源** → 这正是 `hallucination_rate 0.667` 抓到的（1/3 有源）；
- 但它**判 ✅ 通过**——因为 `min_findings` 只在 `r_*` 用例上设了 `1`，
  5 个基础 `q_*` 用例**没有** `min_findings`（默认 0）；
- 且 `must_find` 是**子串匹配**，而报告天然**回显问题**（`golden.py:40-44` 的注释已记录此漏洞）。

> **教训**：`hallucination_rate` 目前只是**记录指标，不是门禁**。
> 一个"编了整张表"的用例能拿 ✅，说明 E6 的**幻觉维度还没有否决权**。

### 四、四个空洞 ✅（`findings=0` 却通过）

`q_revenue_diag` / `q_category_orders` / `q_general_revenue` / `a_normal_query_no_adversarial`
——四条全部 `findings=0`，四条全部 ✅。**零发现 = 零分析，却算通过。**

### 五、澄清不是假绿，但也没带来证据

`r_ratio_denominator` / `r_causal_overreach` / `r_simpson_check` 三条 `CLARIFY_OK`：
这是 `accept_clarify=True` 的**设计内行为**（`golden.py` 注释说明它在两个不同模型上稳定复现），
**不是假绿**。但它们的代价是：**4 条真正的 `r_*` 尝试全部 ❌**，
这套 golden 到目前为止**没有就"统计推理"提供过任何正例证据**。

### 六、结论与下一步（**尚未授权开工，此处只记录**）

**不能下的结论**：不能说"真实模型质量 0.733"——那个数由空洞通过撑起来。
**能下的结论**：**这次 real 基线没有真正测到"模型会不会分析"，它测到的是"planner 与 executor 的
契约断了"**——计划有目标、步骤却没人给 SQL，执行器就用兜底把它填成了一句恒定查询。

两条候选修法（与上一节列的**同一个根因**，此处补充实测支撑）：

1. **planner 提示词**要求 `sql_query` 步必须给出 `input.sql`；
2. **执行器**在解析不出表时**自动补跑 `schema_search`** 并据此合成，而**不是**默认取 `tables[0]`。

配套必须做的（否则修好了也测不出来）：

3. **E6 门禁补强**：给 5 个基础 `q_*` 用例加 `min_findings`；
   并把 `hallucination_rate` 从"指标"升级为**可否决的门禁**（编表却 ✅ 这条必须堵上）。

**本地污染提醒（复现基线前先处理）**：`schema_search("营收")` 交互式探测时
把 `sleep.csv`（`origin: user_upload`，测试上传遗留）排在 `tables[0]`。

> **2026-09-15 D54 更正（重要，别把这条读成"基线被污染了"）**：实测
> `attached_tables(None)` 与 `attached_tables("")` 都返回 `sleep`（**只有**
> 空/缺 session_id 才会命中），而 `attached_tables("no_such_session")` 返回 `[]`。
> 根因是 `sidecar_path()` 把空 session 归一成 **`default` 桶**（`attachments.py:214`），
> 于是"没有 session_id 的上传"落进一个**所有无 session_id 调用者共享**的库位。
> **本次 real 基线不受影响**——`evaluate_case` 用的是非空 session id
> （`eval_<case>_<hash>`），实测 `attached_tables('eval_...')` 为 `[]`。
> 上面那句只对**交互式探测**成立。清 `data/` 残留仍是必要的磁盘卫生，
> 但**不是**基线的正确性前提。

**新发现（未修，待排期）**：空 session_id 落到共享 `default` 桶 ⇒
两个都不带 session_id 的调用者会**互相看见对方上传的表**。
`chat.py:61` 用的是 `req.session_id or ""`，即"客户端不带就为空"是可达路径。
这不是本次授权的三件事之一，**只记录，不动手**。

---

## D54 回写（2026-09-15）：上面那三件事**已做完**，以及它们**各自被实测改写的地方**

> 规格：`docs/specs/E2/02-plan-sql-contract.md`、`docs/specs/E6/02-eval-gates.md`
> 用例：`tests/test_plan_sql_contract.py`（14）、`tests/test_eval_grounding.py`（30）
> 详情：`docs/progress/DailyLog.md` 的 D54 章节

| §六 列的候选 | 状态 | 落地形态 |
|---|---|---|
| ① planner 提示词要求 `input.sql` | ✅ 已做 | `planner.md` 的 Plan Step schema **补上 `input` 字段** + 一节硬要求；`run_planner` 注入**有界** `discovered_schema`（表名/列名/行数，最多 12 张，**不夹带数据行**） |
| ② 执行器不默认取 `tables[0]` | ✅ 已做 | `_pick_table` 按**内容**打分（意图词 +3 / 度量列 +2 / 列清单缺失 +1 / 规模 +1 / 维表 −3 且**相对**）；`_first_table` 委托它；**候选 ≥2 且全 0 分 → 空 SQL → 响亮 FAILED**；上传表**结构性优先** |
| ③ E6 门禁补强 | ✅ 已做 | `ungrounded_numbers` 查**报告正文**的大额数值（补 E1 只查 `findings[].evidence[].value` 的洞）+ `max_ungrounded_numbers` 用例级字段 + 5 个 `q_*` 补 `min_findings=1` + `hallucination_rate` **升为可否决门禁** + `--strict` 退出码 2（**默认不改**） |

**五处"设计的想法"被实测改写**（都在规格里留了痕迹，不是悄悄改的）：

1. **门槛拦不住年份**：`2024-09` 切出的 token 是 `2024`，`2024 >= min_abs(1000)` →
   被判无源。而按时段分析的报告**每份都有年份** → 系统性假红 → 显式排除 `1900–2100`。
2. **`--mode mock --strict` 永远退出 2**：`requires_real` 用例在 mock 下**按设计跳过**，
   第一版门禁把它算作失败 → 每跑必红 → **恒红的门禁会被绕过**。
   改为**只在 `real` 模式**下把"跳过"当否决理由（降级两种模式都否决）。
3. **`a_dq_override_not_silent` 加 `min_findings` 是 mock 专属假红**：它走 `quick_answer`，
   **本就不产 findings**，而报告顶部已带 `⚠ 数据质量提示`、`expect_refusal` 已通过
   ——那是模式的正确行为。改为**豁免**，且豁免**必须付代价**（仍带 `expect_refusal`，
   另加守卫用例防"往豁免集里塞后门"）。
4. **一刀切 fail-closed 真打断了导出链路**：全量回归抓到 `test_export` / `test_dlp`
   **2 条红**——夹具是单表 `c(id INTEGER, phone TEXT)`（无度量列），
   而"导出脱敏值"本就不需要聚合列。修法：**候选只有一张时不 fail-closed**
   （**没有第二个选项就没有"选错"**，与上传优先同一条道理）；
   D54 要堵的 bug **必须有 ≥2 个候选**才复现。
   一并**改掉了自己写错的边界用例**（原用例用单候选把错的契约钉死了）。
5. **`evidence.value` 无条件当出处 → 让模型给自己作证**（**用真实产物回放才发现的**，
   本卡最接近"漏洞"的一处）：门禁写完、用例全绿之后，拿那次的 checkpoint 原件
   `data/checkpoints/eval_q_region_top_0b7e93.json` 喂进去，**只抓到 8 条**，
   最刺眼的 `1,245,000` **漏了**。原因在出处集：那 4 条编造值**同时**写在
   `findings[].evidence[].value` 里（`sql_id: "step_5"`、`row_sample: "[]"`），
   而 `step_5` 实际是那条 **3 行维表**查询——第一版等于**让模型的自我声明给自己作证**：
   编一个数 → 写进 evidence → 报告里再写一遍 → 全部"有出处"。
   改为**核对**：evidence 的值必须在**它声称的那条 SQL 步骤输出里真能找到**才算出处，
   `sql_id` 缺失/悬空/该步非 SUCCESS → 一律不算（与 `lineage.py` 同纪律）。
   **修前 8 条 → 修后 12 条，`1,245,000` 在内**。

> 第 5 条值得单独记一句：**它是"用例全绿"之后才暴露的**。
> 30 条红绿用例都没抓到——因为那些用例的 evidence 是我自己写的、和工具输出一致。
> 抓住它的是**拿真实产物回放**。**构造用例只能验证你想到的失效模式**。

**这条基线的读法（更新）**：`工具成功率` 的含义**没有变**——它只证明"SQL 执行了"。
变的是**现在会有别的东西否决它**：正文编数字 → `grounded_numbers` 门禁；
零发现 → `min_findings`；跳过/降级 → `evidence` 门禁。
**修完后的 `eval --mode real` 已于 2026-09-15 15:34 重跑**（`--strict`，exit 2），
读数见 DailyLog「D54 附加」与下表第 2 行——**门禁第一次真的否决了一次**。

---

## D55 回写（2026-09-15）：D54 那轮留下的四处，**没有一处是"模型不够聪明"**

| D54 附加 § 列的项 | 状态 | 落地形态 |
|---|---|---|
| ① 模型自写 SQL 跑不通（方言错 + 编造 schema） | ✅ 已做 | `E2/03`：`sql_precheck.py`（纯函数）——**方言提示按引擎分级**（`resolve_source` 取 dialect，真 PG 源不误伤）+ **schema 只判能证明的**（`alias.column` 且别名可解析，裸列名不判）；预检**只拦方言**，schema 只**补进 `error`**；接线进**合流后**的 `_run_one_step` |
| ② `工具成功率` 分母错（44/61 未执行） | ✅ 已做 | `E6/03`：`ToolResult.skipped`（`status` 仍 `FAILED`）+ 分母 `success/(success+fail−skipped)` + `tool_skipped_total`；**全跳过 → `None`（未定义）** |
| ③ 空 `session_id` 落共享 `default` 桶 | ✅ 已做 | `AUTH/02`：`resolve_session_id`（**生成**而非拒绝，每次 `uuid4`）× 上传/列表/清空/chat（含 SSE）；`default` 兜底保留但**不再可达** |
| ④ 成本恒为 0（"没配"表达成"免费"） | ✅ 已做 | `E6/03`：`None` → `未计（未配单价）` / `0.0` → `0.0（单价为 0 = 已知免费）`；`.env` 两行注释掉；**不猜单价** |

**两处"设计的想法"被实测改写**（都在规格里留了痕）：

1. **`tool_success_rate` 全跳过时报 `None` 而不是 `0.0`**：直觉是"没成功就是 0"，
   但那**混淆了"全都失败"与"什么都没有发生"**——前者是质量信号，后者是**没有信号**。
   与 `hallucination_rate` 零 claim 的处理对齐（把"没测到"报成 0 是最典型的自欺）。
2. **方言预检按引擎分级，不是无条件纠错**：E7 多源下 `input.source` 可能指向真 PG 库，
   那里 `DATE_TRUNC` 是**原生**写法 —— 一律"纠错"会**拦掉正确的 SQL**。
   测试里 `dialect_hints(sql, engine="postgres") == []` 钉的就是这条。

**这条基线有了一处新读法**：`工具成功率 0.228` 应读作 **`0.514`**
（79 减去 44 条**从未执行**的 `依赖步骤未完成` → `18/35`），
且**新分母 35 恰好等于审计条数 35**——审计一直是对的，是 `state.tool_results`
把"依赖没满足、根本没跑"也记成了一条 `FAILED`。
**这是重算，不是新基线**（重跑一次 real 要 49 分钟量级）。

`[待真实验证]`：**"方言提示与真实列清单回灌之后，模型是否真的改对"**——
离线只能钉到"错误消息里确实带了这些内容"，改不改对是一次**模型行为**。
（另：`tests/test_agent_real.py` 13 条自 D54 代码改动起**仍未重跑**，是欠账。）
> **补记（D56）**：这笔欠账**已清**——重跑 1 failed / 13 passed，红的是用例过时
> （`CLARIFY` 不在允许集里，`D52` 那次 14 passed 是**假绿**，该分支从没走到过），
> 修后 **14 passed / 947s / exit 0**。详见上方「D56 回写」。

---

## D56 回写（2026-09-15）：补上 D55 漏掉的**预防侧**

D55 做的是**检测**（写错了→拦住→提示→重写），本卡做的是**预防**（别一上来就写错）。
D54 §四 的两个抓手至此**两个都落地**：

| # | 抓手 | 落地 |
|---|---|---|
| ① | SQL 失败时把方言提示 + 真实列清单回灌给 replan | ✅ D55（`E2/03`）**检测侧** |
| ② | planner 提示词里明确执行引擎 | ✅ **D56（`E2/04`）预防侧**——`dialect_brief` + `task_context["sql_dialect"]` + `planner.md` 新节 |

**关键纪律（比"补一句 prompt"重要得多）**：**绝不硬编码 "SQLite"**。
E7 多源下 `input.source` 可能指向真 PG 库，那里 `DATE_TRUNC` 是原生写法；
提示词与预检是**同一条知识的两个出口**，写死禁令就会在 PG 源上出现
「prompt 禁止用它能用的写法、而预检根本不拦」→ **两边口径相反**。
所以引擎从 `datasource.sources()` 的 `dialect` **读**，判不出来**就不说**（不猜）。

**顺手挖出一处既有漂移**：预检的 `_CURRENT_RE` 一直**抓得到** `CURRENT_DATE`，
但错误提示 `_ENGINE_NOTE` 里**从没提过它**（模型只能在"被拦"与"被引导"之间反复）。
这类漂移本该由"先验 ↔ 预检"的守卫用例抓，这次是它**先在检测侧内部**发生了。
现在 `_SQLITE_BRIEF` 直接拼 `_ENGINE_NOTE` 与 `_TRUNC_FMT`——**改一处两边同时变**。

`[待真实验证]`：**"给了先验之后，模型第一版 SQL 的正确率是否真的提高"**——仍是一次**模型行为**。
判据（下次 `eval --mode real`）：

* 审计里 `sql_query` 的 SUCCESS 率（D54 是 **3/20**）是否上升；
* 失败原因里**方言错**的条数是否下降（若"编造 schema"占比上升，说明先验生效但列纪律仍弱）；
* 计划仍 **35/35** 自带 `input.sql`（D54 的成果不得回退）。

回归：离线全量 **1242 passed / 32 skipped / 0 failed**（差值 25 = 新增用例数）；
`eval --mode mock --strict` **exit 0** 且与 D55 基线**逐项相同**
（mock 的 planner 不读 payload，**先验本就不该在这里产生差异**）。

### 欠账已清：`test_agent_real.py` 重跑 **14 passed**（947s / exit 0）

这笔账从 D54 挂到 D56（D55 也记了一笔）。重跑**不是"跑一遍就绿了"**：
第一次 **1 failed / 13 passed**，红的是 `test_context_stage_dimensions`——
`assert 'CLARIFY' in ('UNDERSTAND', 'PLAN', 'ERROR')`。

* **不是 D56 引入的**：红在 `run_context` 的**状态词表**上，而 D56 改的是 planner 之后的环节
  （`sql_dialect` 注入与 SQL 预检），两者不相交。
* **是用例过时**：`CLARIFY` 是 CLARIFY/01 就有的终止态（`state.py:22`、
  `graph._TERMINAL_STATUSES`、`nodes.run_context` 设它时**显式清空 `state.error`**，
  注释写着"澄清不是错误"）。用例把"模型反问"只当成 `ERROR` 分支。
* **D52 的 14 passed 是假绿**：那天模型没在这一问上反问（**模型非确定性**），
  该用例**从没走过 CLARIFY 分支**。
* 修法：允许集加 `CLARIFY`，并**给该分支补断言**（`metadata["clarification"]["questions"]`
  必须非空——空问题的 CLARIFY 仍判红，那才是静默失败）；两个 context 用例是同一处过时断言，一起修。
  另用离线抽查验了谓词的三种形状（真实形状 → `True` / 空问题 → 判红 / 无 `metadata` → 不抛），
  **因为新分支在当次运行里没被走到，不验等于又添一条假绿**。

**教训**：真模型套件的红要先问"**红在哪个阶段**"——红在别的阶段，多半是用例与产品演进脱节，
而不是本轮回归。
