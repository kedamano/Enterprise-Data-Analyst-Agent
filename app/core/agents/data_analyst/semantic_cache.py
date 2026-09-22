"""Semantic cache — cosine-similarity hit on near-duplicate queries.

当本次 query 与历史某个 query embedding cosine > threshold 时，直接复用历史结果、不调 LLM。
与 exact-match ``response_cache`` 并列：exact 未命中才走语义（两条独立路径，分别按各自规则准入/淘汰）。

嵌入方案：hashing trick（与 ``scripts/benchmark_rag.py`` 同构）——把 query 词袋映射到
dim 维实数向量（进程内确定性，无需模型）。cosine 用 numpy 算。

存储：SQLite（``data/cache/semantic_cache.db``），按 session_id 隔离（跨 session 有租户泄漏风险）。
写入 best-effort：任何异常静默打日志 + return，**绝不打断主流水线**。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Optional

logger = logging.getLogger("da.semantic_cache")

# 嵌入维度默认 64（可通过 config 覆盖）
_DEFAULT_DIM = 64

# 参数化命中风险：query 中含数字/期间 → 同模板仅参数不同时 cosine 仍会很高，
# 命中则把 A 的结论给 B（例如"2024年总营收"命中"2025年总营收"）。
# 这类 query 跳过语义缓存，只走精确匹配 response_cache。
# 注意：同时被 graph.py 的 is_followup 门覆盖（增量 query 完全不走缓存）。
_PARAMETRIC_RE = re.compile(
    r"\d|"
    r"[一二三四五六七八九十]+[个只条件条次]|"
    r"[0-9]{4}\s*年|"
    r"[上下本]月|[上下本]周|"
    r"昨天|今天|明天|去年|今年|明年|"
    r"前[天周月年]|后[天周月年]"
)


def has_parametric_values(query: str) -> bool:
    """query 含数字/期间参数 → 跳过语义缓存。

    理由：hashing trick 对数字/期间不敏感（「2024」和「2025」逐字 cosine 仅差 1-2 维），
    同模板 query 极易跨参数命中。例如"2024年总营收"和"2025年总营收"cosine > 0.95，
    命中意味着把去年的结论直接给今年 —— 业务错误且不可接受。
    """
    return bool(_PARAMETRIC_RE.search(query or ""))
try:
    import numpy as np

    def _cosine(a: list[float], b: list[float]) -> float:
        A = np.array(a)
        B = np.array(b)
        dot = float(A @ B)
        na = float(np.linalg.norm(A))
        nb = float(np.linalg.norm(B))
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0
except ImportError:
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def _tokenize(text: str) -> list[str]:
    """CJK 字符级 tokenization：中文逐字、英文按词（len>1）。

    为什么逐字而非整句：hashing trick 把每个 token 独立映射到维度。CJK 无空格分隔，
    若整句作 token 则"营收"和"营收是多少"落在不同维度 → cosine=0；
    逐字后"营""收"等共享维度 → 近义 query 才能拿到高 cosine。
    """
    tokens: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff]|[\w]+", (text or "").lower()):
        token = m.group()
        if re.match(r"[\u4e00-\u9fff]", token):
            tokens.append(token)          # 中文逐字
        elif len(token) > 1:
            tokens.append(token)          # 英文/数字词（去单字符噪声）
    return tokens


def embed_query(query: str, dim: int = _DEFAULT_DIM) -> list[float]:
    """Hashing trick：把 query 词袋映射到 dim 维实数向量。

    进程内确定性（无需模型、无需离线/在线一致性）。每个 token 哈希到维度索引，
    sign=±1 让正负都有、避免全正偏差；最后 L2 归一。
    """
    vec = [0.0] * dim
    for token in _tokenize(query or ""):
        h = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 64) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ── SQLite 连接管理 ──────────────────────────────────────────────────────
_lock = threading.Lock()

def _db_path() -> Path:
    env_path = os.environ.get("SEMANTIC_CACHE_DB_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    base = Path("data") / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base / "semantic_cache.db"


def _conn() -> sqlite3.Connection:
    """进程级打开一次的 SQLite 连接（check_same_thread=False 让多线程安全）。"""
    if not hasattr(_conn, "_c") or _conn._c is None:
        c = sqlite3.connect(str(_db_path()), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")  # WAL 让读写并发不锁死
        c.execute("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                query TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                query_hash TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_semantic_query_hash ON semantic_cache(query_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_semantic_session ON semantic_cache(session_id)")
        c.commit()
        _conn._c = c
    return _conn._c


def _query_hash(query: str) -> str:
    """sha256 前 16 位，建 INDEX 防同一 query 重复入库。"""
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:16]


def _threshold() -> float:
    try:
        from ....config import get_settings
        return float(getattr(get_settings(), "semantic_cache_similarity_threshold", 0.92) or 0.92)
    except Exception:
        return 0.92


def get_semantic(session_id: str, query: str) -> Optional[object]:
    """同 session 内按 cosine >= threshold 命中。命中时：
    - 深拷贝 AgentState（避免可变共享）
    - 在 metadata 上打 {"cache_hit": True, "cache_key":"semantic", "similarity": 0.xx,
                         "matched_query": "历史问题原文"}
    - 返回新对象
    没命中 → None
    """
    try:
        q_vec = embed_query(query)
        q_hash = _query_hash(query)
        conn = _conn()
        # 先按 query_hash 做粗过滤（精确同 query 直接命中，跳过 cosine 计算）
        rows = conn.execute(
            "SELECT id, query, embedding_json, state_json FROM semantic_cache "
            "WHERE session_id = ? AND query_hash = ?",
            (session_id, q_hash),
        ).fetchall()

        # 同 hash → cosine=1.0（同一个 query）
        for _id, q_text, emb_json, state_json in rows:
            try:
                state = _state_from_json(state_json)
                if state is None:
                    continue
                state.metadata["cache_hit"] = True
                state.metadata["cache_key"] = "semantic"
                state.metadata["similarity"] = 1.0
                state.metadata["matched_query"] = q_text
                state.metadata["matched_session"] = session_id
                return state
            except Exception:
                continue

        # 候选集：同 session 内所有记录（走 cosine 比较）
        rows = conn.execute(
            "SELECT id, query, embedding_json, state_json FROM semantic_cache WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        best_sim = _threshold()
        best_state = None
        best_query = ""

        for _id, q_text, emb_json, state_json in rows:
            try:
                hist_vec = json.loads(emb_json)
                sim = _cosine(q_vec, hist_vec)
                if sim >= best_sim:
                    # 取最高相似度的那一条
                    if best_state is None or sim > best_sim:
                        state = _state_from_json(state_json)
                        if state is None:
                            continue
                        best_sim = sim
                        best_state = state
                        best_query = q_text
            except Exception:
                continue

        if best_state is not None:
            best_state.metadata["cache_hit"] = True
            best_state.metadata["cache_key"] = "semantic"
            best_state.metadata["similarity"] = round(best_sim, 4)
            best_state.metadata["matched_query"] = best_query
            best_state.metadata["matched_session"] = session_id
            return best_state

        return None
    except Exception as exc:
        logger.warning("语义缓存查询失败（静默降级）：%s", exc)
        return None


def put_semantic(session_id: str, query: str, state: object) -> None:
    """与 response_cache.put_cached 同样的"三不存"守卫：
    - status != FINISH
    - report 为空
    - degraded（降级兜底报告不复用）
    通过守卫后 embed + 写 SQLite。
    """
    try:
        if getattr(state, "status", "") != "FINISH":
            return
        if not getattr(state, "report", ""):
            return
        try:
            if (getattr(state, "metadata", None) or {}).get("degraded"):
                logger.warning("降级结果不入语义缓存（避免模板报告被复用）")
                return
        except Exception:
            pass

        vec = embed_query(query)
        q_hash = _query_hash(query)
        state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False)
        emb_json = json.dumps(vec)

        conn = _conn()
        with _lock:
            conn.execute(
                "INSERT INTO semantic_cache "
                "(session_id, query, embedding_json, state_json, created_at, query_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, query, emb_json, state_json, __import__("time").time(), q_hash),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("语义缓存写入失败（静默降级）：%s", exc)


def clear_semantic(session_id: Optional[str] = None) -> None:
    """清缓存（测试与运维用；给 session_id 时只清该会话）。"""
    try:
        conn = _conn()
        if session_id is None:
            conn.execute("DELETE FROM semantic_cache")
        else:
            conn.execute("DELETE FROM semantic_cache WHERE session_id = ?", (session_id,))
        conn.commit()
    except Exception as exc:
        logger.warning("语义缓存清理失败（静默）：%s", exc)


def _reset_connection() -> None:
    """关闭 SQLite 连接（测试隔离用，避免跨测试共享连接 + DB 路径变更不生效）。"""
    if hasattr(_conn, "_c") and _conn._c is not None:
        try:
            _conn._c.close()
        except Exception:
            pass
        _conn._c = None


def semantic_enabled() -> bool:
    try:
        from ....config import get_settings
        return bool(getattr(get_settings(), "semantic_cache_enabled", True))
    except Exception:
        return False


def _state_from_json(state_json: str):
    """反序列化 AgentState，失败返回 None。"""
    try:
        from .state import AgentState
        return AgentState.model_validate_json(state_json)
    except Exception:
        # 兼容老版本可能用 mode="json" dump 的 dict
        try:
            from .state import AgentState
            return AgentState.model_validate(json.loads(state_json))
        except Exception:
            return None
