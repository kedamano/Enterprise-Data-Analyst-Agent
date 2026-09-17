# E8/02 知识库嵌入失败自愈调度 + chunk 质量运维面板

> 版本 v1 · 2026-09-15 · D58
> 依据：`docs/specs/E8/01-knowledge-depth.md` §3 回归修复后遗留 — `embed_failed` 的 chunk
> 长期堆积无出路：要么重新手动入库、要么永远 `vec=NULL`（离线跑 BM25 仍可，但加不了向量通道）。

---

## 1. 背景与动机

D57 落地了 `embed_failed` 状态（嵌入不可用时标记 `status='embed_failed'`），
但这条回流**到此为止**：

* 没有**运维入口**看「哪些 chunk 嵌入失败、堆积了多少、多久没重试」。
* 没有**自愈机制**：嵌入模型从不可用恢复后（缓存预热完成 / 服务重启 / 重新联网），
  不会主动回头重试那些 `embed_failed` 的 chunk。
* 没有**放弃阈值**：永远不自我放弃的 chunk 会无限重试，浪费在没有希望的文本上。
* `chunk_diagnostics()` 已统计 `{total, ok, empty, noise, embed_failed, deprecated}`，
  **但没有任何 API 把它暴露给前端**——运维面板看不到质量全貌。

**D58 解法**：把 `embed_failed` 从**一次性标记**升级为**带生命周期的状态机**：

```
embed_failed ──(retry 成功)──→ ok
     │
     └──(失败次数 > MAX)──→ abandoned
```

并围绕它补：运维可见性（diagnostics 端点 + aging 直方图）、
手动/自动重试触发、放弃阈值与 TTL、可观测指标。

---

## 2. SDD 契约

### 2.1 Schema 扩展（SQLite，懒迁移两列）

| 列 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `failed_count` | `INTEGER NOT NULL DEFAULT 0` | 0 | 嵌入连续失败次数 |
| `retried_at` | `INTEGER` | `NULL` | 最近一次重试时间（unix epoch，秒）；从未重试为 `NULL` |

新增状态：`STATUS_ABANDONED = "abandoned"`（重试超阈值 **或** 超过 TTL 不再希望的 chunk）。

**状态机**：
* `add()` 时嵌入失败 → `status='embed_failed'`, `failed_count=1`, `retried_at=now`
* 重试成功 → `status='ok'`, `vec=<vector>`, `failed_count=0`, `retried_at=now`
* 重试再失败 → `failed_count += 1`, `retried_at=now`
* 失败次数 > `MAX_EMBED_RETRIES`（默认 3）→ `status='abandoned'`, 停止重试
* 外部手动触发「放弃」→ 直接置 `status='abandoned'`

### 2.2 后台自愈调度器

新增模块 `app/core/tools/embed_scheduler.py`：

| 方法 | 契约 |
|---|---|
| `retry_once(store, max_chunks=100) -> dict` | 单次跑批：找出 `(status='embed_failed' AND failed_count < MAX)` 的 chunk，**每次不超过 max_chunks** 条，逐一重试。返回 `{retried, fixed, still_failed, abandoned_now}` |
| `start_background_scheduler(store, interval_s=3600) -> threading.Thread` | 启动 daemon 线程，每隔 `interval_s` 调 `retry_once`。返回线程句柄（主线程 `join(timeout)` 触发首次立即跑）。**生产启用** |
| `abandon_expired(store, ttl_s=86400*30) -> int` | 找出 `retried_at < now - ttl_s` 的 `embed_failed` chunk，置 `status='abandoned'`。返回放弃数量。**TTL 兜底：再老的 chunk 也不再重试** |

调度器不持有 `store` 的长连接；每次 `retry_once` 用 `with _lock, sqlite3.connect(...)` 的短事务。

### 2.3 运维 API（管理面）

在 `app/api/routes/knowledge.py` 加：

| 端点 | 方法 | 契约 |
|---|---|---|
| `/knowledge-bases/admin/embed-failed/retry` | POST | `{ "kb_id": <optional>, "max_chunks": 100 }` → 调 `retry_once`，返回 `{retried, fixed, still_failed, abandoned_now}` |
| `/knowledge-bases/admin/embed-failed/abandon` | POST | `{ "kb_id": <optional>, "ttl_s": 2592000 }` → 调 `abandon_expired`，返回 `{abandoned}` |
| `/knowledge-bases/{kb_id}/diagnostics` | GET | `chunk_diagnostics(tenant)` + 扩展：`{embed_failed_aging: {<"1h"|"1d"|"7d"|"older">: N}, failed_bucket_stats}` |
| `/knowledge-bases/admin/embed-failed/list` | GET | `{ "kb_id": <optional>, "limit": 50 }` → 返回 embed_failed chunk 列表（含 source / text 片段 / failed_count / retried_at）**用于排查** |

**鉴权**：这些是管理面接口，当前项目管理面统一不加额外中间件
（已有 AUTH/01 全链路）；本卡不多加，保持一致性。

### 2.4 可观测（Prometheus `/metrics`）

在 `app/infrastructure/observability/metrics.py` 加：

* `kb_embed_failed_retry_total`（counter）— 累计重试次数
* `kb_embed_failed_fixed_total`（counter）— 重试成功数
* `kb_embed_failed_abandoned_total`（counter）— 放弃数
* `kb_embed_failed_age_seconds`（histogram）— 当前 embed_failed chunk 的年龄分布

