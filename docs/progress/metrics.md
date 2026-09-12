# 基线指标（每 Epic / Gate 更新）

## 记录于三项缺口补齐（2026-09-12）

| 指标 | 值 |
|---|---|
| 离线回归 | **613 passed / 5 skipped / 396s** |
| 规模（100 万行·9 列） | 聚合 94ms · 分组 723ms · **profile 2043ms（线性，瓶颈）** · 语义 3ms |
| 并发（8×40，mock） | 7.21s · 5.54 req/s · 成功率 1.00 · P50 78ms / P95 3.8s（缓存命中 60%） |
| 真实基线 | **第一次无效**（模型链 4/5 不可用 → 大部分退化为 mock）→ 已修链重跑 |

## 记录于 INTERVIEW/01 收口（2026-09-11）

| 指标 | 值 |
|---|---|
| 离线回归 | **593 passed / 5 skipped / 258s**（E7 时为 547） |
| RAG 检索 | **hit@4 = 1.0 · recall@4 = 1.0 · MRR = 1.0**（7 用例含 1 负例） |
| RAG 忠实度（下界） | **0.747**；含伪造数字的那条 0.30（指标可分辨） |
| 记忆检索 | 三因子（相关性/时效/重要性）替代子串匹配 |
| 工具路由 | 7 类中文问句全命中；无效查询回退全量 |
| 请求缓存 | 同会话同问 → 0 次 LLM 调用（命中标记 `cache_hit`） |

## 记录于 E7 收口（Gate-2 前置 · 2026-09-11）

### 离线回归（不含 real）
| 时点 | 用例数 | 通过 | 跳过 | 耗时 |
|---|---|---|---|---|
| D1 基线 | 169 | 169 | 0 | 148s |
| E1 门禁（D7） | 187 | 187 | 0 | — |
| E7 收口 | 552 | 547 | 5 | 333s |
| **INTERVIEW/01（今）** | **598** | **593** | **5** | **258s** |

> 5 个 skipped 均为外部依赖缺失（docker live / redis live 等），非静默跳过。

### eval mock 基线
| 指标 | D6 | 今 |
|---|---|---|
| FINISH 率 | 1.0 | **1.0** |
| 断言通过率 | 1.0 | **1.0** |
| 工具成功率 | 1.0 | **1.0** |
| **溯源覆盖率** | 1.0（9/9） | **1.0（13/13）** |
| avg 工具调用 | 4.8 | **4.75** |
| avg LLM 调用 | 5.0 | **4.62** |
| 用例总数 / 其中 requires_real 跳过 | 5 / 0 | **15 / 7** |

> `skipped_requires_real: 7` **单独计数**，不进通过率（铁律 6）。

