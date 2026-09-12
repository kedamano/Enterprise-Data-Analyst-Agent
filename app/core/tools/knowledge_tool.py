"""``knowledge_search`` tool – RAG over the enterprise knowledge base.

When Milvus is configured the vector store is used; otherwise a local SQLite
store with optional sentence-transformers embeddings (falling back to keyword
overlap scoring) keeps the service fully functional offline. This honours the
spec's principle: *knowledge is not data* – it answers metric definitions and
business terminology, never substitutes for actual query results.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ...config import get_settings

_DB_PATH = Path("data/knowledge.db")
_lock = threading.Lock()

# SQLite 与 Milvus 两个后端共用 add/search 鸭子类型接口
knowledge_store_type = Any


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w\u4e00-\u9fff]+", text.lower()) if len(t) > 1]


# \u5d4c\u5165\u6a21\u578b\u8fdb\u7a0b\u7ea7\u7f13\u5b58 + \u5931\u8d25\u5373\u7981\u7528\uff08\u907f\u514d\u6bcf\u6b21\u8c03\u7528\u91cd\u65b0\u52a0\u8f7d ~13s\uff0c\u6216\u8054\u7f51\u6821\u9a8c\u6302\u8d77\uff09
_embed_model = None
_embed_error: str | None = None


def _get_embed_model():
    """\u61d2\u52a0\u8f7d\u5d4c\u5165\u6a21\u578b\uff0c\u5e26\u5899\u949f\u9884\u7b97\u2014\u2014\u7edd\u4e0d\u8ba9\u5165\u5e93/\u68c0\u7d22\u88ab\u6a21\u578b\u52a0\u8f7d\u65e0\u9650\u963b\u585e\u3002

    \u540e\u53f0\u7ebf\u7a0b\u52a0\u8f7d\uff08\u5148\u79bb\u7ebf\u7f13\u5b58\uff0c\u7f13\u5b58\u7f3a\u5931\u518d\u5728\u7ebf\uff09\uff0c\u4e3b\u7ebf\u7a0b ``join(timeout)``\uff1b
    \u8d85\u65f6\u6216\u5931\u8d25\u5219\u6c38\u4e45\u7981\u7528\u5d4c\u5165\uff0c\u6df7\u5408\u68c0\u7d22\u7684 BM25 \u901a\u9053\u53ef\u72ec\u7acb\u5de5\u4f5c\uff08\u964d\u7ea7\u800c\u975e\u6302\u8d77\uff09\u3002
    """
    global _embed_model, _embed_error
    if _embed_model is not None:
        return _embed_model
    if _embed_error is not None:
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
    t.start()
    t.join(get_settings().embed_load_timeout_s)
    if "model" in holder:
        _embed_model = holder["model"]
        return _embed_model
    if "error" in holder:
        _embed_error = f"\u5d4c\u5165\u6a21\u578b\u52a0\u8f7d\u5931\u8d25\uff08\u5df2\u964d\u7ea7\u4e3a\u7eaf BM25 \u68c0\u7d22\uff09: {holder['error']}"
    else:
        _embed_error = f"\u5d4c\u5165\u6a21\u578b\u52a0\u8f7d\u8d85\u65f6\uff08>{get_settings().embed_load_timeout_s}s\uff0c\u5df2\u964d\u7ea7\u4e3a\u7eaf BM25 \u68c0\u7d22\uff09"
    return None


def _embed(text: str) -> list[float] | None:
    model = _get_embed_model()
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


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


class KnowledgeStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db = str(db_path or _DB_PATH)
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        with _lock, sqlite3.connect(self.db) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY, source TEXT, text TEXT,
                    tokens TEXT, vec TEXT)"""
            )
            # 多租户：懒迁移加 tenant 列；既有库不加租户值时保持全局可见
            cols = {r[1] for r in c.execute("PRAGMA table_info(chunks)").fetchall()}
            if "tenant" not in cols:
                c.execute("ALTER TABLE chunks ADD COLUMN tenant TEXT")

    @staticmethod
    def _resolve_tenant(tenant: str | None) -> str:
        return _resolve_tenant(tenant)

    def add(self, text: str, source: str, tenant: str | None = None) -> int:
        tokens = " ".join(_tokenize(text))
        vec = _embed(text)
        ten = self._resolve_tenant(tenant)
        with _lock, sqlite3.connect(self.db) as c:
            cur = c.execute(
                "INSERT INTO chunks(source, text, tokens, vec, tenant) VALUES(?,?,?,?,?)",
                (source, text, tokens, json.dumps(vec) if vec else None, ten or None),
            )
            return int(cur.lastrowid)

    def search(self, query: str, top_k: int = 4, tenant: str | None = None) -> list[dict[str, Any]]:
        q_vec = _embed(query)
        ten = self._resolve_tenant(tenant)
        with _lock, sqlite3.connect(self.db) as c:
            if ten:
                rows = c.execute(
                    "SELECT id, source, text, vec FROM chunks WHERE tenant=?",
                    (ten,)).fetchall()
            else:
                # 全局模式：不过滤（兼容既有无 tenant 数据）
                rows = c.execute("SELECT id, source, text, vec FROM chunks").fetchall()
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
            index_params = client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="AUTOINDEX",
                                   metric_type="COSINE")
            # Strong 一致性：ingest 后立即可检索（默认 Bounded 有可见延迟）
            client.create_collection(collection, schema=schema, index_params=index_params,
                                     consistency_level="Strong")
        self._has_tenant = self.TENANT_FIELD in self._field_names()

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


def run(params: dict[str, Any]) -> dict[str, Any]:
    query = (params.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "缺少 query 参数", "chunks": []}
    top_k = int(params.get("top_k", 4))
    tenant = params.get("tenant")
    try:
        chunks = get_store().search(query, top_k, tenant=tenant)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "chunks": []}
    return {"ok": True, "chunks": chunks}
