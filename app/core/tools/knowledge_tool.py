"""``knowledge_search`` tool – RAG over the enterprise knowledge base.

When Milvus is configured the vector store is used; otherwise a local SQLite
store with optional sentence-transformers embeddings (falling back to keyword
overlap scoring) keeps the service fully functional offline. This honours the
spec's principle: *knowledge is not data* – it answers metric definitions and
business terminology, never substitutes for actual query results.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ...config import get_settings

# 路径可用 ``KNOWLEDGE_DB_PATH`` 覆盖。默认不变（``data/knowledge.db``）。
# 缺这个开关时测试与旁路实例只能写真实库——与 ``knowledge_catalog`` 同理。
_DB_PATH = Path(os.environ.get("KNOWLEDGE_DB_PATH") or (Path("data") / "knowledge.db"))
_lock = threading.Lock()

# SQLite 与 Milvus 两个后端共用 add/search 鸭子类型接口
knowledge_store_type = Any


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if len(t) > 1]


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


# \u5d4c\u5165\u6a21\u578b\u8fdb\u7a0b\u7ea7\u7f13\u5b58 + \u5931\u8d25\u5373\u7981\u7528\uff08\u907f\u514d\u6bcf\u6b21\u8c03\u7528\u91cd\u65b0\u52a0\u8f7d ~13s\uff0c\u6216\u8054\u7f51\u6821\u9a8c\u6302\u8d77\uff09
_embed_model = None
# ``_embed_error`` 只记录**确定性失败**（依赖缺失 / 加载抛异常）——这类重试也没用。
# ⚠️ **超时绝不能写进它**：超时只说明"还没加载完"，加载线程仍在后台跑；一旦写进去，
#    整个进程此后所有检索都静默退化成纯 BM25，只能重启恢复。这是真实踩过的坑：
#    启动预热遇上慢加载（本机缓存模型要 ~17s，超时阈值 25s）→ 服务永久降级。
_embed_error: str | None = None
# 后台加载线程与它的结果容器：超时返回后线程可能仍在跑，下次调用据此接管结果。
_embed_thread: threading.Thread | None = None
_embed_holder: dict[str, object] = {}


def _get_embed_model():
    """懒加载嵌入模型，带墙钟预算——绝不让入库/检索被模型加载无限阻塞。

    后台线程加载（先离线缓存，缓存缺失再在线），主线程 ``join(timeout)``：

    * 加载成功 → 进程级缓存，后续零成本。
    * **超时 → 不阻塞、不永久禁用**：加载线程继续在后台跑，本次返回 ``None``
      （这一轮退化为纯 BM25），下次调用若线程已跑完会自动接管结果。
      把超时当成永久失败写入 ``_embed_error``，会让整个进程此后静默降级——
      实测本机缓存模型加载约 17s、超时阈值 25s，边距很小，一旦抖动就中招。
    * 仅**确定性失败**（sentence_transformers 不可用 / 加载抛异常）才置
      ``_embed_error`` 永久禁用，避免每次请求反复重试白等。
    * 已有加载线程在跑时，其他调用**立即返回 None**、不重复 ``join``。
    """
    global _embed_model, _embed_error, _embed_thread, _embed_holder
    if _embed_model is not None:
        return _embed_model
    if _embed_error is not None:
        return None
    # 已有加载线程在跑 → 立即降级返回，**不再 join**。否则每个并发调用各付一次
    # embed_load_timeout_s：实测多个 TestClient 启动预热会让套件从 ~50s 涨到 450s+。
    if _embed_thread is not None:
        if _embed_thread.is_alive():
            return None
        # 线程已结束但还没人接管结果（上一轮超时留下的）→ 在此补齐
        _late = _embed_holder.get("model")
        if _late is not None:
            _embed_model = _late
            return _embed_model
        _late_err = _embed_holder.get("error")
        if _late_err is not None:
            _embed_error = f"嵌入模型加载失败（已降级为纯 BM25 检索）: {_late_err}"
            return None
    try:
        from sentence_transformers import SentenceTransformer  # lazy
    except Exception as exc:
        _embed_error = f"sentence_transformers \u4e0d\u53ef\u7528: {exc}"
        return None

    holder: dict[str, object] = {}

    def _load() -> None:
        last: Exception | None = None
        # \u79bb\u7ebf\u7f13\u5b58\u4f18\u5148\uff1b\u7f13\u5b58\u7f3a\u5931\u624d\u5728\u7ebf\u4e0b\u8f7d\uff08\u9996\u6b21\u8fd0\u884c\uff09\u3002
        for offline in (True, False):
            os.environ["HF_HUB_OFFLINE"] = "1" if offline else "0"
            os.environ["TRANSFORMERS_OFFLINE"] = "1" if offline else "0"
            try:
                holder["model"] = SentenceTransformer(get_settings().embed_model)
                return
            except Exception as exc:  # noqa: PERF203
                last = exc
                if offline:
                    continue
        holder["error"] = last or RuntimeError("embed load failed")

    t = threading.Thread(target=_load, daemon=True)
    _embed_thread = t
    _embed_holder = holder      # 同一个 dict 对象：加载线程的写入随后对外可见
    t.start()
    t.join(get_settings().embed_load_timeout_s)
    if t.is_alive():
        # ⚠️ 超时**不置 _embed_error**：线程仍在后台加载，写进去等于把整个进程
        #    永久降级为纯 BM25（无声的检索质量下降）。下次调用自动接管加载结果。
        return None
    if "model" in holder:
        _embed_model = holder["model"]
        return _embed_model
    if "error" in holder:
        _embed_error = f"嵌入模型加载失败（已降级为纯 BM25 检索）: {holder['error']}"
    return None


def _embed(text: str) -> list[float] | None:
    model = _get_embed_model()
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def warm_up_embedder() -> None:
    """后台预热嵌入模型（幂等）。

    首次碰 ``_embed`` 时才会去加载模型，本机实测要 ~17s（离线缓存优先，缺失才在线
    下载）。把这次「一次性首帧代价」提前到服务启动后的后台线程里付掉，用户第一次
    「入库」或「检索」就不必干等。

    预热**只负责提速，绝不影响可用性**：超时不代表失败，加载线程会继续在后台跑，
    后续调用会自动接管结果（见 ``_get_embed_model``）。失败也无所谓——混合检索的
    BM25 通道可独立工作。
    """
    try:
        _get_embed_model()
    except Exception:
        pass  # 预热仅为提速，绝不影响可用性


# --------------------------------------------------------------------------- #
# Hybrid retrieval: Okapi BM25 (keyword) + cosine (vector) fused with RRF
# （project-python MultiRetriever 的 HYBRID 模式同款思路）
# --------------------------------------------------------------------------- #
def _retrieve_tokens(text: str) -> list[str]:
    """词级 token + CJK 重叠二元组，弥补无空格中文整句被当一个词的问题。

    例：``"营收是订单金额"`` → 词级 1 个长词，bigram 层贡献 营收/收是/是订/订单…，
    使 BM25 对中文短语有判别力。
    """
    words = _tokenize(text)
    extras: list[str] = []
    for w in words:
        if len(w) > 2 and re.search(r"[一-鿿]", w):
            extras.extend(w[i:i + 2] for i in range(len(w) - 1))
    return words + list(dict.fromkeys(extras))  # bigram 去重，避免 tf 虚高


def _bm25_scores(query: str, docs: dict[int, str],
                 k1: float = 1.5, b: float = 0.75) -> dict[int, float]:
    """Okapi BM25 over retrieval tokens. Returns non-zero scores only."""
    q_tokens = _retrieve_tokens(query)
    if not q_tokens or not docs:
        return {}
    doc_tokens = {i: _retrieve_tokens(d) for i, d in docs.items()}
    n_docs = len(doc_tokens)
    avgdl = sum(len(t) for t in doc_tokens.values()) / n_docs
    df: dict[str, int] = {}
    for toks in doc_tokens.values():
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scores: dict[int, float] = {}
    for i, toks in doc_tokens.items():
        dl = len(toks)
        s = 0.0
        for qt in q_tokens:
            tf = toks.count(qt)
            if tf == 0:
                continue
            idf = math.log((n_docs - df.get(qt, 0) + 0.5) / (df.get(qt, 0) + 0.5) + 1)
            s += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * (dl / avgdl)))
        if s > 0:
            scores[i] = s
    return scores


def _rrf_scores(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion 得分表：score(d) = Σ 1 / (k + rank_i(d))."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused


def _rrf_fuse(rankings: list[list[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion：多路排名融合为单一有序文档列表."""
    scores = _rrf_scores(rankings, k)
    return sorted(scores, key=scores.get, reverse=True)


