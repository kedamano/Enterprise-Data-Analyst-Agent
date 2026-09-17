# E8/01 知识库深度：表格感知 + 索引版本 + 坏 chunk 回流

> 版本 v1 · 2026-09-15 · D57
> 依据：`docs/对标企业级Gap.md` §二 ❌（无表格/版面理解、父子 chunk、索引版本、坏 chunk 回流）

---

## 1. 背景与动机

当前知识库 ETL（`app/etl/chunker.py` + `app/core/tools/knowledge_tool.py`）存在三处结构性缺口：

1. **表格被当作纯文本切分**：HTML `<table>` / Markdown `|` 表 / CSV 数据行被字符滑窗任意切断，一个 chunk 里可能只有半行数据且缺失表头——检索命中后模型拿到的是无结构上下文。
2. **无索引版本**：`delete_source` + 重新入库 = 旧 chunk 全部丢失。无法做增量更新（同一文件改了某几段时不该全量重建），也无法回滚或对比两个版本的差异。
3. **坏 chunk 静默入库**：嵌入失败 → `vec=NULL` 仍入库，只剩 BM25 通道；空 chunk / 纯标点 chunk / 被截断到无意义的 chunk 不做任何标记——检索结果里混着低质量片段，模型仍会引用。

---

## 2. SDD 契约

### 2.1 表格感知分块（Table-Aware Chunking）

| 条目 | 契约 |
|---|---|
| 输入 | MD / HTML / CSV 文本（已 `parse_file` 后） |
| 表格检测 | 三种格式：① Markdown 表（连续 `|...\|...|` 行，含至少一个 `|---\|---|---|` 分隔行）；② HTML `<table>`（`\<table>...\</table>`，含 `<tr>`/`<td>`）；③ CSV（纯文本但首行后 N 行逗号数一致 ≥2） |
| 分块单元 | 表格以**行组**为单元：表头（1 行）+ 行组（每组 ~chunk_size 字符，默认同纯文本 600 字符，每行不可拆分——整行保留） |
| 行 chunk 格式 | `"<table_prefix>\n<header>\n<rows>"`，其中 `table_prefix` 是紧邻表格前的最后一段非表文本（最多 1 句，作为语义前缀注入） |
| 非表文本 | 沿用既有滑窗逻辑不动 |
| 失败兜底 | 表格解析异常（畸形表、单列表、嵌套异常）→ 回退纯文本滑窗，不丢数据 |

**输出**：`list[str]`，每个 str 要么是非表滑窗 chunk，要么是带表头 context 的表格行组 chunk。

### 2.2 索引版本（Index Versioning）

| 条目 | 契约 |
|---|---|
| schema 变更 | `chunks` 表新增 `version INTEGER NOT NULL DEFAULT 1` 列（懒迁移，`ALTER TABLE` + 默认值） |
| `add` 行为 | 相同 `(source, text, tenant)` 在 `version=N` 已存在 → 不重复插入（内容去重，返回已有 id） |
| `rebuild_source(source, text)` 行为 | ① 给该 source 现有 chunk 的 version 号 +1；② 以最新版本号入库新 chunk；③ 旧版本保留但标记 `deprecated=1`（新列，默认 0，rebuild 时把旧 version 行置 1） |
| `search` 行为 | **只检索 `deprecated=0` 的 chunk**；旧版本不参与打分 |
| 清理 | `cleanup_old_versions(source, keep=2)` 物理删除 version < MAX-version+1 且 deprecated=1 的行（keep=2 保留最近两个版本，防重建失败时可回滚） |
| 失败兜底 | 版本列未就绪（老库无列）时等价于 `version=1, deprecated=0`（懒迁移默认值保证）；rebuild 事务内完成，中途失败回滚不产生半新半旧 |

### 2.3 坏 chunk 回流（Bad Chunk Feedback）

