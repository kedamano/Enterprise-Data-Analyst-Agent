"""多知识库目录（KB Catalog）——「我的知识库」这一层的真相源。

**为什么需要它**：原实现是「单库」——``chunks`` 表只有 ``source`` / ``tenant``，
没有「库」这一层。于是前端知识库面板只能摊开一张扁平的「来源 → 分块数」清单，
做不出资产管理该有的形态：多库并列、库有类型（通用 / 网站）、创建者、可见性、
按库检索、按库统计。

**职责拆分（关键设计）**：把「库 / 文档」的**元信息**与「分块 / 向量」的
**存储**解耦，各管各的：

* 元信息（库名、描述、类型、可见性、文档名、字节数、入库时间）→ 本模块
  （独立 SQLite：``data/knowledge_meta.db``）
* 分块正文与 embedding → ``KnowledgeStore``（``knowledge.db``）或 Milvus

好处：换向量后端（SQLite ↔ Milvus）不会把库结构弄丢；删库时由调用方按
``kb_id`` 级联清理分块，职责清晰。

**向后兼容**：历史分块没有 ``kb_id``。首次使用本模块时会建一个默认库，
并把那些无归属分块**接管**进来（``adopt_orphan_chunks``），避免它们在
新界面上"消失"。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

# 路径可用 ``KNOWLEDGE_META_DB`` 覆盖。默认不变（``data/knowledge_meta.db``）。
# 为什么需要：本模块的路径是模块级常量，缺开关时**测试与多实例部署无法隔离目录库**——
# 跑一次测试就会往真实目录里塞一条空库（实测累积 16 条同名「诊断测试库」），
# 起一个旁路实例做验证也会和开发实例共用同一份目录。
_DB_PATH = Path(os.environ.get("KNOWLEDGE_META_DB") or (Path("data") / "knowledge_meta.db"))
_LOCK = threading.Lock()

# 默认库名：历史无归属分块会被收进这里（企业术语 / 指标口径 / 业务规则）
DEFAULT_BASE_NAME = "企业知识库"
DEFAULT_BASE_DESC = "系统默认知识库：企业术语、指标口径与业务规则。"

# 库类型 / 可见性取值（前端下拉与之对齐）
KB_TYPES = ("general", "website")
KB_VISIBILITIES = ("private", "public")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class KnowledgeCatalog:
    """知识库与文档的元信息目录（SQLite）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db = str(db_path or _DB_PATH)
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    kb_type TEXT NOT NULL DEFAULT 'general',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    owner TEXT NOT NULL DEFAULT '本地用户',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS kb_documents (
                    id TEXT PRIMARY KEY,
                    kb_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    doc_type TEXT NOT NULL DEFAULT 'file',
                    mime TEXT NOT NULL DEFAULT '',
                    bytes INTEGER NOT NULL DEFAULT 0,
                    chunks INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ready',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS kb_documents_kb ON kb_documents(kb_id)"
            )

    # ------------------------------------------------------------------ 库
    def list_bases(self) -> list[dict[str, Any]]:
        """全部知识库（按更新时间倒序），带文档数与分块数。"""
        with _LOCK, sqlite3.connect(self.db) as c:
            rows = c.execute(
                "SELECT id, name, description, kb_type, visibility, owner, "
                "created_at, updated_at FROM knowledge_bases ORDER BY updated_at DESC"
            ).fetchall()
            stats = dict(
                c.execute(
                    "SELECT kb_id, COUNT(*) FROM kb_documents GROUP BY kb_id"
                ).fetchall()
            )
            chunks = dict(
                c.execute(
                    "SELECT kb_id, COALESCE(SUM(chunks), 0) FROM kb_documents "
                    "GROUP BY kb_id"
                ).fetchall()
            )
        return [
            {
                "id": r[0], "name": r[1], "description": r[2], "kb_type": r[3],
                "visibility": r[4], "owner": r[5],
                "created_at": r[6], "updated_at": r[7],
                "documents": int(stats.get(r[0], 0)),
                "chunks": int(chunks.get(r[0], 0)),
            }
            for r in rows
        ]

    def get_base(self, kb_id: str) -> dict[str, Any] | None:
        for b in self.list_bases():
            if b["id"] == kb_id:
                return b
        return None

    def create_base(
        self,
        name: str,
        description: str = "",
        kb_type: str = "general",
        visibility: str = "private",
        owner: str = "本地用户",
    ) -> dict[str, Any]:
        name = (name or "").strip() or "未命名知识库"
        kb_type = kb_type if kb_type in KB_TYPES else "general"
        visibility = visibility if visibility in KB_VISIBILITIES else "private"
        kid = _new_id("kb")
        ts = _now()
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO knowledge_bases(id, name, description, kb_type, "
                "visibility, owner, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (kid, name, description or "", kb_type, visibility, owner, ts, ts),
            )
        return self.get_base(kid) or {}

    def update_base(self, kb_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "description", "kb_type", "visibility"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "kb_type" and v not in KB_TYPES:
                continue
            if k == "visibility" and v not in KB_VISIBILITIES:
                continue
            if k == "name":
                v = str(v).strip() or "未命名知识库"
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return self.get_base(kb_id)
        sets.append("updated_at=?")
        vals.extend([_now(), kb_id])
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(f"UPDATE knowledge_bases SET {', '.join(sets)} WHERE id=?", vals)
        return self.get_base(kb_id)

    def delete_base(self, kb_id: str) -> bool:
        with _LOCK, sqlite3.connect(self.db) as c:
            cur = c.execute("DELETE FROM knowledge_bases WHERE id=?", (kb_id,))
            c.execute("DELETE FROM kb_documents WHERE kb_id=?", (kb_id,))
            return bool(cur.rowcount)

    def touch_base(self, kb_id: str) -> None:
        """文档增减后刷新库的「最近修改」时间。"""
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                "UPDATE knowledge_bases SET updated_at=? WHERE id=?", (_now(), kb_id)
            )

    # ---------------------------------------------------------------- 文档
    def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        with _LOCK, sqlite3.connect(self.db) as c:
            rows = c.execute(
                "SELECT id, kb_id, name, source, doc_type, mime, bytes, chunks, "
                "status, created_at, updated_at FROM kb_documents "
                "WHERE kb_id=? ORDER BY created_at DESC",
                (kb_id,),
            ).fetchall()
        return [self._doc_row(r) for r in rows]

    @staticmethod
    def _doc_row(r: tuple) -> dict[str, Any]:
        return {
            "id": r[0], "kb_id": r[1], "name": r[2], "source": r[3],
            "doc_type": r[4], "mime": r[5], "bytes": int(r[6] or 0),
            "chunks": int(r[7] or 0), "status": r[8],
            "created_at": r[9], "updated_at": r[10],
        }

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        with _LOCK, sqlite3.connect(self.db) as c:
            row = c.execute(
                "SELECT id, kb_id, name, source, doc_type, mime, bytes, chunks, "
                "status, created_at, updated_at FROM kb_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        return self._doc_row(row) if row else None

    def find_document(self, kb_id: str, source: str) -> dict[str, Any] | None:
        with _LOCK, sqlite3.connect(self.db) as c:
            row = c.execute(
                "SELECT id, kb_id, name, source, doc_type, mime, bytes, chunks, "
                "status, created_at, updated_at FROM kb_documents "
                "WHERE kb_id=? AND source=?",
                (kb_id, source),
            ).fetchone()
        return self._doc_row(row) if row else None

    def add_document(
        self,
        kb_id: str,
        name: str,
        source: str,
        doc_type: str = "file",
        mime: str = "",
        bytes: int = 0,
        chunks: int = 0,
        status: str = "ready",
    ) -> dict[str, Any]:
        """新增文档记录；同库同 source 则**更新**（重新上传 = 覆盖语义）。"""
        existing = self.find_document(kb_id, source)
        ts = _now()
        if existing:
            with _LOCK, sqlite3.connect(self.db) as c:
                c.execute(
                    "UPDATE kb_documents SET name=?, doc_type=?, mime=?, bytes=?, "
                    "chunks=?, status=?, updated_at=? WHERE id=?",
                    (name, doc_type, mime, int(bytes), int(chunks), status, ts,
                     existing["id"]),
                )
            self.touch_base(kb_id)
            return self.get_document(existing["id"]) or {}
        did = _new_id("doc")
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO kb_documents(id, kb_id, name, source, doc_type, mime, "
                "bytes, chunks, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (did, kb_id, name, source, doc_type, mime, int(bytes), int(chunks),
                 status, ts, ts),
            )
        self.touch_base(kb_id)
        return self.get_document(did) or {}

    def update_document(self, doc_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "chunks", "bytes", "status"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return self.get_document(doc_id)
        sets.append("updated_at=?")
        vals.extend([_now(), doc_id])
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(f"UPDATE kb_documents SET {', '.join(sets)} WHERE id=?", vals)
        return self.get_document(doc_id)

    def delete_document(self, doc_id: str) -> dict[str, Any] | None:
        doc = self.get_document(doc_id)
        if not doc:
            return None
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM kb_documents WHERE id=?", (doc_id,))
        self.touch_base(doc["kb_id"])
        return doc

    # ---------------------------------------------------------------- 统计
    def stats(self) -> dict[str, int]:
        with _LOCK, sqlite3.connect(self.db) as c:
            bases = int(c.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0])
            docs = int(c.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0])
            chunks = int(
                c.execute(
                    "SELECT COALESCE(SUM(chunks), 0) FROM kb_documents"
                ).fetchone()[0]
            )
        return {"bases": bases, "documents": docs, "chunks": chunks}

    def base_exists(self, kb_id: str) -> bool:
        with _LOCK, sqlite3.connect(self.db) as c:
            return bool(
                c.execute(
                    "SELECT 1 FROM knowledge_bases WHERE id=? LIMIT 1", (kb_id,)
                ).fetchone()
            )

    # ------------------------------------------------------------ 首次接管
    def ensure_seed(self) -> dict[str, Any]:
        """首次使用：建默认库，并把历史无归属分块接管进来。幂等。

        只在「一个库都没有」时执行，因此不会覆盖用户自建的库；接管本身也是
        幂等的（只动 ``kb_id IS NULL`` 的行）。
        """
        with _LOCK, sqlite3.connect(self.db) as c:
            has_any = c.execute("SELECT 1 FROM knowledge_bases LIMIT 1").fetchone()
        if has_any:
            return {}

        base = self.create_base(DEFAULT_BASE_NAME, DEFAULT_BASE_DESC)
        try:
            from .tools.knowledge_tool import get_store

            adopted: dict[str, int] = get_store().adopt_orphan_chunks(base["id"])
            for src, n in adopted.items():
                self.add_document(
                    base["id"], name=_display_name(src), source=src,
                    doc_type="text", chunks=n,
                )
        except Exception:  # 向量库不可用也不能阻断目录初始化
            pass
        return base


def _display_name(source: str) -> str:
    """把 source 转成给人看的文档名（URL 保留，路径只留文件名）。"""
    s = (source or "").strip()
    if s.startswith(("http://", "https://")):
        return s
    p = Path(s)
    return p.name if p.name else s


_CATALOG: KnowledgeCatalog | None = None


def get_catalog() -> KnowledgeCatalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = KnowledgeCatalog()
        try:
            _CATALOG.ensure_seed()
        except Exception:
            pass
    return _CATALOG
