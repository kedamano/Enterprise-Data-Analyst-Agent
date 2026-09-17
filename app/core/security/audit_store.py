"""D46：**审计落库**（SQLite / PostgreSQL）+ SQL 查询。

缺口（`docs/对标企业级Gap.md` 五）
----------------------------------
> 审计为文件非**不可篡改库**、无 **SQL 审计查询**。

现实：四条审计流都是 JSONL（`tool_audit` / `auth` / `masking` / `hitl`）。
复盘"谁在什么时候导出了什么"只能 grep，而且**文件可被就地改写**——
出了争议时，这份记录不具备证据力。

后端三选一（`AUDIT_BACKEND`）
-----------------------------
| 值 | 落点 | 适用 |
|---|---|---|
| `jsonl`（**默认**） | 四个既有文件，行为不变 | 向后兼容、本地开发 |
| `sqlite` | 单文件库（`AUDIT_DB_URL`） | 单机、可 SQL 查询 |
| `postgres` | 复用 `POSTGRES_DSN` | 多副本共享、可权限收紧 |

**不做双写。** 审计写两处会立刻带来"以哪份为准"的新问题；
只写一处，由配置决定，历史数据用 `import_jsonl()` 一次性迁入。

写入**绝不打断业务**（沿用既有纪律：审计故障吞掉并记 warning）；
但**吞掉不等于不管**——失败会 `logger.warning`，不是静默 `except: pass`。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("da.audit")

#: JSONL 追加锁。**不是性能优化，是正确性**（2026-09-15 实证）：
#: 并行执行器 4 个 worker 会同时写审计，无锁时 1200 次 `record()` 只有 1069~1174 条落盘，
#: 且**零异常零告警**（`record()` 的"绝不抛"把失败吞了），落盘还出现半行。
#: 审计记录无声缺失＝审计链可被截断——出争议时这份记录不胜任证据。
_jsonl_lock = threading.Lock()

_TABLE = "audit_events"
# kind → 默认 JSONL 文件（`jsonl` 后端沿用，保证既有行为一字不变）
JSONL_PATHS: dict[str, Path] = {
    "tool": Path("data/audit/tool_audit.jsonl"),
    "auth": Path("data/audit/auth.jsonl"),
    "masking": Path("data/audit/masking.jsonl"),
    "hitl": Path("data/audit/hitl.jsonl"),
}


def _settings() -> Any:
    from ...config import get_settings

    return get_settings()


def _backend() -> str:
    return str(getattr(_settings(), "audit_backend", "jsonl") or "jsonl").lower()


def _db_url() -> str:
    st = _settings()
    url = str(getattr(st, "audit_db_url", "") or "")
    if url:
        return url
    # postgres 后端复用 POSTGRES_DSN；否则本地 SQLite
    if _backend() == "postgres":
        return str(getattr(st, "postgres_dsn", "") or "")
    return "sqlite:///./data/audit.db"


def fingerprint(kind: str, entry: dict) -> str:
    """内容指纹 → **重复导入幂等**（同一份 JSONL import 两次不会翻倍）。"""
    raw = kind + json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _engine():
    from sqlalchemy import create_engine

    url = _db_url()
    if not url:
        raise RuntimeError("审计库未配置（AUDIT_DB_URL / POSTGRES_DSN 均为空）")
    return create_engine(url, pool_pre_ping=True)


def _ensure_table(engine) -> None:
    from sqlalchemy import Column, Integer, MetaData, String, Table, Text, Index

    meta = MetaData()
    table = Table(
        _TABLE, meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("ts", String(32), nullable=False),
        Column("kind", String(32), nullable=False),
        Column("session_id", String(128)),
        Column("payload", Text, nullable=False),
        Column("fingerprint", String(64), nullable=False, unique=True),
    )
    Index("ix_audit_kind_ts", table.c.kind, table.c.ts)
    meta.create_all(engine)


def record(kind: str, entry: dict, *, path: Path | str | None = None) -> None:
    """写一条审计。**绝不抛**（审计故障不得打断业务）。

    ``path``：`jsonl` 后端下的落点覆盖。各调用方传入**自己的模块级常量**，
    这样既保持既有文件位置，也让测试里对常量的 monkeypatch 继续生效。
    """
    try:
        if _backend() == "jsonl":
            _append_jsonl(kind, entry, path)
            return
        _insert(kind, entry)
    except Exception:  # noqa: BLE001
        logger.warning("审计写入失败（kind=%s，已忽略但不静默）", kind, exc_info=True)


def _append_jsonl(kind: str, entry: dict, path: Path | str | None = None) -> None:
    """追加一条 JSONL。**必须并发安全**——见 `_jsonl_lock` 的注释。

    写的是**字节**而不是文本模式：文本层的缓冲 + 每个线程各持一个句柄，
    正是丢记录的原因（实测 1200 条少 21~131 条）。这里改成
    "锁内单次 `os.write` 到 `O_APPEND` 句柄"——进程内由锁串行化，
    跨进程靠 `O_APPEND` 的原子追加；一次系统调用写完**整行**，
    不留"别的写入方插进来"的窗口。产物字节与文本模式一字不差（仍是 UTF-8 无 BOM）。
    """
    target = Path(path) if path else JSONL_PATHS.get(kind, Path(f"data/audit/{kind}.jsonl"))
    line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    with _jsonl_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
            0o644,
        )
        try:
            view = memoryview(line)
            while view:  # os.write 可能短写；循环到写完为止
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


def _row(kind: str, entry: dict) -> dict:
    return {
        "ts": str(entry.get("ts") or datetime.now(timezone.utc).isoformat()),
        "kind": kind,
        "session_id": str(entry.get("session_id") or entry.get("session") or "") or None,
        "payload": json.dumps(entry, ensure_ascii=False, default=str),
        "fingerprint": fingerprint(kind, entry),
    }


def _insert(kind: str, entry: dict) -> None:
    from sqlalchemy import insert

    engine = _engine()
    _ensure_table(engine)
    with engine.begin() as conn:
        conn.execute(insert(_table(engine)), [_row(kind, entry)])


def _table(engine):
    from sqlalchemy import Column, Integer, MetaData, String, Table, Text

    meta = MetaData()
    return Table(
        _TABLE, meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("ts", String(32), nullable=False),
        Column("kind", String(32), nullable=False),
        Column("session_id", String(128)),
        Column("payload", Text, nullable=False),
        Column("fingerprint", String(64), nullable=False),
    )


def import_jsonl(kind: str, path: Path | str | None = None) -> int:
    """把历史 JSONL 迁入审计库 → 返回**新增**条数（重复导入不翻倍）。"""
    from sqlalchemy import insert

    src = Path(path) if path else JSONL_PATHS.get(kind)
    if not src or not src.exists():
        return 0
    engine = _engine()
    _ensure_table(engine)
    table = _table(engine)
    added = 0
    with engine.begin() as conn:
        for line in src.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                conn.execute(insert(table).values(**_row(kind, entry)))
                added += 1
            except Exception:      # 唯一约束冲突 = 已导入过
                continue
    logger.info("审计导入 %s：新增 %d 条（源 %s）", kind, added, src)
    return added


def query(*, kind: Optional[str] = None, session_id: Optional[str] = None,
          since: Optional[str] = None, limit: int = 200) -> list[dict]:
    """按条件查审计（**仅 DB 后端**；`jsonl` 后端请直接读文件）。"""
    from sqlalchemy import select

    if _backend() == "jsonl":
        return []
    engine = _engine()
    _ensure_table(engine)
    table = _table(engine)
    stmt = select(table).order_by(table.c.id.desc()).limit(max(1, min(limit, 2000)))
    if kind:
        stmt = stmt.where(table.c.kind == kind)
    if session_id:
        stmt = stmt.where(table.c.session_id == session_id)
    if since:
        stmt = stmt.where(table.c.ts >= since)
    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            entry = json.loads(r.payload)
        except Exception:
            entry = {"_raw": r.payload}
        out.append({"id": r.id, "ts": r.ts, "kind": r.kind,
                    "session_id": r.session_id, "entry": entry})
    return out