### 能力指标（新增）
| 指标 | 值 | 来源 |
|---|---|---|
| 质量门禁规则数 | 6（profile）+ 5（统计）+ 2（对抗） | `gate.py` / `rigor.py` |
| 口径可比检查项 | 3 类确定性 + 2 类待真实验证 | `caliber.py` |
| 业务语义采集 | 3 维表 / 3 外键关系 / 12 取值 | `semantics.py` |
| 脱敏覆盖出口 | rows / enums / profile columns | `security/masking.py` |
| 交付包内容 | report.md + queries.sql + trace.json + data/*.csv + README | `routes/export.py` |
| 生产镜像 | 多阶段 / 非 root / HEALTHCHECK / 默认无 torch | `Dockerfile.prod`（**体积未实测，见下**） |

### 未验证项（如实记录）
- **生产镜像体积**：本机三个 Docker 镜像源全部不可用
  （`docker.m.daocloud.io` EOF、`docker.nju.edu.cn` 403、`dockerhub.azk8s.cn` 不可达），
  `docker build` 无法完成 → **<2GB 目标未实测**。冒烟脚本本身的失败/跳过路径已验证。
- 所有 `requires_real` 用例、写码质量、语义/统计判断 → `pending-real.md`。

## 记录于企业化收口（#1~#6 补齐 · 2026-09-12）

### 现状 → 补齐映射（前端/后端/测试/E2E 四维度）
| 领域 | 后端 | 前端 | 测试 | E2E |
|---|---|---|---|---|
| #1 用户级鉴权+数据权限 | RBAC 角色→权限门禁落地（`execute_tool` 执行前按角色拦截）+ 启动告警 | `auth.ts` 注入 X-API-Key + `AuthGate` 登录弹窗（被拦问题暂存重试） | `test_rbac.py`(11) + `test_frontend_collab.py`(4) | 待补 |
| #2 并发与规模 | `parallel_executor_workers` 实测生效 | — | `test_concurrency_smoke`/`test_parallel_executor` | 实测脚本 |
| #3 可观测性 | `/metrics`(Prometheus 文本) + OTel 钩子 + 告警规则 | — | `test_metrics.py`(6) | `/metrics` 端点 |
| #4 CI+镜像 | `Dockerfile.prod` 多阶段 | — | — | `.github/workflows/ci.yml` |
| #5 LLM-judge | `judge.py`（离线 rubric + 真 LLM 钩子）接入 runner | — | `test_judge.py`(6) | eval 指标 |
| #6 前端协作+E2E | 导出端点已就绪 | 导出 + 分享/评论/权限骨架已落地并渲染 | `test_ui.py`(2) + `test_frontend_collab.py`(3) | Playwright 配置+用例已交付，**未实跑**（npm registry 不可达） |

### 本轮验证（2026-09-12 实跑）
| 项 | 值 |
|---|---|
| CI 口径离线全量（mock / 无 key） | **651 passed / 2 skipped / 0 failed / 378.86s** |
| 前端构建 | `npm run build`（tsc -b + vite）**EXIT=0** |
| 新增前端契约 | `tests/test_frontend_collab.py` **7 passed** + 变异校验（删 `pendingRef` 暂存 → 红） |
| 未跑成 | `test_agent_real.py`（402 余额）、Playwright E2E（代理不可达）、生产镜像（镜像源不可用） |

> **真实基线重跑（11:25）**：7 条 `requires_real` → **FINISH 2 / CLARIFY 5**、断言通过率 0.0、
> 平均 109s、155k tokens。**本次 0 次降级**（链路已通）。
> 判 CLARIFY 的 5 条均为"给定数字做判断"型问题（样例库无对应数据）；
> 属 **golden 语义与澄清策略冲突**，待定夺，见 `pending-real.md` §C。

### 实测数字（合成数据，非估算）
**规模（1,000,000 行 · 9 列 · SQLite 单机）**
| 场景 | 耗时(ms) |
|---|---|
| 全表聚合 COUNT/SUM | 154.6 |
| 分组聚合（20 组） | 1046.3 |
| 明细 LIMIT 100 | 7.2 |
| 维度下钻 JOIN 维表 | 480.2 |
| dataset_profile（9 列逐列 COUNT DISTINCT） | 2378.6（列数线性瓶颈） |
| 业务语义采集（维表取值） | 4.6 |

> 与 9-11 记录相比：profile 2043ms → 2378.6ms（随 SQLite 版本/本机波动）；结论不变——
> 质量基元的列级 COUNT DISTINCT 随**列数**线性恶化，宽表需下推到列存/物化。

**并发（8 并发 · 24 请求 · 工具并行 workers=4 · mock LLM）**
| 指标 | 值 |
|---|---|
| 总耗时 | 9.35s |
| 吞吐 | 2.57 req/s |
| 成功率 | 1.00 |
| 缓存命中率 | 0.333 |
| P50 / P95 / P99 | 4116 / 5260 / 5262 ms |

> P95 由 mock 执行链（executor span ≈ 3.8s）主导，非真实生产延迟；真实 P95 需在真 LLM/真库下复测。
> `parallel_executor_workers` 已确认在多工具步骤上并发生效（`test_parallel_executor` 峰值 ≥ 2）。

### 新增可观测性指标（运行时可由 `/metrics` 抓取）
`http_requests_total` / `http_request_duration_seconds`(P50/P95/P99) / `tool_calls_total` /
`tool_calls_failed_total` / `tool_call_duration_seconds` / `llm_calls_total` /
`llm_prompt_tokens_total` / `llm_completion_tokens_total` / `llm_cost_usd_total` /
`event_http_{401,429,503}_total`。告警规则见 `docs/observability-alerts.md`。



### Live 真库/真向量库实测（2026-09-12 · 详见 `live-validation.md`）

| 项 | 真实环境 | 结果 |
|---|---|---|
| MySQL live | **MySQL 8.0.26**（本机 3306；亦曾用自建免密实例 3307） | **13 passed** —— 命名源/只读/文件原语拦截/行·表·列级数据权限/跨方言 |
| PostgreSQL live | **PostgreSQL**（本机 5432） | **4 passed**（`test_pg_live`）+ **1 passed**（`test_profile_cross_dialect` 端到端） |
| Redis live | **真 Redis 服务端**（本机 6379，`PING`→`+PONG`） | **2 passed** —— 短期记忆读写往返 + 不可达时的降级 |
| Milvus live（Lite） | **Milvus Lite** + all-MiniLM-L6-v2（384 维，真实嵌入） | **6 passed** —— 写入/ANN 检索/**多租户隔离**/回退 |
| Milvus live（**服务端**） | **`milvusdb/milvus:v2.4.15` standalone**（本机 19530，+etcd+MinIO） | **5 passed / 1 skipped**（跳过项为 `test_lite_uri_is_isolated_file`，**仅 Lite 适用**，非缺陷） |
| 浏览器 E2E | **chromium**（真实后端 MOCK_LLM + 样本库） | **5 passed** —— 含真请求导出 `?format=zip` 断言 200 + `PK` |
| 真 LLM judge | OpenRouter 免费模型 | 区分度 excellent .35 / medium .30 / wrong .10 / poor .00（抓到算术矛盾） |
| 跨方言回归（新） | 三方言 + 真库 live | `test_profile_cross_dialect.py` **10 passed**（8 离线 + 2 真库） |