| 条目 | 契约 |
|---|---|
| schema 变更 | `chunks` 表新增 `status TEXT NOT NULL DEFAULT 'ok'` + `status_reason TEXT` + `vec NULL` 已有（不新增） |
| status 枚举 | `'ok'` / `'empty'` / `'noise'` / `'embed_failed'` / `'deprecated'` |
| 入库时检测 | ① 文本 strip 后空 → `status='empty', status_reason='zero_length'`；② 文本 strip 后纯标点/空白占比 >80% 或字符数 <10 → `status='noise', status_reason='low_signal_ratio'`；③ `_embed` 返回 `None`（模型不可用 / 超时 / 异常）→ 仍入库但 `status='embed_failed', status_reason='<错误类型>'` |
| 检索过滤 | `search` 默认**不返回** `status != 'ok'` 的 chunk（BM25 通道里坏 chunk 仍参与打分但不出现在最终结果） |
| 诊断接口 | `GET /analyze/kb/diagnostics/{source}`（或集成进 `kb_status`）返回 `{total, ok, empty, noise, embed_failed}` 统计 |
| 修复入口 | `rebuild_source` 重跑时清洗旧坏 chunk；`run()` 工具响应里 `metadata['kb_chunk_quality'] = {ok:N, filtered:M}` 让调用方知道本轮过滤了多少 |

---

## 3. TDD 测试矩阵

| 测试 | 契约覆盖 | 负路径 |
|---|---|---|
| `test_table_markdown_chunked_by_row` | MD 表 → 行组 chunk + 表头 context | — |
| `test_table_html_preserved` | HTML `<table>` → 不切断 `<tr>` | — |
| `test_malformed_table_fallback_to_text` | 畸形表 → 回退纯文本滑窗 | 不丢数据、不抛错 |
| `test_rebuild_source_no_dup` | 同内容二次 rebuild → 不重复（内容 hash）| — |
| `test_old_version_deprecated_excluded_from_search` | rebuild 后旧版本 chunk 不出现在 search 结果 | — |
| `test_cleanup_old_versions` | keep=2 物理删掉第三旧版本 | — |
| `test_empty_chunk_marked_noise` | 空 / 纯标点 → status='noise' | 不出现在 search 结果 |
| `test_embed_failed_chunk_stored_with_status` | 嵌入失败 → status='embed_failed' | BM25 仍可检索 |
| `test_backward_compat_no_version_col` | 老库（无 version 列）懒迁移默认值 | 不抛错 |

---

## 4. 实现要点

- `chunker.py`：新增 `chunk_structured(text)` 入口（检测格式→分流），`chunk_text` 保留为纯文本兜底。HTML 表用简单正则（`re.DOTALL`），不用 lxml/beautifulsoup 等新依赖。
- `knowledge_tool.py`（SQLite 路径）：`__init__` 加两步懒迁移（`version` 列 + `status` 列 + `deprecated` 列 + `content_hash` 列用于去重）；`add` 增加入前检测（空 / 噪声 / 嵌入失败）+ 去重检查；`search` 默认 `WHERE deprecated=0 AND status='ok'`；新增 `rebuild_source` / `cleanup_old_versions` / `chunk_diagnostics`。
- `MilvusKnowledgeStore` 对齐：`add` 同样做入前检测（status 写入 entity 字段）；版本由 Milvus 侧 `tenant+source+version` composite key 处理——Milvus 路径的 versioning 为**最佳努力**（不阻塞主流程），主路径是 SQLite。
- `etl/pipeline.py` `ingest_text` 换用 `chunk_structured`。
- `app/core/security/audit_store`：修改事件记一条（不强制）。

---

## 5. 边界与不做什么

- **不做 OCR**：图片/扫描 PDF 的文字提取留给 pypdf + 外部工具（D57 不做）。
- **不做版面理解**：不识别段落层级、标题树（那是 D58+ 候选）。
- **不做 embedding 重试的自动调度**：`embed_failed` 只标记，人工通过 `rebuild_source` 重跑。
- **Milvus 路径 versioning 不是强一致**：SQLite 是 truth source，Milvus 是缓存/加速层；rebuild 时全删旧 source 即可（利用 `delete_source`）。

---

## 6. DoD

- [ ] 规格文档（本文件）✅
- [ ] 红绿测试（正+负，TDD 先红）
- [ ] 全量回归无回退（`pytest tests/ -q --ignore=tests/test_agent_real.py`）
- [ ] eval mock 基线不变
- [ ] DailyLog D57 追加
- [ ] `对标企业级Gap.md` §二 ❌ → ⚠️ 回写