def _resolve_tenant(tenant: str | None) -> str:
    """租户归一化：显式传入优先，否则用配置的 default_tenant，再否则全局模式（空）。"""
    return (tenant or "").strip() or (get_settings().default_tenant or "").strip()


# ── D59 嵌入版本（EMV）运行时覆盖 ────────────────────────────────────────────
# 版本号是**字符串标签**（"v1"/"bge-v1.5"），不是数值。写入分块时把当时的标签快照
# 进 embed_model_version 列；检索只用「当前版本」的分块，stale 分块不参与 RRF 融合
# ——否则不同嵌入空间算出的余弦相似度会混在一起，得到无意义的排序。
#
# rotate 只改这里的**进程内存态**，重启即恢复 settings 默认。这是有意设计：版本迁移
# 是需要人工决策的运维动作（先 rotate 观察分布，再批量 re-embed），不该在某次重启后
# 自动对全库生效。
_emv_override: str | None = None


def _resolve_emv() -> str:
    """当前活跃的嵌入模型版本标签：运行时覆盖优先，否则用 settings 默认。"""
    return _emv_override or get_settings().embed_model_version


def set_emv_override(version: str) -> str:
    """设置运行时版本覆盖，返回设置**之后**的当前版本（rotate 接口据此回报 current）。

    空值视为清除覆盖、回到 settings 默认——避免 rotate 传空串把库悬空在一个既非默认、
    也不存在于任何分块的标签上（那会让检索结果恒为空）。
    """
    global _emv_override
    _emv_override = (version or "").strip() or None
    return _resolve_emv()


