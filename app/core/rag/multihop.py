"""E9/01 多跳检索 + 子问题拆分。

把复合 query（含多个子问题的问句）拆成若干子 query，各自独立检索后合并去重，
最终以全查询 rerank，产出 ≤ top_k chunks 喂给 D53 置信门。

与既有管线的关系：
- D53 置信门（confidence_of）**不动**——它只看最终 top_k 首条
- run() 的对外 JSON 契约**不变**（仍返回 {ok, chunks, confidence, low_confidence, note}）
- 单跳路径与原 store.search 等价（不做任何截断/重排/去重）

Spec: ``docs/specs/E9/01-multi-hop.md``
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from ...config import get_settings
from . import reranker

# 拆分符：枚举符 + 并列/选择连词。
# 中文语法里连词左右都是紧接字符（无空白），所以用裸捕获而不是 \s+ 包裹。
_SPLIT_RE = re.compile(
    r"\s*[,，、]\s*"                                    # 枚举符
    r"|(?:以及|或者|和|与|及|跟|或)"                      # 并列/选择连词（无空白要求）
    , re.IGNORECASE,
)
# 保底：整段 strip 后是否仅由标点/空白组成
_TRIVIAL_RE = re.compile(r"^[\s\p{P}]*$".replace(r"\p{P}", r"!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"))


class _Store(Protocol):
    """duck-typing：仅要求 store 具备 .search(...) 方法；Milvus 后端同构适配。"""

    def search(self, query: str, top_k: int = 4, *,
               tenant: str | None = None,
               kb_id: str | None = None,
               include_stale_versions: bool = False) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# QuerySplitter — 规则拆分
# --------------------------------------------------------------------------- #
@dataclass
class QuerySplitter:
    """按枚举符 + 并列/选择连词切分子问题。

    任何情况下都不会抛出异常：异常输入走 fallback ``[query]`` 单跳直通。
    """

    enabled: bool = True
    max_splits: int = 3
    min_query_len: int = 20     # query 总长低于此 → 直接单跳
    min_piece_len: int = 4      # 切分后每段至少这么多字符

    @property
    def _max_splits(self) -> int:
        return max(2, int(self.max_splits) or 3)

    def split(self, query: str) -> list[str]:
        """拆出子 query 列表；不值得拆 → [query] 单跳直通。"""
        if not self.enabled:
            return [query]
        if not query or len(query) < self.min_query_len:
            return [query]
        try:
            raw_pieces: list[str] = _SPLIT_RE.split(query)
        except Exception:
            return [query]

        # 去空白/纯标点段 + 去重 + 保序
        pieces: list[str] = []
        for p in raw_pieces:
            cleaned = p.strip(" ，、和与及以及跟或或者!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ \t\n")
            if len(cleaned) < self.min_piece_len:
                continue
            # 去掉仍只剩纯标点的
            if _TRIVIAL_RE.match(cleaned):
                continue
            pieces.append(cleaned)

        # 去重（保序）
        seen: set[str] = set()
        deduped: list[str] = []
        for p in pieces:
            if p not in seen:
                seen.add(p)
                deduped.append(p)

        # 不够 2 段就没必要多跳
        if len(deduped) <= 1:
            return [query]
        return deduped[: self._max_splits]


# --------------------------------------------------------------------------- #
# MultiHopRetriever
# --------------------------------------------------------------------------- #
@dataclass
class MultiHopResult:
    chunks: list[dict[str, Any]]          # ≤ top_k（已 rerank 全查询）
    sub_queries: list[str]                # 实际展开的子 query
    splits: int                           # len(sub_queries)
    single_hop: bool                      # 是否单跳直通
    # D61 seed_queries：改写层注入的多个 seed（主改写 + 同义 phrasing）；
    # 单 seed（未改写）时为 [query]，便于上游 metrics / trace。
    seed_queries: list[str] = field(default_factory=list)


# 模块级 metrics 计数器（挂 Metrics 类同名语义；简单起见走模块级 dict + lock）
_lock = threading.Lock()
_METRICS: dict[str, float] = {
    "rag_multi_hop_splits_total": 0.0,    # 多跳查询次数（实际展开子 query 数 ≥ 2）
}


def multihop_metrics() -> dict[str, float]:
    """返回多跳指标的当前快照（用于测试与 /metrics 暴露）。"""
    with _lock:
        return dict(_METRICS)


def _emit_split(n_splits: int) -> None:
    with _lock:
        _METRICS["rag_multi_hop_splits_total"] += 1.0


class MultiHopRetriever:
    """复合 query 的多跳召回。

    ``store`` 可以是 KnowledgeStore（SQLite）或 MilvusKnowledgeStore——只要
    ``.search(query, top_k, tenant=..., kb_id=...)`` 方法走 duck-typing。

    失败-关闭：``retrieve`` 内任何未捕获异常 → 退回 ``store.search`` 单跳，
    绝不上游 RAG path 受影响。
    """

    def __init__(self, store: _Store,
                 tenant: str | None = None,
                 *,
                 splitter: QuerySplitter | None = None) -> None:
        self._store = store
        self._tenant = tenant
        if splitter is not None:
            self._splitter = splitter
        else:
            st = get_settings()
            self._splitter = QuerySplitter(
                enabled=getattr(st, "rag_multi_hop_enabled", True),
                max_splits=getattr(st, "rag_multi_hop_max_splits", 3) or 3,
                min_query_len=getattr(st, "rag_multi_hop_min_query_len", 20) or 20,
                min_piece_len=getattr(st, "rag_multi_hop_min_piece_len", 4) or 4,
            )

    # -- public --------------------------------------------------------- #
    def retrieve(self, query: str, top_k: int = 4, *,
                 kb_id: str | None = None,
                 include_stale_versions: bool = False) -> MultiHopResult:
        if not query or top_k <= 0:
            return MultiHopResult(chunks=[], sub_queries=[query], splits=1,
                                  single_hop=True, seed_queries=[query])

        splitter = self._splitter
        # 强制禁用检测：settings 关门 → 仍用 splitter.enabled=False 路径
        enabled = getattr(get_settings(), "rag_multi_hop_enabled", True)
        if not enabled:
            splitter = QuerySplitter(enabled=False)
        sub_queries = splitter.split(query)
        single_hop = len(sub_queries) == 1

        if single_hop:
            # 单跳路径：异常自然传播给调用方，保留 fail-closed 语义
            # （D53 test_tool_search_failure_stays_fail_closed 要求如此）
            chunks = self._store.search(
                query, top_k, tenant=self._tenant, kb_id=kb_id,
                include_stale_versions=include_stale_versions)
            return MultiHopResult(chunks=chunks, sub_queries=sub_queries,
                                  splits=1, single_hop=True)

        return self._fanout_merge(query, sub_queries, top_k, kb_id,
                                  include_stale_versions)

    # -- D61: retrieve_many (rewrite seeds fan-out) -------------------------- #
    def retrieve_many(self, queries: list[str], top_k: int, *,
                      kb_id: str | None = None,
                      include_stale_versions: bool = False) -> MultiHopResult:
        """多个 query 分别 fan-out，合并去重，用首 query rerank 到 top_k。

        用于 D61 改写层：改写产出主改写 + 若干同义 phrasing = 多个 seed，每个 seed
        各自走 ``retrieve``（单跳或 multi-hop），合并候选池按 chunk.id 去重后用
        首 seed rerank。任一 seed 异常 → 跳过（fail-open），其它 seed 仍参与。
        全部 seed 都失败 → 返回空 MultiHopResult（single_hop=False），让 D53
        置信门走到 none = 兜底。
        """
        if not queries or top_k <= 0:
            empty = queries[0] if queries else ""
            return MultiHopResult(chunks=[], sub_queries=[empty], splits=1,
                                  single_hop=False, seed_queries=list(queries))

        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        all_subs: list[str] = []
        any_multi_hop = False
        any_seed_ok = False
        last_exc: BaseException | None = None

        for seed in queries:
            try:
                res = self.retrieve(seed, top_k, kb_id=kb_id,
                                    include_stale_versions=include_stale_versions)
            except Exception as exc:
                last_exc = exc
                continue
            any_seed_ok = True
            if not res.single_hop:
                any_multi_hop = True
            all_subs.extend(res.sub_queries)
            for c in res.chunks:
                cid = c.get("id")
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                candidates.append(c)

        # D53 失败-关闭：所有 seed 检索都抛异常 → 整趟视为失败，不得伪装成"查到了但为空"。
        # (只要有 1 个 seed 正常返回 → 按该 seed 结果判定，另一个 seed 的异常被吞掉 = fail-open per seed)
        if not any_seed_ok and last_exc is not None:
            raise last_exc

        if not candidates:
            return MultiHopResult(chunks=[], sub_queries=all_subs, splits=1,
                                  single_hop=not any_multi_hop,
                                  seed_queries=list(queries))

        # rerank 用**首 seed**（通常 = 改写主结果）
        try:
            ranked = reranker.rerank(queries[0], candidates, top_k)
        except Exception:
            ranked = candidates[:top_k]

        return MultiHopResult(chunks=ranked, sub_queries=all_subs, splits=1,
                              single_hop=not any_multi_hop,
                              seed_queries=list(queries))

    # -- internal ------------------------------------------------------- #
    def _fanout_merge(self, query: str, sub_queries: list[str],
                      top_k: int, kb_id: str | None,
                      include_stale_versions: bool) -> MultiHopResult:
        sub_budget = max(top_k, int(top_k * 1.5) + 1)

        # 1. fan-out：每个子 query 独立 search；任一子 query 异常不影响其它
        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for sq in sub_queries:
            try:
                hits = self._store.search(
                    sq, sub_budget, tenant=self._tenant, kb_id=kb_id,
                    include_stale_versions=include_stale_versions)
            except Exception:
                continue
            for h in hits:
                cid = h.get("id")
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                candidates.append(h)

        if not candidates:
            _emit_split(len(sub_queries))
            return MultiHopResult(chunks=[], sub_queries=sub_queries,
                                  splits=len(sub_queries), single_hop=False)

        # 2. 重新按**全查询** rerank（消除跨子 query rerank_score 不可比问题）
        try:
            ranked = reranker.rerank(query, candidates, top_k)
        except Exception:
            ranked = candidates[:top_k]

        _emit_split(len(sub_queries))
        return MultiHopResult(chunks=ranked, sub_queries=sub_queries,
                              splits=len(sub_queries), single_hop=False)