### 2.5 可配置项（`app/config.py`）

| 配置 | 默认 | 含义 |
|---|---|---|
| `embed_retry_max` | 3 | 放弃前的最大重试次数 |
| `embed_retry_batch` | 100 | 单次调度的最大重试数 |
| `embed_retry_interval_s` | 3600 | 后台调度间隔（秒） |
| `embed_failed_ttl_s` | 2592000 (30 天) | embed_failed chunk 的最长存活时间 |

---

## 3. 边界（不做）

* **不**在 Milvus 后端实现：`MilvusKnowledgeStore` 仍用 `status` 向量字段以外的
  schema 管理 — 本卡只在 `KnowledgeStore`（SQLite）上做自愈；
  Milvus 端 `retry_embed` / `abandon_expired` 返回 `{}`（鸭子类型兼容，防 admin API 调用时报 `AttributeError`）。
* **不**做嵌入失败根因诊断：`status_reason='embed_unavailable'` 已是终态
  （预加载超时 vs 模型缺失 vs 联网挂起，不细分）。
* **不**主动**重做 bad chunk**（`empty`/`noise`）：它们是**内容本身的问题**，不是嵌入的问题。
* **不**调度 `cleanup_old_versions`：那是D57已有的独立接口。
* 前端**不加**自愈按钮：本卡只加 API + 日志；前端消费留 D59。

---

## 4. 测试矩阵（TDD）

### 4.1 KnowledgeStore 方法（unit）

| 用例 | 断言 |
|---|---|
| `test_embed_failed_tracks_retried_at_and_count` | 嵌入失败时 `failed_count=1, retried_at` 被设置 |
| `test_retry_fixed_flips_status_to_ok` | 重试成功 → `status='ok'`, `vec` 非空 |
| `test_retry_failure_increments_count` | 连续失败次数累加 |
| `test_retry_exceeds_max_marks_abandoned` | 失败次数 > `embed_retry_max` → `status='abandoned'` |
| `test_retry_respects_kb_filter` | `retry(kb_id=OTHER)` 不触发本库的 embed_failed |
| `test_retry_does_not_retry_abandoned` | `abandoned` 的 chunk 不被重试 |
| `test_abandon_expired_marks_old_chunks` | `retried_at` 早于 TTL 的 chunk → `abandoned` |
| `test_abandon_keeps_fresh_chunks` | 最近重试过的 `embed_failed` 不被放弃 |

### 4.2 scheduler 模块（单元 + 集成）

| 用例 | 断言 |
|---|---|
| `test_retry_once_respects_batch_limit` | 即使有 200 个 embed_failed，单次 ≤ `max_chunks` 条 |
| `test_retry_once_returns_correct_counts` | 返回字典键全对 + 值与实际一致 |
| `test_background_scheduler_starts_and_stops` | 启动 daemon 线程；`join(timeout=...)` 后干净退出 |

### 4.3 运维 API

| 用例 | 断言 |
|---|---|
| `test_admin_retry_endpoint_returns_stats` | POST `/admin/embed-failed/retry` → 200，返回字段正确 |
| `test_admin_abandon_endpoint_returns_count` | POST `/admin/embed-failed/abandon` → 200，`{abandoned: N}` |
| `test_kb_diagnostics_includes_aging` | GET `/{kb_id}/diagnostics` → 含 `embed_failed_aging` 字典（四桶） |
| `test_list_embed_failed_returns_rows` | GET `/admin/embed-failed/list` → 返回行含 source/text 片段/failed_count |

### 4.4 Milvus 鸭子类型兼容

| 用例 | 断言 |
|---|---|
| `test_milvus_no_attribute_error_on_self_healing` | `MilvusKnowledgeStore.retry_embed()` / `abandon_expired()` 不抛 `AttributeError` |

---

## 5. TDD 红 → 绿顺序

① 写 `tests/test_e8_embed_self_healing.py`（含 §4 所有用例）→ 跑红
② 实现 schema 扩展 + `retry_embed` / `abandon_expired` / 状态机 → 绿
③ 写 `app/core/tools/embed_scheduler.py` → 绿
④ 在 `app/api/routes/knowledge.py` 加 API → 绿
⑤ 加 Prometheus 指标 → 绿
⑥ 加 config 项 → 绿
⑦ Milvus 后端补零方法 → 绿
⑧ 跑全量回归 + mock eval → 确认无回退

---

## 6. 回归门禁与 DoD

**D58 收工门禁**：

```bash
pytest tests/test_e8_embed_self_healing.py -v          # 本体
pytest tests/test_e8_knowledge_depth.py -v             # D57 用例不回归
pytest tests/test_kb_management_api.py tests/test_rag_confidence.py tests/test_tenant.py -v
                                                      # 邻接面
pytest tests/ -q --ignore=tests/test_agent_real.py    # 全量
python -m app.eval.runner --mode mock                 # eval 基线
```

**DoD**：

* `docs/specs/E8/02-embed-self-healing.md` ✅（契约已定义）
* `embed_failed` 生命周期：标记 → 重试 → (ok / abandoned) ✅
* 运维 API（retry / abandon / diagnostics / list）✅
* 手动 + 后台自愈两种触发路径 ✅
* Milvus 端无 `AttributeError` ✅
* 回归 + eval 无回退 ✅
* `docs/progress/DailyLog.md` 追加 D58 节 ✅
* `docs/对标企业级Gap.md` §二 ❌ 残留项更新 ✅