**八项真跑才暴露的缺陷（均已修 + 回归）**：
1. 流式路径不落 checkpoint → `/export` `/trace` `/artifacts` 全 404（导出按钮点不动）→ `tests/test_stream_checkpoint.py`(6)
2. SQL 只读守卫多重绕过（`INTO OUTFILE`/`LOAD_FILE`/`SLEEP`/`CALL`/`/*!…*/` 及注释拆分）
   → `tests/test_sql_readonly_guard.py`(33：22 攻击全拦 / 9 合法零误杀)
3. Milvus 后端 `tenant` 参数缺失 → 配 Milvus 时 `knowledge_search` 必崩且无租户隔离
   → live 用例 `test_milvus_tenant_isolation_end_to_end`
4. `dataset_profile` 取列名用 `Row` 字符串下标 → PG 抛
   `TypeError: tuple indices must be integers or slices, not str`
   （样例库走 `PRAGMA`，**从没进过** `information_schema` 这条线）
5. 改用 `_mapping` 后又撞 MySQL 两坑：**info_schema 列名大写 `COLUMN_NAME`** + `Row._mapping` 区分大小写；
   以及 `_q()` 硬编码**双引号**（MySQL 默认当字符串字面量）→ `ERROR 1064`
   → 取列名改**按位置**；`_q()` 改**方言感知**并按连接绑定（`_q_of(conn)`，**线程安全**）
   → `tests/test_profile_cross_dialect.py`(10)
6. **畸形模型输出打挂整次评测**：免费模型给出退化步骤 `{"id":"step_0"}` → `PlanModel`
   抛 `ValidationError` 裸穿透 → 退出码 1、**已跑完的用例全部丢失、报告没产出**
   → 四层修复（模型层丢弃退化条目并记 `raw._dropped_steps`；节点层 `_llm_model` 回喂重试；
   编排层异常收敛成 `status=ERROR`；评测层单用例隔离）
   → `tests/test_llm_output_robustness.py`(18)
7. **真实基线的"测量诚信"没守住（最重要）**：LLM 429 限流 → `router` 静默降级为 Mock
   → 旧 `runner.py` **零引用** `degraded`/`fallback` → **把 mock 输出当真实成绩计分**，
   报告里也看不出。直接违背铁律 6 → 降级用例改判 `DEGRADED` 并从计分口径剔除，
   指标增 `degraded_excluded`/`scored_cases`，报告增 ⚠️ 告警块
