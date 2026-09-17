# E8/03 嵌入向量版本迁移

> 版本 v1 · 2026-09-15 · D59
> 依据：`docs/对标企业级Gap.md` §二 残留（**索引一致性、embedding 版本迁移、权限对齐**）
> 前置：E8/01（D57）、E8/02（D58）已落地

---

## 1. 背景与动机

当前知识库（`KnowledgeStore`）把嵌入向量当作永恒不变的：一旦 chunk 入库，它就永远参与检索 — 即使底层嵌入模型换了名字、维度、甚至权重。

实际生产升级的两种情形：

1. **模型名/版本切换**：`all-MiniLM-L6-v2`（384 维）→ `bge-large-zh-v1.5`（1024 维）。旧 vec 跟 query vec 维度不匹配，点积非法。
2. **同模型权重刷新**：同一模型发行了权重刷新（hypothetical），旧向量与新版 query cos 距离失去可比性。

若不做版本跟踪，升级后：
- 要么**全量重嵌入**（昂贵、停机），
- 要么**混维检索**（无意义打分）；
- 且无法知道哪些 chunk 是陈旧向量、哪些已经跟到新版。

---

## 2. 契约

### 2.1 设计原则

| 条目 | 约束 |
|---|---|
| 版本号格式 | 字符串 `"v1"` / `"v2"`，不强制整数；可使用 `"bge-large-v1.5"` 之类可辨识标签 |
| 版本戳位置 | chunks 表新增 `embed_model_version TEXT` 列，每次 add/update 写入版本快照 |
| 当前活跃版本 | `Settings.embed_model_version` 全局值；`rotate(new_version)` 只更新 settings，不自动 reembed 任何 chunk |
| 检索隔离 | `search()` 默认只命 `embed_model_version = 当前 settings` 的 chunk；参数 `include_stale_versions=True` 兼容期回退 |
| **不**变 D58 `embed_failed` 语义 | 旧版本 chunk 是「维度失效」不是「嵌入失败」；重试用 D58 `retry_embed()` |
| Milvus 后端 | Milvus collection 维度硬约束（创建时定死）；当前 Milvus stub 路径**不支持**「同 collection 同维度」之外的变化 — 显式 `NotImplementedError("collection dimension ... is fixed; create a new collection for a new model")`；SQLite 路径是主实现 |

### 2.2 Schema 扩展

chunks 表新增一列：

```sql
ALTER TABLE chunks ADD COLUMN embed_model_version TEXT
```

- `add()` 自动填 `get_settings().embed_model_version`。
- 老数据（NULL）处理：默认当 `embed_model_version IS NULL` 视为第一代 `"v1"`（兼容期 `include_stale_versions` 默认 True 时不过滤；强制隔离模式时要先把 NULL chunk 捞出来 mark）。
- 索引：`CREATE INDEX IF NOT EXISTS chunks_emv ON chunks(embed_model_version, kb_id)` — 为按版本/库快速过滤准备，也为 migration 扫描准备。

### 2.3 Settings

```python
# D59 嵌入版本迁移：活跃版本标签
embed_model_version: str = "v1"
```

- 独立于 `embed_model`（模型名）；运维可自行决定何时升版本号。
- `rotate(new_version)` admin 调用只改 settings，后续新 add 自动挂新标签。

### 2.4 KnowledgeStore 新方法

| 方法 | 返回 | 失败语义 |
|---|---|---|
| `get_current_embed_version() -> str` | 当前 settings.embed_model_version | 不抛 |
| `version_stats(kb_id=None) -> dict[str,Any]` | `{current, distribution: {ver: count}, stale_count, total}` | 空库 `{current, distribution:{}, stale_count:0, total:0}` |
| `reembed_chunk(chunk_id) -> dict[str,int]` | `{status_before("ok"\|"embed_failed"\|"stale_versions"\|...), version_before, version_after, ok}` | chunk 不存在 → `{"ok":0,"reason":"missing"}` |
| `reembed_batch(kb_id=None, target_version=None, limit=None) -> dict[str,int]` | 见 §2.6 | limit<=0 返回 `{…0…}` |

### 2.5 `search()` 版本隔离

```python
def search(self, query, top_k*, tenant=None, kb_id=None,
           include_stale_versions=False, ...):
```

- `include_stale_versions=False`（默认）：添加 WHERE 过滤
  `embed_model_version IS NULL OR embed_model_version = <current>`
  — 让 NULL 历史数据仍可见，避免一升级就整库空。
- 调用方若想「只看同版本 chunk」，在 `rag/retrieve.py` 透传 `include_stale_versions=False` 即可。
- 里程碑：未来 K 计划可让 `include_stale_versions` 默认 True（兼容期） → False（硬隔离）；D59 默认 False 是安全路径。

### 2.6 Reembed 流程

`reembed_chunk(chunk_id)`:

1. SELECT chunk by id（含 vec、embed_model_version、status）。
2. 缺失 → 返回 `{ok:0, reason:"missing"}`。
3. 成功路径：
   - `vec_before = chunk.vec`
   - `emb = _embed(chunk.text)`（复用既有 embed；返回 None 则保留旧版本、status 原地不变，返回 `{ok:0, reason:"embed_error"}`——不扣 failed_count，因为 version 维度是"自然失效"不是 embed 失败）
   - 成功：UPDATE `vec=?, embed_model_version=?current?, status='ok', failed_count=0, retried_at=now`
   - 返回 `{status_before, version_before, version_after, ok:1}`

