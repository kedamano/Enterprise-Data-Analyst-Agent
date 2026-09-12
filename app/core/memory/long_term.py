"""Long-term memory – cross-session learnings / resolved metric definitions.

Backends (spec §20 Memory: Redis/PG tier):
* **PostgreSQL** when ``postgres_dsn`` is configured – table ``da_long_term``
  (JSONB), survives restarts and multiple workers.
* **JSONL file** fallback under ``data/`` – zero-dependency offline mode.

Sensitive data must never be written here (per the Security section of the
system prompt), regardless of backend.
"""
from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path

from ...config import get_settings

_LOCK = threading.Lock()

# PG 路径的候选窗口：三因子排序在本地做，用「最近 N 条」兜住规模（再大应换向量索引）
_CANDIDATE_WINDOW = 2000


def _path() -> Path:
    """JSONL 兜底路径（可配 `LONG_TERM_PATH`；测试用 tmp 隔离）。"""
    from ...config import get_settings

    return Path(getattr(get_settings(), "long_term_path", "data/long_term.jsonl"))

_SENSITIVE_KEYS = {"credential", "secret", "api_key", "password"}


def _effective_tenant(tenant: str | None) -> str:
    return (tenant or "").strip() or (get_settings().default_tenant or "").strip()


def _sanitize(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in _SENSITIVE_KEYS}


@lru_cache(maxsize=1)
def _pg_engine():
    """Return a SQLAlchemy engine (with table ensured) when DSN is configured."""
    settings = get_settings()
    if not settings.postgres_dsn:
        return None
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(settings.postgres_dsn, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS da_long_term ("
                " id SERIAL PRIMARY KEY,"
                " ts TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " data JSONB NOT NULL)"
            ))
        return engine
    except Exception:
        return None  # PG 不可用 → 调用方回退 JSONL


def _load() -> list[dict]:
    if not _path().exists():
        return []
    out = []
    for line in _path().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append(entry: dict, tenant: str | None = None) -> None:
    """Append a non-sensitive long-term memory entry (e.g. a resolved KPI def)."""
    entry = _sanitize(entry)
    # 时效因子的前提：落库必须带时间戳（ISO UTC，与 PG 的 ts 列语义一致）
    if not entry.get("ts"):
        from datetime import datetime, timezone

        entry["ts"] = datetime.now(timezone.utc).isoformat()
    ten = _effective_tenant(tenant)
    if ten:
        entry["tenant"] = ten  # 存入 JSON data，PG/JSONL 后端统一生效
    engine = _pg_engine()
    if engine is not None:
        from sqlalchemy import text
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO da_long_term (data) VALUES (CAST(:d AS JSONB))"),
                         {"d": json.dumps(entry, ensure_ascii=False)})
        return
    with _LOCK:
        _path().parent.mkdir(parents=True, exist_ok=True)
        with _path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def search(query: str, top_k: int = 5, tenant: str | None = None) -> list[dict]:
    """按**三因子**（相关性/时效/重要性）召回长期记忆。

    与旧实现（子串匹配 + 时间倒序）的差别：一条三个月前的高相关结论，
    不该输给昨天的无关摘要。排序在本地做，故需要把候选取回来——
    PG 路径取「最近 N 条」窗口兜住规模（`_CANDIDATE_WINDOW`），
    JSONL 路径全量扫描（本地文件，量级可控）。**规模再大应换向量索引**。
    """
    from .scoring import rank

    ten = _effective_tenant(tenant)
    engine = _pg_engine()
    if engine is not None:
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                if ten:
                    rows = conn.execute(
                        text("SELECT data FROM da_long_term "
                             "WHERE data->>'tenant' = :t "
                             "ORDER BY ts DESC LIMIT :n"),
                        {"t": ten, "n": _CANDIDATE_WINDOW},
                    ).fetchall()
                else:
                    rows = conn.execute(
                        text("SELECT data FROM da_long_term ORDER BY ts DESC LIMIT :n"),
                        {"n": _CANDIDATE_WINDOW},
                    ).fetchall()
            return rank([r[0] for r in rows], query, top_k=top_k)
        except Exception:
            pass  # 回退 JSONL
    hits = [e for e in _load() if not (ten and e.get("tenant") != ten)]
    return rank(hits, query, top_k=top_k)


def recent_lessons(limit: int = 10, tenant: str | None = None) -> list[dict]:
    """返回最近的 lesson 类长期记忆（失败 / 降级教训），供会话复盘或排障。

    P1-3：把「降级 / 失败」显式写回长期记忆后，需要一条对应的回读通道，
    否则这些教训只进不出，等于没记。默认 JSONL 离线模式；配 PG 时走 SQL。
    """
    ten = _effective_tenant(tenant)
    engine = _pg_engine()
    if engine is not None:
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                if ten:
                    rows = conn.execute(
                        text("SELECT data FROM da_long_term "
                             "WHERE data->>'type' = 'lesson' AND data->>'tenant' = :t "
                             "ORDER BY ts DESC LIMIT :k"),
                        {"t": ten, "k": limit},
                    ).fetchall()
                else:
                    rows = conn.execute(
                        text("SELECT data FROM da_long_term "
                             "WHERE data->>'type' = 'lesson' "
                             "ORDER BY ts DESC LIMIT :k"),
                        {"k": limit},
                    ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            pass  # 回退 JSONL
    out = [e for e in _load()
           if e.get("type") == "lesson" and (not ten or e.get("tenant") == ten)]
    out.sort(key=lambda x: str(x.get("ts", "")), reverse=True)
    return out[:limit]