8. **质量门禁对"逗号连接"整体失明**：`join_amplification_facts` 的启发式分支要求 SQL 里
   出现字面 `join` 关键字，而"忘写 join 条件"最常见的写法正是 `FROM a, b` 逗号连接
   → 最该被抓的笛卡尔积被整体跳过（实测 182,136 行进结论 → 零告警）。
   修复：新增 `_has_join()` / `_join_tables()`，FROM 子句**截断到下一顶层子句**避免
   把 `IN (1,2)`/`GROUP BY a, b`/`ORDER BY a, b` 的逗号误判 → `tests/test_e4_quality_gate.py` **36 passed**（+8）

**回归基线（终态）**：离线全量（CI 口径，排除 4 个真库/真 LLM 文件）
**759 passed / 18 skipped / 0 failed（405.1s）**（junitxml 复核：tests=777, failures=0, errors=0, skipped=18；
缺陷 8 修复前为 **751 passed / 769 tests**，差值 8 即本轮新增的门禁回归用例）。
`test_agent_real.py` 单跑 8 passed / 6 failed —— 6 条为需真实 LLM 的用例（`openai.APIStatusError`，账户余额），
非代码问题且已被离线门禁排除。新增 live 套件 13 + 6，离线上线新单测 33 + 6 + 10 + 6 + 11 + 6 + 10 = 82。

> **踩坑（工具口径，重要）：本环境 `pytest` 的退出码不可信。**
> `pytest tests/` 的**汇总行会被 safe-delete 钩子吞掉**——钩子在 `sessionfinish` 阶段拦下
> pytest 清理临时目录（`Temp\pytest-of-<user>\garbage-*`，**759 个文件 > 阈值 50**），
> 进程在打印 `N passed` **之前**退出，日志里只剩进度行却**看不到汇总**。
> **同一组参数实测两次：一次 `exit=0`、一次 `exit=1`**，而 junitxml 两次都是 `failures=0 errors=0`
> —— 钩子触发时机不确定，**退出码与测试结果无关**。
> **正解**：`-p no:cacheprovider --junitxml=xxx.xml`，成败与计数**一律读 XML**
> （`failures`/`errors` 为 0 才真绿）。仅凭"进度条里没有 `F`"可判全绿，但给不出确切数字。
**仍未真验**：镜像 <2GB（**镜像源不可达**——daemon 正常，`docker pull hello-world` 60s 超时；
非"本机无 Docker"，见 DailyLog 事实校正）、真实 LLM（402）。
（Milvus 服务端 live 与 Redis live **已于 2026-09-12 续真跑，见下**；此行原本列它们为未验，现更正。）

> **本机 live 实测（补记 2026-09-12 续）**：`test_pg_live` **4 passed**（PG 在 5432 正常监听，
> 前文"PG live 无服务端"有误，以本行为准）；`test_python_docker_live` **4 passed**（Docker 29.5.3）；
> `test_milvus_live`（`MILVUS_LITE_PATH=./data/milvus_lite`，**文档里那个不带 `.db` 的写法**）
> **6 passed**；`web/e2e` **5 passed**。另修 `get_client()` 的静默降级
> （连接失败现在会 `logger.warning` 打出 uri+原因；未配置仍静默）→ `test_milvus_client_visibility.py`(3)。