> D58 的 `retry_embed()` 重试路径**也**在成功时设 `embed_model_version=?current?` — 因为旧版本 chunk 如果成功 retry 就自然升级到最新版。失败/abandon 不变。D59 要同步更新 D58 的这个方法（见 §3.1 改动清单）。

### 2.7 Admin API

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/knowledge-bases/admin/embed-version` | 返回 settings 当前 version + distribution + stale_count |
| POST | `/knowledge-bases/admin/embed-version/rotate` | body `{new_version: str}` → 更新 `kt.get_settings()` 实例字段（运行时；不影响 .env 文件)。返回 `{current: new_version, previous}`。 |
| POST | `/knowledge-bases/admin/chunks/{chunk_id}/re-embed` | body `{}` → `reembed_chunk(chunk_id)` 单条，返回 `{status_before, version_before, version_after, ok}` |
| POST | `/knowledge-bases/admin/embed-version/migrate` | body `{kb_id?, limit?}` → `reembed_batch(...)`，返回 `{migrated, failed, skipped}` |

> 鉴权：所有 admin 路由由 `app.api.middleware.auth_middleware` 统一校验（若 `auth_enabled=True`）。
> rotate 只改运行时；不支持写回 .env；进程重启恢复 `config.py` 默认。

### 2.8 Milvus 后门 stub

`MilvusKnowledgeStore` 上三个新方法兜底：

```python
def get_current_embed_version(self) -> str:
    return get_settings().embed_model_version

def version_stats(self, kb_id=None) -> dict[str, Any]:
    return {"current": get_settings().embed_model_version,
            "distribution": {}, "stale_count": 0, "total": 0}

def reembed_chunk(self, chunk_id) -> dict[str, int]:
    return {"ok": 0, "reason": "milvus_not_implemented"}

def reembed_batch(self, kb_id=None, target_version=None, limit=None) -> dict[str, int]:
    return {"migrated": 0, "failed": 0, "skipped": 0,
            "reason": "milvus_collection_dimension_fixed"}
```

Milvus **搜索**路径：因维度硬约束不能在运行时动态过滤旧版本 — Milvus 路径不强制加版本 where；由 Milvus 之上层保证。

---

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| `app/config.py` | 新增 `embed_model_version: str = "v1"` |
| `app/core/tools/knowledge_tool.py` | 懒迁移加列 + 索引；`add()` 写入 version；`search()` WHERE；`reembed_chunk` / `reembed_batch` / `version_stats`；`retry_embed()` 成功路径也设 version=current |
| `app/api/routes/knowledge.py` | 4 个 admin 端点 |
| `app/core/tools/knowledge_tool.py` Milvus 后门 | 三个新方法兜底 + `version_stats` |

---

## 4. 测试矩阵

| # | 用例 | 预期 |
|---|---|---|
| V1 | add() 写入 chunk → `embed_model_version == settings.embed_model_version` | SQLite 路径 schema 自动填 |
| V2 | 设置 chunk `embed_model_version="v1"` → 升 `settings.embed_model_version="v2"` → search 不再命中旧版 chunk（include_stale_versions=False） | 隔离生效 |
| V3 | include_stale_versions=True 时旧版 chunk 仍命中 | 兼容路径 |
| V4 | `version_stats()` 返回正确的分布 `{v1:1, v2:1}` + stale_count==1 | 按 settings 判定 stale |
| V5 | `reembed_chunk` 后 chunk.version == current + vec 非空 + version_before==v1 | 单条升级 |
| V6 | `reembed_batch` 把多个 v1 chunk 批量升到 v2 | {migrated:N} |
| V7 | reembed 遇到 embed 返回 None → chunk 保留旧版本（status 不变） | {ok:0, reason:"embed_error"} |
| V8 | reembed 不存在的 chunk → {ok:0, reason:"missing"} | |
| V9 | admin rotate 更新 current（下次 add 用新标签） | GET admin/embed-version 反映变化 |
| V10 | admin re-embed 端点 | 走通 API |
| V11 | admin migrate 端点 | 走通 API |
| V12 | Milvus stub 三个新方法可调用且不抛 AttributeError | duck-typing |
| V13 | D58 retry_embed 成功路径也设 version=current | 回归 |

---

## 5. DoD / 收工门禁

- [ ] `tests/test_e8_embed_versioning.py` 全部 GREEN
- [ ] `tests/test_e8_embed_self_healing.py`（D58）全绿
- [ ] `tests/test_e8_knowledge_depth.py`（D57）全绿
- [ ] 全量 offline 回归 `--ignore=tests/test_agent_real.py` 不新增失败
- [ ] `eval --mode mock` 门禁 PASS（不打断 FINISH/断言/幻觉基线）
- [ ] `DailyLog.md` 追加 D59 节；`Gap doc §二` 新增 D59 证据；残留降为「索引一致性 / 结构权限对齐 / Milvus 维度」

---

## 6. Boundary（**不做**）

- LLM 调用路径重写：`rag/retrieve.py` 不强制加 `include_stale_versions=False` 默认行为（D59 仅做数据面；调用方适配是 K 计划）
- 多跳检索（query decomposition）/ query 改写 — 属于 RAG 能力深度，归入 E9
- 向量与源文档权限对齐：hook 点已存在（kb_id / tenant），authz 推送失效不在 D59 范围
- 自动 scheduler：版本迁移不落 D58 调度器；D59 只做触发式 API