class KnowledgeStore:
    """SQLite-backed knowledge store with table-aware chunking, versioning, and
    chunk-quality tracking.

    Schema (lazy-migrated):
        id, source, text, tokens, vec, tenant,
        version INTEGER DEFAULT 1,         # D57 chunk 内容版本（rebuild 自增）
        status TEXT DEFAULT 'ok', status_reason TEXT,
        deprecated INTEGER DEFAULT 0,
        content_hash TEXT,
        failed_count INTEGER DEFAULT 0,     # D58 嵌入失败计数
        retried_at INTEGER,                 # D58 最近重试时间
        kb_id TEXT,                         # D57 所属知识库
        embed_model_version TEXT            # D59 写入时嵌入模型版本快照
    """

    # status values
    STATUS_OK = "ok"
    STATUS_EMPTY = "empty"
    STATUS_NOISE = "noise"
    STATUS_EMBED_FAILED = "embed_failed"
    STATUS_ABANDONED = "abandoned"


    def __init__(self, db_path: Path | None = None) -> None:
        self.db = str(db_path or _DB_PATH)
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        with _lock, sqlite3.connect(self.db) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, source TEXT, text TEXT,
                    tokens TEXT, vec TEXT)"""
            )
            # Lazy migrations — every ADD is idempotent (only adds if missing)
            cols = {r[1] for r in c.execute("PRAGMA table_info(chunks)").fetchall()}
            if "tenant" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN tenant TEXT")
            if "version" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
            if "status" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'")
            if "status_reason" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN status_reason TEXT")
            if "deprecated" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0")
            if "content_hash" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN content_hash TEXT")
            if "failed_count" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0")
            if "retried_at" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN retried_at INTEGER")
            # 多知识库（KB）：分块归属某个知识库；NULL = 历史遗留分块，由
            # KnowledgeCatalog.ensure_seed() 在首次使用时接管到默认库。
            # 建索引是因为「按库检索 / 按库统计」是知识库详情页的主路径。
            if "kb_id" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN kb_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS chunks_kb ON chunks(kb_id)")
            # D59 嵌入版本迁移：写入时的嵌入模型版本；独立于 content version。
            # WHERE embed_model_version=current OR IS NULL 隔离检索，stale chunk 不贡献 RRF。
            if "embed_model_version" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN embed_model_version TEXT")
            c.execute(
                "CREATE INDEX IF NOT EXISTS chunks_emv "
                "ON chunks(embed_model_version, kb_id)")

    @staticmethod
    def _resolve_tenant(tenant: str | None) -> str:
        return _resolve_tenant(tenant)

    def add(self, text: str, source: str, tenant: str | None = None,
            version: int = 1, kb_id: str | None = None) -> int:
        """Insert one chunk with quality gate + dedup.

        Returns the row id (>=0). Bad chunks (empty / noise) are still stored
        (so diagnostics can count them) but marked with ``status != 'ok'``.
        Dupes (same source+tenant+kb+content_hash at the same version) return
        the existing row id.

        ``kb_id``（多知识库）：分块归属的知识库。**参与去重与版本判定**——
        两个库各自上传同名文件时互不覆盖，这是"同库同来源才算同文档"的前提。
        """
        stripped = (text or "").strip()
        # ── quality gate (before embedding, no wasted compute) ───────────
        status, status_reason = self._classify_chunk(stripped)
        ten = self._resolve_tenant(tenant)
        content_hash = _sha1(stripped)
        now_ts = int(time.time())
        # dedup check (same version only)
        with _lock, sqlite3.connect(self.db) as c:
            dup = c.execute(
                "SELECT id FROM chunks WHERE source=? AND tenant IS ? "
                "AND kb_id IS ? AND content_hash=? AND version=? AND deprecated=0",
                (source, ten or None, kb_id, content_hash, version),
            ).fetchone()
            if dup:
                return int(dup[0])
            vec = None
            reason = status_reason
            failed_count = 0
            if status == self.STATUS_OK:
                emb = _embed(stripped)
                if emb is not None:
                    vec = json.dumps(emb)
                else:
                    status = self.STATUS_EMBED_FAILED
                    reason = "embed_unavailable"
                    failed_count = 1
            tokens = " ".join(_tokenize(stripped))
            retried_at = now_ts if status == self.STATUS_EMBED_FAILED else None
            # D59 版本快照：通过 _resolve_emv() 让 rotate 接口即时生效，
            # 无需重启进程。embed_failed 也记（retry 成功后会被覆盖为新版）。
            current_emv = _resolve_emv()
            cur = c.execute(
                "INSERT INTO chunks(source, text, tokens, vec, tenant, "
                "version, status, status_reason, content_hash, kb_id, "
                "failed_count, retried_at, embed_model_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, stripped, tokens, vec, ten or None, version,
                 status, reason, content_hash, kb_id, failed_count, retried_at,
                 current_emv),
            )
            return int(cur.lastrowid)

    def rebuild_source(self, source: str, text: str,
                       tenant: str | None = None,
                       kb_id: str | None = None) -> dict[str, int]:
        """Reindex *source* with a bumped version.

        Returns ``{"added": N, "version": V}``. Idempotent: re-running
        identical text yields ``added=0`` (content-hash dedup).

        ``kb_id`` 全程参与：版本号、去重、旧版废弃都按 (source, tenant, kb) 三元组
        计算，因此**不同知识库里的同名文件互不干扰**（同库重传才等于更新）。
        """
        from ...etl.chunker import chunk_structured

        ten = self._resolve_tenant(tenant)
        new_version = (
            self._next_version(source, tenant, kb_id)
            if self._has_chunks(source, tenant, kb_id) else 1
        )

        # Idempotency: if latest version already has exactly this content → no-op
        if new_version > 1:
            if self._source_version_matches(source, text, new_version - 1, ten, kb_id):
                return {"added": 0, "version": new_version - 1}
            self._deprecate_source(source, tenant, kb_id)

        chunks = chunk_structured(text)
        added = 0
        for chunk in chunks:
            self.add(chunk, source, tenant=tenant, version=new_version, kb_id=kb_id)
            added += 1
        return {"added": added, "version": new_version}

    def _source_version_matches(self, source: str, text: str, version: int,
                                tenant: str | None,
                                kb_id: str | None = None) -> bool:
        """True if re-chunking *text* produces exactly the same set of content hashes
        as the rows already stored for *source* at *version* (order-insensitive).
        """
        try:
            from ...etl.chunker import chunk_structured
            new_chunks = chunk_structured(text)
            new_hashes = {_sha1(c) for c in new_chunks}
        except Exception:
            return False
        with _lock, sqlite3.connect(self.db) as c:
            existing = c.execute(
                "SELECT content_hash FROM chunks WHERE source=? AND tenant IS ? "
                "AND kb_id IS ? AND version=?",
                (source, tenant, kb_id, version),
            ).fetchall()
            existing_hashes = {r[0] for r in existing if r[0]}
        return bool(new_hashes) and new_hashes == existing_hashes

    def retry_embed(self, kb_id: str | None = None, limit: int | None = None) -> dict[str, int]:
        """Attempt to re-embed ``embed_failed`` chunks.

        Returns ``{retried, fixed, still_failed, abandoned_now}``.
        Chunks exceeding ``embed_retry_max`` are flipped to
        ``STATUS_ABANDONED`` (no longer considered for retry or search).
        """
        max_retries = get_settings().embed_retry_max
        batch = get_settings().embed_retry_batch if limit is None else limit
        ten = self._resolve_tenant(None)
        with _lock, sqlite3.connect(self.db) as c:
            # Fetch candidate ids (oldest retried_at first — they were failed longest)
            base_where = "status=? AND failed_count<=? AND deprecated=0"
            params: list = [self.STATUS_EMBED_FAILED, max_retries]
            if kb_id:
                base_where += " AND kb_id IS ?"
                params.append(kb_id)
            if ten:
                base_where += " AND tenant IS ?"
                params.append(ten)
            rows = c.execute(
                f"SELECT id, text FROM chunks WHERE {base_where} "
                f"ORDER BY retried_at ASC LIMIT ?",
                params + [batch],
            ).fetchall()
            if not rows:
                return {"retried": 0, "fixed": 0, "still_failed": 0, "abandoned_now": 0}

            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            full = c.execute(
                f"SELECT id, text, failed_count FROM chunks WHERE id IN ({placeholders})",
                ids,
            ).fetchall()

        retried = fixed = still_failed = abandoned_now = 0
        now_ts = int(time.time())
        current_emv = _resolve_emv()
        for _id, text, prev_count in full:
            retried += 1
            vec = _embed(text)
            new_count = prev_count + 1
            with _lock, sqlite3.connect(self.db) as c:
                if vec is not None:
                    # D59 顺带升级 embed_model_version：retry 成功后 chunk 视为
                    # 已用当前活跃版本重新嵌入，自然归属到最新版，下次不再被
                    # search 隔离；让旧版 chunk 被 D58 scheduler 自然消化。
                    c.execute(
                        "UPDATE chunks SET status=?, vec=?, failed_count=0, "
                        "status_reason=NULL, retried_at=?, embed_model_version=? "
                        "WHERE id=?",
                        (self.STATUS_OK, json.dumps(vec), now_ts, current_emv, _id),
                    )
                    fixed += 1
                elif new_count > max_retries:
                    c.execute(
                        "UPDATE chunks SET status=?, failed_count=?, "
                        "status_reason='max_retries_exceeded', retried_at=? WHERE id=?",
                        (self.STATUS_ABANDONED, new_count, now_ts, _id),
                    )
                    abandoned_now += 1
                else:
                    c.execute(
                        "UPDATE chunks SET failed_count=?, retried_at=? WHERE id=?",
                        (new_count, now_ts, _id),
                    )
                    still_failed += 1
        return {"retried": retried, "fixed": fixed,
                "still_failed": still_failed, "abandoned_now": abandoned_now}

    def abandon_expired(self, ttl_s: int | None = None,
                        kb_id: str | None = None) -> int:
        """Mark ``embed_failed`` chunks older than ``ttl_s`` as abandoned."""
        ttl = ttl_s if ttl_s is not None else get_settings().embed_failed_ttl_s
        cutoff = int(time.time()) - ttl
        ten = self._resolve_tenant(None)
        with _lock, sqlite3.connect(self.db) as c:
            where = "status=? AND retried_at IS NOT NULL AND retried_at<?"
            params: list = [self.STATUS_EMBED_FAILED, cutoff]
            if kb_id:
                where += " AND kb_id IS ?"
                params.append(kb_id)
            if ten:
                where += " AND tenant IS ?"
                params.append(ten)
            cur = c.execute(
                f"UPDATE chunks SET status=? WHERE {where}",
                [self.STATUS_ABANDONED] + params,
            )
            return int(cur.rowcount)

    # --------------------------------------------------------------------- #
    # D59 嵌入版本迁移
    # --------------------------------------------------------------------- #

    def reembed_chunk(self, chunk_id: int) -> dict[str, int | str]:
        """单条 chunk 升级到当前 ``embed_model_version``。

        返回 ``{"status_before","version_before","version_after", "ok": 0|1,
        "reason":...}``。embed 失败返回 ``{ok:0, reason:"embed_error"}``（不扣
        failed_count，因为版本维度是自然失效不是 embed 失败）；找不到返回
        ``{ok:0, reason:"missing"}``。
        """
        now_ts = int(time.time())
        current_emv = _resolve_emv()
        with _lock, sqlite3.connect(self.db) as c:
            row = c.execute(
                "SELECT text, status, embed_model_version FROM chunks WHERE id=?",
                (chunk_id,)).fetchone()
            if not row:
                return {"ok": 0, "reason": "missing"}
            text, status_before, emv_before = row[0], row[1], row[2]
            emb = _embed(text)
            if emb is None:
                return {"ok": 0, "reason": "embed_error",
                        "status_before": status_before,
                        "version_before": emv_before,
                        "version_after": emv_before}
            c.execute(
                "UPDATE chunks SET vec=?, embed_model_version=?, "
                "status='ok', failed_count=0, retried_at=? "
                "WHERE id=?",
                (json.dumps(emb), current_emv, now_ts, chunk_id),
            )
            return {"ok": 1, "status_before": status_before,
                    "version_before": emv_before,
                    "version_after": current_emv}

    def reembed_batch(self, kb_id: str | None = None,
                      target_version: str | None = None,
                      limit: int | None = None) -> dict[str, int]:
        """批量把旧版本 chunk 升级到当前（或 ``target_version``）embed 版本。

        候选 = ``status='ok' AND embed_model_version IS NOT NULL
               AND embed_model_version < >current`` — 已新版 / NULL 老数据不动。
        返回 ``{"migrated","failed","skipped"}``。skipped = 已是新版或 embed_failed。
        """
        target = target_version if target_version is not None \
            else _resolve_emv()
        batch = limit if (limit is not None and limit > 0) else 200
        now_ts = int(time.time())
        ten = self._resolve_tenant(None)
        with _lock, sqlite3.connect(self.db) as c:
            where = ("status=? AND embed_model_version IS NOT NULL "
                     "AND embed_model_version <> ?")
            params: list = [self.STATUS_OK, target]
            if kb_id:
                where += " AND kb_id IS ?"
                params.append(kb_id)
            if ten:
                where += " AND tenant IS ?"
                params.append(ten)
            rows = c.execute(
                f"SELECT id, text FROM chunks WHERE {where} "
                f"ORDER BY id LIMIT ?",
                params + [batch],
            ).fetchall()
            if not rows:
                return {"migrated": 0, "failed": 0, "skipped": 0}
            ids = [r[0] for r in rows]
            placeholders = ",".join("?" * len(ids))
            full = c.execute(
                f"SELECT id, text FROM chunks WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        migrated = failed = skipped = 0
        for _id, text in full:
            emb = _embed(text)
            with _lock, sqlite3.connect(self.db) as c:
                if emb is None:
                    failed += 1
                    continue
                c.execute(
                    "UPDATE chunks SET vec=?, embed_model_version=?, "
                    "failed_count=0, retried_at=? WHERE id=?",
                    (json.dumps(emb), target, now_ts, _id),
                )
                migrated += 1
        return {"migrated": migrated, "failed": failed, "skipped": skipped}

    def get_current_embed_version(self) -> str:
        """返回当前生效 ``embed_model_version``（运行时覆盖 > settings 默认）。"""
        return _resolve_emv()

    def version_stats(self, kb_id: str | None = None) -> dict[str, Any]:
        """版本分布统计：``{current, distribution:{ver count}, stale_count, total}``。

        stale = 行数中 ``embed_model_version IS NOT NULL`` 且
        ``!= current`` 的数量。
        """
        current_emv = _resolve_emv()
        ten = self._resolve_tenant(None)
        with _lock, sqlite3.connect(self.db) as c:
            args: list = []
            where = ""
            if ten:
                where = "WHERE tenant IS ?"
                args = [ten]
            if kb_id:
                where = f"{where} {'AND' if where else 'WHERE'} kb_id IS ?"
                args = args + [kb_id]
            total = c.execute(
                f"SELECT COUNT(*) FROM chunks {where}", args
            ).fetchone()[0] or 0
            dist = {
                r[0] or "v1": r[1] for r in c.execute(
                    f"SELECT embed_model_version, COUNT(*) FROM chunks "
                    f"{where} GROUP BY embed_model_version",
                    args,
                ).fetchall()
            }
            stale = c.execute(
                f"SELECT COUNT(*) FROM chunks "
                f"{where} {'AND' if where else 'WHERE'} embed_model_version "
                f"IS NOT NULL AND embed_model_version <> ?",
                args + [current_emv],
            ).fetchone()[0] or 0
        return {"current": current_emv, "distribution": dist,
                "stale_count": stale, "total": total}

    def cleanup_old_versions(self, source: str, keep: int = 2,
                             tenant: str | None = None,
                             kb_id: str | None = None) -> int:
        """Physically delete versions older than the *keep* most-recent ones.

        Returns the number of remaining rows for this source.
        """
        ten = self._resolve_tenant(tenant)
        with _lock, sqlite3.connect(self.db) as c:
            # find the version KEEP_THRESHOLD = (max version) - keep + 1
            row = c.execute(
                "SELECT COALESCE(MAX(version), 0) FROM chunks WHERE source=? "
                "AND tenant IS ? AND kb_id IS ?",
                (source, ten or None, kb_id),
            ).fetchone()
            if not row or not row[0]:
                return 0
            threshold = row[0] - keep + 1
            c.execute(
                "DELETE FROM chunks WHERE source=? AND tenant IS ? AND kb_id IS ? "
                "AND version < ?",
                (source, ten or None, kb_id, threshold),
            )
            remaining = c.execute(
                "SELECT COUNT(*) FROM chunks WHERE source=? AND tenant IS ? AND kb_id IS ?",
                (source, ten or None, kb_id),
            ).fetchone()[0]
            return int(remaining)

    def chunk_diagnostics(self, tenant: str | None = None,
                          kb_id: str | None = None) -> dict[str, Any]:
        """Return quality stats: {total, ok, empty, noise, embed_failed, deprecated,
        abandoned, embed_failed_aging: {1h, 1d, 7d, older}}.

        可按 ``tenant`` 或 ``kb_id`` 过滤（两者均为 None = 全局）。"""
        ten = self._resolve_tenant(tenant)
        now = int(time.time())
        with _lock, sqlite3.connect(self.db) as c:
            args: list = []
            where = ""
            if ten:
                where = "WHERE tenant IS ?"
                args = [ten]
            if kb_id:
                where = f"{where} {'AND' if where else 'WHERE'} kb_id IS ?"
                args = args + [kb_id]
            total = c.execute(f"SELECT COUNT(*) FROM chunks {where}", args).fetchone()[0]
            ok = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} status='ok'", args
            ).fetchone()[0]
            empty = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} status='empty'", args
            ).fetchone()[0]
            noise = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} status='noise'", args
            ).fetchone()[0]
            embed_failed = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} status='embed_failed'", args
            ).fetchone()[0]
            deprecated = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} deprecated=1", args
            ).fetchone()[0]
            abandoned = c.execute(
                f"SELECT COUNT(*) FROM chunks {where} {'AND' if where else 'WHERE'} status='abandoned'", args
            ).fetchone()[0]
            # embed_failed_aging histogram (only rows that ARE embed_failed)
            aging = {"1h": 0, "1d": 0, "7d": 0, "older": 0}
            ef_where = f"{'WHERE' if not where else where + ' AND'} status='embed_failed'"
            ef_args = list(args)
            # 1h = last 3600s, 1d = last 86400s, 7d = last 604800s, older = rest
            h1 = c.execute(
                f"SELECT COUNT(*) FROM chunks {ef_where} {'AND' if ef_where else 'WHERE'} retried_at >= ?",
                ef_args + [now - 3600],
            ).fetchone()[0]
            h24 = c.execute(
                f"SELECT COUNT(*) FROM chunks {ef_where} {'AND' if ef_where else 'WHERE'} retried_at >= ?",
                ef_args + [now - 86400],
            ).fetchone()[0]
            h7d = c.execute(
                f"SELECT COUNT(*) FROM chunks {ef_where} {'AND' if ef_where else 'WHERE'} retried_at >= ?",
                ef_args + [now - 604800],
            ).fetchone()[0]
            older_ = embed_failed - h7d  # embed_failed bucketed by oldest-first already counted
            aging = {"1h": h1, "1d": h24 - h1, "7d": h7d - h24, "older": older_}
        return {"total": total, "ok": ok, "empty": empty, "noise": noise,
                "embed_failed": embed_failed, "deprecated": deprecated,
                "abandoned": abandoned, "embed_failed_aging": aging}

    def preview_source(self, source: str,
                       kb_id: str | None = None,
                       limit_chars: int = 50000) -> dict[str, Any]:
        """把某个来源的分块按原始顺序拼回全文，用于前端「预览」。

        只取最新版（``deprecated=0`` 且最大 version）的 chunk，按 id 升序拼接
        （id 即入库顺序，等于原文顺序）。超出 ``limit_chars`` 截断并标记
        ``truncated=True``，避免把几十万一整本书弹进前端。

        不暴露 embedding / tokens 等内部字段；不是 knowledge_store_type 签名上的
        必选方法（Milvus stub 暂不实现）。
        """
        with _lock, sqlite3.connect(self.db) as c:
            # 取最新 version
            row = c.execute(
                "SELECT MAX(version) FROM chunks WHERE source=? AND kb_id IS ? "
                "AND deprecated=0",
                (source, kb_id),
            ).fetchone()
            if not row or row[0] is None:
                return {"found": False, "text": "", "truncated": False,
                        "chars": 0, "chunks": 0}
            ver = int(row[0])
            rows = c.execute(
                "SELECT text FROM chunks WHERE source=? AND kb_id IS ? "
                "AND version=? AND deprecated=0 ORDER BY id ASC",
                (source, kb_id, ver),
            ).fetchall()
        parts: list[str] = []
        total = 0
        truncated = False
        for (txt,) in rows:
            t = txt or ""
            if total + len(t) > limit_chars:
                remain = max(0, limit_chars - total)
                if remain > 0:
                    parts.append(t[:remain])
                    total += remain
                truncated = True
                break
            parts.append(t)
            total += len(t)
        # 分块之间用换行接回（chunk 切分时已保留段落间的换行，这里补一个保证
        # 相邻 chunk 的末/首不会粘成一个词）
        text = "\n".join(parts)
        return {"found": True, "text": text, "truncated": truncated,
                "chars": total, "chunks": len(rows)}

    def list_embed_failed(self, kb_id: str | None = None,
                          limit: int = 50) -> list[dict[str, Any]]:
        """返回 embed_failed chunk 的排查信息（含 source / text 片段 / failed_count / retried_at）。"""
        ten = self._resolve_tenant(None)
        with _lock, sqlite3.connect(self.db) as c:
            where = "WHERE status=? AND deprecated=0"
            params: list = [self.STATUS_EMBED_FAILED]
            if kb_id:
                where += " AND kb_id IS ?"
                params.append(kb_id)
            if ten:
                where += " AND tenant IS ?"
                params.append(ten)
            rows = c.execute(
                f"SELECT id, source, text, failed_count, retried_at "
                f"FROM chunks {where} ORDER BY retried_at ASC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [
            {"id": r[0], "source": r[1], "text": (r[2] or "")[:200],
             "failed_count": r[3], "retried_at": r[4]}
            for r in rows
        ]

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_chunk(text: str) -> tuple[str, str | None]:
        if not text:
            return KnowledgeStore.STATUS_EMPTY, "zero_length"
        # noise heuristic: <10 non-whitespace chars, or >80% punctuation
        if len(text) < 10:
            return KnowledgeStore.STATUS_NOISE, "too_short"
        punct_ratio = sum(1 for ch in text if ch in "，。！？；：、…,.!?;:`~@#$%^&*()[]{}|/\\\"'\n\t ") / max(len(text), 1)
        if punct_ratio > 0.8:
            return KnowledgeStore.STATUS_NOISE, f"punct_ratio={punct_ratio:.2f}"
        return KnowledgeStore.STATUS_OK, None

    def _next_version(self, source: str, tenant: str | None,
                      kb_id: str | None = None) -> int:
        with _lock, sqlite3.connect(self.db) as c:
            ten = self._resolve_tenant(tenant)
            row = c.execute(
                "SELECT COALESCE(MAX(version), 0) FROM chunks "
                "WHERE source=? AND tenant IS ? AND kb_id IS ?",
                (source, ten or None, kb_id),
            ).fetchone()
            return int(row[0]) + 1

    def _has_chunks(self, source: str, tenant: str | None,
                    kb_id: str | None = None) -> bool:
        with _lock, sqlite3.connect(self.db) as c:
            ten = self._resolve_tenant(tenant)
            row = c.execute(
                "SELECT 1 FROM chunks WHERE source=? AND tenant IS ? AND kb_id IS ? LIMIT 1",
                (source, ten or None, kb_id),
            ).fetchone()
            return row is not None

    def _deprecate_source(self, source: str, tenant: str | None,
                          kb_id: str | None = None) -> None:
        ten = self._resolve_tenant(tenant)
        with _lock, sqlite3.connect(self.db) as c:
            c.execute(
                "UPDATE chunks SET deprecated=1 WHERE source=? AND tenant IS ? "
                "AND kb_id IS ? AND deprecated=0",
                (source, ten or None, kb_id),
            )

    def search(self, query: str, top_k: int = 4, tenant: str | None = None,
               kb_id: str | None = None,
               include_stale_versions: bool = False) -> list[dict[str, Any]]:
        """混合检索（BM25 + 向量 → RRF 融合 → 可选重排）。

        ``kb_id``：指定则**只在该知识库内**召回（知识库详情页的「检索预览」走这条）；
        不指定则跨库全局检索（agent 的 ``knowledge_search`` 工具保持原行为）。

        ``include_stale_versions``（D59）：默认 False → 仅命当前 model_version 档
        的 chunk（``embed_model_version IS NULL`` 的旧数据仍保留可见，避免一升级就
        整库空）；True 时回退到不过滤旧版本（兼容期兜底）。
        """
        # D59 version gate：仅过滤"版本号非空且不等于当前活跃版本"的行；
        # NULL chunk = 老库默认兼容保留。
        emv_where = ("" if include_stale_versions
                     else " AND (embed_model_version IS NULL"
                          " OR embed_model_version=? )")
        emv_arg: list = ([] if include_stale_versions
                         else [_resolve_emv()])
        q_vec = _embed(query)
        ten = self._resolve_tenant(tenant)
        with _lock, sqlite3.connect(self.db) as c:
            if kb_id:
                rows = c.execute(
                    "SELECT id, source, text, vec FROM chunks "
                    "WHERE kb_id IS ? AND deprecated=0 AND status IN ('ok','embed_failed')"
                    + emv_where,
                    [kb_id, *emv_arg]).fetchall()
            elif ten:
                rows = c.execute(
                    "SELECT id, source, text, vec FROM chunks "
                    "WHERE tenant IS ? AND deprecated=0 AND status IN ('ok','embed_failed')"
                    + emv_where,
                    [ten, *emv_arg]).fetchall()
            else:
                # 全局模式：不过滤（兼容既有无 tenant 数据；D59 仍然隔离 stale version）
                rows = c.execute(
                    "SELECT id, source, text, vec FROM chunks "
                    "WHERE deprecated=0 AND status IN ('ok','embed_failed')"
                    + emv_where,
                    emv_arg,
                ).fetchall()
        if not rows:
            return []
        text_of = {r[0]: r[2] for r in rows}
        source_of = {r[0]: r[1] for r in rows}
        vec_of = {r[0]: r[3] for r in rows}

        rankings: list[list[int]] = []
        # Channel 1 — keyword BM25（CJK 感知），无需嵌入，离线可用
        bm = _bm25_scores(query, text_of)
        if bm:
            rankings.append(sorted(bm, key=bm.get, reverse=True))
        # Channel 2 — vector cosine（嵌入可用时）
        if q_vec is not None:
            sims = []
            for _id, text in text_of.items():
                vec_str = vec_of.get(_id)
                if not vec_str:
                    continue
                v = json.loads(vec_str)
                sims.append((sum(a * b for a, b in zip(q_vec, v)), _id))
            if sims:
                sims.sort(key=lambda x: x[0], reverse=True)
                rankings.append([doc_id for _, doc_id in sims])
        if not rankings:
            return []

        fused_scores = _rrf_scores(rankings)
        ordered = sorted(fused_scores, key=fused_scores.get, reverse=True)
        ranked = [
            {"id": doc_id, "source": source_of[doc_id], "text": text_of[doc_id],
             "score": round(fused_scores[doc_id], 4)}
            for doc_id in ordered
        ]
        if get_settings().rerank_enabled and ranked:
            # 先取更大候选集再做重排，避免重排无素材
            from ..rag.reranker import rerank

            candidates = ranked[: max(top_k * 3, len(ranked))]
            return rerank(query, candidates, top_k)
        return ranked[:top_k]

    # ---- 管理面接口（管理面板/前端知识库用）----
    def list_sources(self, kb_id: str | None = None) -> list[dict[str, Any]]:
        """按 source 聚合，返回 {source, chunks}，chunks 多者在前。

        ``kb_id`` 指定时只统计该库（知识库详情页用）；不指定则跨库汇总。
        """
        with _lock, sqlite3.connect(self.db) as c:
            if kb_id:
                rows = c.execute(
                    "SELECT source, COUNT(*) FROM chunks WHERE kb_id IS ? "
                    "GROUP BY source ORDER BY COUNT(*) DESC",
                    (kb_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT source, COUNT(*) FROM chunks GROUP BY source "
                    "ORDER BY COUNT(*) DESC"
                ).fetchall()
        return [{"source": r[0], "chunks": r[1]} for r in rows]

    def delete_source(self, source: str, kb_id: str | None = None) -> int:
        """删除某来源的全部分块。``kb_id`` 限定范围，避免误删其他库的同名来源。"""
        with _lock, sqlite3.connect(self.db) as c:
            if kb_id:
                cur = c.execute(
                    "DELETE FROM chunks WHERE source=? AND kb_id IS ?", (source, kb_id)
                )
            else:
                cur = c.execute("DELETE FROM chunks WHERE source=?", (source,))
            return int(cur.rowcount)

    def total_chunks(self, kb_id: str | None = None) -> int:
        with _lock, sqlite3.connect(self.db) as c:
            if kb_id:
                return int(c.execute(
                    "SELECT COUNT(*) FROM chunks WHERE kb_id IS ? AND deprecated=0",
                    (kb_id,)).fetchone()[0])
            return int(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def adopt_orphan_chunks(self, kb_id: str) -> dict[str, int]:
        """把历史无归属分块（``kb_id IS NULL``）划归指定知识库。

        返回 ``{source: 分块数}``，供目录模块登记成文档。幂等：库里已无无归属
        分块时再调用返回 ``{}``（不会重复接管）。
        """
        with _lock, sqlite3.connect(self.db) as c:
            rows = c.execute(
                "SELECT source, COUNT(*) FROM chunks WHERE kb_id IS NULL "
                "AND deprecated=0 GROUP BY source"
            ).fetchall()
            c.execute("UPDATE chunks SET kb_id=? WHERE kb_id IS NULL", (kb_id,))
        return {r[0]: int(r[1]) for r in rows}

    def delete_kb_chunks(self, kb_id: str) -> int:
        """删库时级联清空该库全部分块，返回删除行数。"""
        with _lock, sqlite3.connect(self.db) as c:
            cur = c.execute("DELETE FROM chunks WHERE kb_id IS ?", (kb_id,))
            return int(cur.rowcount)


_store: knowledge_store_type | None = None


def _milvus_client():
    """Return a Milvus client when configured & reachable, else None.

    统一走 ``app.infrastructure.vectorstore.milvus.get_client``：支持两种接法——
    * ``MILVUS_URI``：真实服务端 ``http://host:port``，或 **Milvus Lite** 本地文件
      （如 ``./data/milvus_lite.db``，无需起服务，便于本机/CI 真实验证）；
    * ``MILVUS_HOST`` + ``MILVUS_PORT``（向后兼容）。
    """
    from ...infrastructure.vectorstore.milvus import get_client

    return get_client()


class MilvusKnowledgeStore:
    """Milvus-backed knowledge store (spec §20 RAG layer).

    Requires sentence-transformers embeddings (vector dimension comes from the
    configured ``embed_model``). When embedding is unavailable we cannot use
    Milvus and callers fall back to the SQLite store.

    多租户（RAG/02）：与 SQLite 后端**语义对齐**——``tenant`` 显式传入优先，
    否则回退 ``default_tenant``；都为空则"全局模式"（不过滤，兼容无租户的历史数据）。
    此前 Milvus 后端的 ``add/search`` 根本没有 ``tenant`` 参数，工具层
    ``knowledge_search`` 传 ``tenant=`` 时会直接 TypeError（真实 Milvus 上必现），
    且没有租户字段 → 既不可用、也无隔离。
    """

    TENANT_FIELD = "tenant"

    def __init__(self, client, collection: str) -> None:
        self.client = client
        self.collection = collection
        probe = _embed("dimension probe")
        if probe is None:
            raise RuntimeError("Milvus 后端需要 sentence-transformers 嵌入模型")
        self.dim = len(probe)
        if not client.has_collection(collection):
            from pymilvus import DataType
            schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
            schema.add_field("id", DataType.INT64, is_primary=True)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)
            schema.add_field("source", DataType.VARCHAR, max_length=512)
            schema.add_field("text", DataType.VARCHAR, max_length=65535)
            schema.add_field(self.TENANT_FIELD, DataType.VARCHAR, max_length=128)
            schema.add_field(self.KB_FIELD, DataType.VARCHAR, max_length=128)
            index_params = client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="AUTOINDEX",
                                   metric_type="COSINE")
            # Strong 一致性：ingest 后立即可检索（默认 Bounded 有可见延迟）
            client.create_collection(collection, schema=schema, index_params=index_params,
                                     consistency_level="Strong")
        self._has_tenant = self.TENANT_FIELD in self._field_names()
        # 老 collection 可能没有 kb_id 字段；缺字段时按库过滤无法表达，
        # 降级为「全局检索」而不是报错（可用性优先，前端会看到跨库结果）。
        self._has_kb = self.KB_FIELD in self._field_names()

    def _field_names(self) -> set[str]:
        """已有 collection 的字段集（老库可能没有 tenant 字段，需兼容）。"""
        try:
            desc = self.client.describe_collection(self.collection)
            return {f.get("name") for f in (desc.get("fields") or [])}
        except Exception:
            return set()

    def _tenant_filter(self, ten: str) -> str:
        """构造 Milvus 过滤表达式（转义双引号，防表达式注入）。"""
        safe = ten.replace("\\", "\\\\").replace('"', '\\"')
        return f'{self.TENANT_FIELD} == "{safe}"'

    def add(self, text: str, source: str, tenant: str | None = None) -> int:
        vec = _embed(text)
        if vec is None:
            return 0
        row: dict[str, Any] = {"vector": vec, "source": str(source)[:500],
                               "text": text[:65000]}
        if self._has_tenant:
            row[self.TENANT_FIELD] = _resolve_tenant(tenant)[:128]
        res = self.client.insert(self.collection, [row])
        return int(res.get("insert_count", 0) or 0)

    def search(self, query: str, top_k: int = 4,
               tenant: str | None = None) -> list[dict[str, Any]]:
        q_vec = _embed(query)
        if q_vec is None:
            return []
        kwargs: dict[str, Any] = {"output_fields": ["source", "text"]}
        ten = _resolve_tenant(tenant)
        if self._has_tenant and ten:
            kwargs["filter"] = self._tenant_filter(ten)
        results = self.client.search(
            self.collection, data=[q_vec], limit=top_k, **kwargs,
        )
        hits = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            hits.append({
                "id": hit.get("id") if isinstance(hit, dict) else hit.id,
                "source": entity.get("source", ""),
                "text": entity.get("text", ""),
                # COSINE 相似度：越大越相关
                "score": round(float(hit.get("distance", 0.0)) if isinstance(hit, dict)
                               else float(hit.distance), 4),
            })
        return hits

    # ---- 管理面接口 ----
    def _kb_filter(self, kb_id: str) -> str:
        safe = str(kb_id).replace("\\", "\\\\").replace('"', '\\"')
        return f'{self.KB_FIELD} == "{safe}"'

    def list_sources(self, kb_id: str | None = None) -> list[dict[str, Any]]:
        flt = self._kb_filter(kb_id) if (kb_id and self._has_kb) else ""
        try:
            res = self.client.query(
                self.collection, filter=flt, output_fields=["source"], limit=16384,
            )
        except Exception:
            return []
        from collections import Counter

        cnt = Counter((r.get("source") or "") for r in (res or []))
        return [{"source": s, "chunks": n} for s, n in cnt.most_common()]

    def delete_source(self, source: str, kb_id: str | None = None) -> int:
        safe = source.replace("\\", "\\\\").replace('"', '\\"')
        flt = f'source == "{safe}"'
        if kb_id and self._has_kb:
            flt = f"{flt} and {self._kb_filter(kb_id)}"
        try:
            res = self.client.delete(self.collection, flt)
        except Exception:
            return 0
        if isinstance(res, dict):
            return int(res.get("delete_count", 0) or 0)
        return 0

    def total_chunks(self, kb_id: str | None = None) -> int:
        flt = self._kb_filter(kb_id) if (kb_id and self._has_kb) else ""
        try:
            res = self.client.query(
                self.collection, filter=flt, output_fields=["id"], limit=16384,
            )
            return len(res or [])
        except Exception:
            return 0

    # Milvus 后端没有「无归属分块」的概念（字段不存在即无归属），接管无操作；
    # 这些方法保证管理面在 Milvus 模式下也能调用而不抛 AttributeError。
    def adopt_orphan_chunks(self, kb_id: str) -> dict[str, int]:
        return {}

    def kb_chunk_count(self, kb_id: str) -> int:
        return self.total_chunks(kb_id)

    def delete_kb_chunks(self, kb_id: str) -> int:
        if not self._has_kb:
            return 0
        try:
            res = self.client.delete(self.collection, self._kb_filter(kb_id))
        except Exception:
            return 0
        return int(res.get("delete_count", 0) or 0) if isinstance(res, dict) else 0

    # Milvus 后端没有「分块状态」概念（status / failed_count / retried_at 不在 schema 里）；
    # 这些方法保证管理面在 Milvus 模式下也能调用而不抛 AttributeError。
    def retry_embed(self, kb_id: str | None = None, limit: int | None = None) -> dict[str, int]:
        return {"retried": 0, "fixed": 0, "still_failed": 0, "abandoned_now": 0}

    def abandon_expired(self, ttl_s: int | None = None, kb_id: str | None = None) -> int:
        return 0

    def list_embed_failed(self, kb_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def preview_source(self, source: str,
                       kb_id: str | None = None,
                       limit_chars: int = 50000) -> dict[str, Any]:
        """Milvus 后端暂不支持分块级重建全文（chunk 在 Milvus 里是无结构向量）。"""
        return {"found": False, "text": "", "truncated": False,
                "chars": 0, "chunks": 0, "unsupported": True}

    # D59 嵌入版本迁移 Milvus 后端兜底：collection 维度硬约束（创建时维度定死）；
    # Milvus 路径不直接支持「同库 / 同 collection 改维度」——真正迁移要上层建
    # 新 collection + 回灌；stub 只保 duck-typing 不抛 AttributeError。
    def get_current_embed_version(self) -> str:
        return _resolve_emv()

    def version_stats(self, kb_id: str | None = None) -> dict[str, Any]:
        return {"current": _resolve_emv(),
                "distribution": {}, "stale_count": 0, "total": 0}

    def reembed_chunk(self, chunk_id: int) -> dict[str, int | str]:
        return {"ok": 0, "reason": "milvus_not_implemented"}

    def reembed_batch(self, kb_id: str | None = None,
                      target_version: str | None = None,
                      limit: int | None = None) -> dict[str, int]:
        return {"migrated": 0, "failed": 0, "skipped": 0,
                "reason": "milvus_collection_dimension_fixed"}


def get_store() -> knowledge_store_type:
    global _store
    if _store is None:
        client = _milvus_client()
        if client is not None:
            try:
                _store = MilvusKnowledgeStore(client, get_settings().milvus_collection)
                return _store
            except Exception:
                pass  # Milvus 不可用 → 回退 SQLite（保持服务可用）
        _store = KnowledgeStore()
    return _store


# --------------------------------------------------------------------------- #
# 管理面辅助函数（前端知识库面板调用，与检索/低置信判定解耦）
# --------------------------------------------------------------------------- #
def kb_status() -> dict[str, Any]:
    """知识库整体状态：后端类型、是否启用、来源数、分块总数。"""
    s = get_settings()
    store = get_store()
    backend = "milvus" if isinstance(store, MilvusKnowledgeStore) else "sqlite"
    try:
        sources = store.list_sources()
        total = sum(x["chunks"] for x in sources)
    except Exception:
        sources, total = [], 0
    return {
        "enabled": s.knowledge_enabled,
        "backend": backend,
        "total_chunks": total,
        "sources": len(sources),
    }


def list_documents(kb_id: str | None = None) -> list[dict[str, Any]]:
    return get_store().list_sources(kb_id)


def delete_document(source: str, kb_id: str | None = None) -> int:
    return get_store().delete_source(source, kb_id)


def search_documents(query: str, top_k: int = 5,
                     kb_id: str | None = None) -> list[dict[str, Any]]:
    """管理预览用检索：直接复用 store.search，不套低置信清空逻辑。

    ``kb_id`` 指定时只在该知识库内召回。

    空库短路：库里没有任何分块时立刻返回 []，**不触发嵌入模型加载**。
    首次加载嵌入模型要等满 ``embed_load_timeout_s``（实测 ~25s，离线缓存缺失时
    会去联网下载而挂起），若空库也照走一遍，用户第一次点「检索预览」就会干等
    数十秒。空库无内容可检，没有理由付这个代价。
    """
    store = get_store()
    try:
        if store.total_chunks(kb_id) == 0:
            return []
    except Exception:
        pass  # 后端缺该方法时退回正常检索路径，不影响可用性
    try:
        return store.search(query, top_k, kb_id=kb_id)
    except TypeError:
        # 老后端签名不支持 kb_id：退回全局检索（可用性优先）
        return store.search(query, top_k)


def _low_confidence_note(level: str, hits: int) -> str:
    """低置信时给模型和人的一句实话。

    **只报"命中了几段"，不报内容**——内容一旦进来就绕开了"不下发"的保证。
    """
    if level == "none":
        return (f"知识库未命中任何相关段落（命中 {hits} 段）。"
                "本次没有知识依据，不得据此推断业务口径——需要先补充知识库或改问法。")
    return (f"知识库命中了 {hits} 段字面相近但相关性不足的内容，已不予采用。"
            "本次没有知识依据，不得据此推断业务口径——需要先补充知识库或改问法。")


def run(params: dict[str, Any]) -> dict[str, Any]:
    query = (params.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "缺少 query 参数", "chunks": []}
    top_k = int(params.get("top_k", 4))
    tenant = params.get("tenant")
    try:
        from ..rag.multihop import MultiHopRetriever

        mh = MultiHopRetriever(store=get_store(), tenant=tenant)
        result = mh.retrieve(query, top_k)
        chunks = result.chunks
    except Exception as exc:
        # fail-closed：出错就报错，绝不伪装成一次"成功的检索"（那会让模型以为查过了）
        return {"ok": False, "error": str(exc), "chunks": []}

    from ..rag.confidence import confidence_of

    conf = confidence_of(query, chunks)
    payload = {"level": conf.level, "score": conf.score, "basis": conf.basis}
    if conf.level == "high":
        return {"ok": True, "chunks": chunks, "confidence": payload,
                "low_confidence": False, "note": ""}

    # 低置信：**清空 chunks**。标记而不清空等于让模型照样读到，只能靠 prompt 说"别引用"——
    # 提示词纪律冒充保证。清空才是结构性保证（见 spec §2.2）。
    from ...infrastructure.observability.metrics import metrics

    metrics.inc("rag_low_confidence_total")   # 只计数，不记录查询内容
    return {"ok": True, "chunks": [], "confidence": payload,
            "low_confidence": True, "note": _low_confidence_note(conf.level, len(chunks))}