> **Redis + Milvus 服务端真跑（2026-09-12 续 2）**
> 两者的"无服务端"结论**是探针错了**：本机早有 2 天前建好的容器（`agent-redis`/`agent-etcd`/
> `agent-minio`/`agent-milvus`），只是 `Exited`，`docker start` 即用、**零拉取**。
>
> | 套件 | 结果 | 说明 |
> |---|---|---|
> | `test_redis_live.py` | **2 passed** | **历史首次通过**（此前恒 skip）；端到端另证：一次分析后 Redis 出现 `da:st:<sid>` hash（`history/last_dataset/...`） |
> | `test_milvus_live.py` | **5 passed / 1 skipped** | **服务端路径首次真跑**（skip 的是 Lite 专用断言） |
> | 合跑（含 pg_live + short_term_ttl） | **16 passed / 1 skipped** | |
>
> 顺带修掉三处真缺陷：① **Redis 会话键从不过期**（`TTL=-1` 只增不减）→ 新增
> `SHORT_TERM_TTL_S`（默认 24h）+ 滑动续期，`test_short_term_ttl.py`(5)；
> ② **内存兜底只写不读**（读路径不对称）→ 修；
> ③ **`docker-compose.yml` 镜像 tag 与本机不符**（必然去拉必失败）→ 改本机实有 tag +
> 补健康检查 + MinIO 变量更名（`MINIO_ROOT_*`）。`docker compose config` 通过、5 镜像全本地命中。
>
> **离线全量（稳定树）**：**762 passed / 19 skipped / 0 failed**。


### 规模基线（CONC/01 · 真库百万行 · 2026-09-12 续）

`scripts/bench_scale.py` 已从"只支持 SQLite"扩展为 **`--backend sqlite|postgres|mysql` + `--dsn`**，
在三种方言上跑**同口径**（同表结构 / 同 9 列 / 同 100 万行 / 同 5 个用例 / 同机单实例）。

| 场景 | SQLite 1M | PostgreSQL 1M | MySQL 1M | 备注 |
|---|---|---|---|---|
| 全表聚合 COUNT/SUM | 135.6 ms | 213.3 ms | 392.4 ms | 无索引也能全扫 |
| 分组聚合（20 组） | 955.7 ms | **156.1 ms** | **8790.8 ms** ⚠️ | MySQL 优化器选错索引，见下 |
| 明细 LIMIT 100 | **5.9 ms** | 58.8 ms | 43.5 ms | SQLite 局部性最好 |
| 维度下钻 JOIN 维表 | 558.3 ms | 166.3 ms | **6057.3 ms** ⚠️ | 同上 |
| `dataset_profile`（9 列逐列 COUNT DISTINCT） | 2868.2 ms | 3675.6 ms | **12570.8 ms** ⚠️ | 唯一随**列数**线性恶化的基元 |
| 业务语义采集（维表取值） | 2.5 ms | 114.5 ms | 88.1 ms | 有界，与行数无关（符合设计） |
| 装载（建表+写 1M 行+建索引） | 2.7 s | 5.9 s（PG `COPY`） | 42.7 s | MySQL 走 ORM 批插，未开 `LOAD DATA` |

**结论（只认实测）**：
- **`dataset_profile` 是规模瓶颈**：1M 行 / 9 列要在 PG 上 3.7s、MySQL 上 12.6s。
  它是逐列 `COUNT(DISTINCT)` 的 N 次全表扫 → **随列数线性恶化**。宽表（30+ 列）会成秒级阻塞，
  建议：① 只对抽样/关键列做 distinct；② 或加 `profile_max_columns` 上限 + 抽样比例。
- **MySQL 慢 50× 不是我们这层的问题，是缺覆盖索引**：`EXPLAIN` 显示优化器选了 `idx_region`
  索引扫描，但 `SUM(revenue)` 需**回表 100 万次** → 随机 I/O。补一个覆盖索引即可：

  | MySQL 分组聚合 | 耗时 | EXPLAIN |
  |---|---|---|
  | 仅 `idx_region(region_id)` | 4946 ms | `index scan`，无 `Using index` |
  | 加 `idx_region_rev(region_id, revenue)` | **562 ms（8.8×）** | `Using index`（覆盖） |

  → 给真业务库接维表时，**建议对"分组键 + 度量"建覆盖索引**，否则单次分组分析即秒级退化。
- MySQL 装载 42.7s vs PG `COPY` 5.9s：批量装载路径未优化（未用 `LOAD DATA LOCAL INFILE`），
  仅影响造数，不影响在线查询。

复现命令：
```bash
python scripts/bench_scale.py --rows 1000000 --backend postgres
python scripts/bench_scale.py --rows 1000000 --backend mysql \
  --dsn 'mysql+pymysql://root:***@127.0.0.1:3306/da_agent'
```
