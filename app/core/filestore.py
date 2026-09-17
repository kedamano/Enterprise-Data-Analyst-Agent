"""企业文件管理（文件库）——目录树 + 文件 CRUD + 磁盘落地。

**与「会话附件」的区别**（``core/attachments.py``）：那个是**会话级临时输入**，
绑定 ``session_id``，随会话生灭，只服务于"这一轮分析吃什么数据"；本模块是
**用户级持久资产**，有目录结构、可全库搜索、可下载、可改名，跨会话长期存在。
两者不是一回事，所以各自独立，互不覆盖。

**存储设计**：
* 元信息（树、名字、大小、时间）→ SQLite（``data/filestore.db``，``fs_nodes`` 表）
* 文件字节 → ``data/filestore/blobs/<node_id>``，**以节点 id 命名**

为什么字节不按原始文件名落盘：中文名、空格、同名覆盖、以及最要紧的
**路径穿越**（``../../etc/passwd``）都会被"用 id 当文件名"一次性消除——
名字只活在数据库里，磁盘上永远是受控的 id。

**同名语义**：同目录下重名拒绝（唯一索引），上传同名文件 = 覆盖原文件
（更新字节与大小），符合网盘/资源管理器的直觉。
"""
from __future__ import annotations

import mimetypes
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .safe_fs import purge_file

_DB_PATH = Path("data") / "filestore.db"
_BLOB_DIR = Path("data") / "filestore" / "blobs"
_LOCK = threading.Lock()

ROOT = ""  # parent_id 为空串表示根目录（NULL 在唯一索引里互不相等，不能用）
MAX_NAME_LEN = 120
# 预置目录骨架：空树什么都点不了，企业文件管理需要一组默认分类
SEED_FOLDERS = ("数据集", "业务资料", "分析报告")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return f"n_{uuid.uuid4().hex[:12]}"


def safe_name(name: str) -> str:
    """清洗上传/新建时的名字：去路径分隔符与控制字符，防目录穿越。

    保留中文与常见符号（企业文件名大量是中文），只干掉真正危险的部分。
    """
    n = (name or "").strip().replace("\\", "_").replace("/", "_")
    n = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", n)
    n = n.strip(". ")  # Windows 不允许以点/空格结尾
    if not n:
        n = "未命名"
    return n[:MAX_NAME_LEN]


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def human_size(n: int) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


class FileStore:
    """文件库的唯一入口。所有方法线程安全（每个操作独立连接 + 全局锁）。"""

    def __init__(self, db_path: Path | str | None = None,
                 blob_dir: Path | str | None = None) -> None:
        self.db = str(db_path or _DB_PATH)
        self.blobs = Path(blob_dir or _BLOB_DIR)
        Path(self.db).parent.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS fs_nodes (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    is_dir INTEGER NOT NULL DEFAULT 0,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    mime TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            # 同目录不允许重名（含同名文件夹）；NOCASE 顺带挡住大小写变体
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS fs_nodes_uniq "
                "ON fs_nodes(parent_id, name COLLATE NOCASE)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS fs_nodes_parent ON fs_nodes(parent_id)")

    # ---------------------------------------------------------------- 读
    @staticmethod
    def _row(r: tuple) -> dict[str, Any]:
        return {
            "id": r[0], "parent_id": r[1], "name": r[2], "is_dir": bool(r[3]),
            "bytes": int(r[4] or 0), "mime": r[5],
            "size_label": "" if r[3] else human_size(int(r[4] or 0)),
            "created_at": r[6], "updated_at": r[7],
        }

    _COLS = "id, parent_id, name, is_dir, bytes, mime, created_at, updated_at"

    def list_all(self) -> list[dict[str, Any]]:
        """全量节点（轻量，左树一次拉完；企业文件量级远小于需要分页的规模）。"""
        with _LOCK, sqlite3.connect(self.db) as c:
            rows = c.execute(
                f"SELECT {self._COLS} FROM fs_nodes ORDER BY is_dir DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, node_id: str) -> dict[str, Any] | None:
        with _LOCK, sqlite3.connect(self.db) as c:
            row = c.execute(
                f"SELECT {self._COLS} FROM fs_nodes WHERE id=?", (node_id,)
            ).fetchone()
        return self._row(row) if row else None

    def list_children(self, parent_id: str | None) -> list[dict[str, Any]]:
        pid = parent_id or ROOT
        with _LOCK, sqlite3.connect(self.db) as c:
            rows = c.execute(
                f"SELECT {self._COLS} FROM fs_nodes WHERE parent_id=? "
                "ORDER BY is_dir DESC, name COLLATE NOCASE",
                (pid,),
            ).fetchall()
        return [self._row(r) for r in rows]

    def breadcrumb(self, parent_id: str | None) -> list[dict[str, Any]]:
        """从根到 *parent_id* 的路径链（含自身），用于面包屑导航。"""
        chain: list[dict[str, Any]] = []
        cur = parent_id or ROOT
        guard = 0
        while cur and guard < 64:
            guard += 1
            node = self.get(cur)
            if not node:
                break
            chain.append(node)
            cur = node["parent_id"]
        chain.reverse()
        return chain

    def path_of(self, node_id: str) -> str:
        """完整展示路径，如 ``/数据集/2024/orders.csv``。"""
        chain = self.breadcrumb(node_id)
        if not chain:
            return "/"
        return "/" + "/".join(n["name"] for n in chain)

    def search(self, q: str, limit: int = 200) -> list[dict[str, Any]]:
        """全库按名称模糊搜索（不区分大小写），结果带完整路径。"""
        kw = (q or "").strip()
        if not kw:
            return []
        like = f"%{kw}%"
        with _LOCK, sqlite3.connect(self.db) as c:
            rows = c.execute(
                f"SELECT {self._COLS} FROM fs_nodes WHERE name LIKE ? "
                "ORDER BY is_dir DESC, name COLLATE NOCASE LIMIT ?",
                (like, int(limit)),
            ).fetchall()
        out = []
        for r in rows:
            node = self._row(r)
            node["path"] = self.path_of(node["id"])
            out.append(node)
        return out

    def stats(self) -> dict[str, int]:
        with _LOCK, sqlite3.connect(self.db) as c:
            folders = int(c.execute(
                "SELECT COUNT(*) FROM fs_nodes WHERE is_dir=1").fetchone()[0])
            files = int(c.execute(
                "SELECT COUNT(*) FROM fs_nodes WHERE is_dir=0").fetchone()[0])
            total = int(c.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM fs_nodes WHERE is_dir=0"
            ).fetchone()[0])
        return {"folders": folders, "files": files, "bytes": total}

    # ---------------------------------------------------------------- 写
    def _name_taken(self, parent_id: str, name: str,
                    exclude_id: str | None = None) -> bool:
        with _LOCK, sqlite3.connect(self.db) as c:
            row = c.execute(
                "SELECT id FROM fs_nodes WHERE parent_id=? AND name=? COLLATE NOCASE",
                (parent_id or ROOT, name),
            ).fetchone()
        return bool(row) and row[0] != exclude_id

    def create_folder(self, parent_id: str | None, name: str) -> dict[str, Any]:
        pid = parent_id or ROOT
        if pid and not (self.get(pid) or {}).get("is_dir"):
            raise ValueError("父目录不存在")
        nm = safe_name(name)
        if self._name_taken(pid, nm):
            raise ValueError(f"「{nm}」已存在")
        nid, ts = _new_id(), _now()
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO fs_nodes(id, parent_id, name, is_dir, bytes, mime, "
                "created_at, updated_at) VALUES(?,?,?,1,0,'',?,?)",
                (nid, pid, nm, ts, ts),
            )
        return self.get(nid) or {}

    def save_file(self, parent_id: str | None, name: str, data: bytes,
                  mime: str = "") -> dict[str, Any]:
        """存文件；同目录同名 = **覆盖**（更新字节与大小，不新建节点）。

        覆盖而不是报错，是因为"再传一次"在用户心里就是"更新这个文件"。
        """
        pid = parent_id or ROOT
        if pid and not (self.get(pid) or {}).get("is_dir"):
            raise ValueError("父目录不存在")
        nm = safe_name(name)
        ts = _now()
        with _LOCK, sqlite3.connect(self.db) as c:
            row = c.execute(
                "SELECT id, is_dir FROM fs_nodes WHERE parent_id=? AND name=? COLLATE NOCASE",
                (pid, nm),
            ).fetchone()
        existing_id = row[0] if row else None
        if existing_id and row[1]:
            raise ValueError(f"「{nm}」是一个文件夹，不能作为文件名")

        nid = existing_id or _new_id()
        blob = self.blobs / nid
        blob.write_bytes(data)
        if existing_id:
            with _LOCK, sqlite3.connect(self.db) as c:
                c.execute(
                    "UPDATE fs_nodes SET bytes=?, mime=?, updated_at=? WHERE id=?",
                    (len(data), mime or guess_mime(nm), ts, nid),
                )
        else:
            with _LOCK, sqlite3.connect(self.db) as c:
                c.execute(
                    "INSERT INTO fs_nodes(id, parent_id, name, is_dir, bytes, mime, "
                    "created_at, updated_at) VALUES(?,?,?,0,?,?,?,?)",
                    (nid, pid, nm, len(data), mime or guess_mime(nm), ts, ts),
                )
        return self.get(nid) or {}

    def rename(self, node_id: str, new_name: str) -> dict[str, Any]:
        node = self.get(node_id)
        if not node:
            raise ValueError("节点不存在")
        nm = safe_name(new_name)
        if self._name_taken(node["parent_id"], nm, exclude_id=node_id):
            raise ValueError(f"「{nm}」已存在")
        with _LOCK, sqlite3.connect(self.db) as c:
            c.execute(
                "UPDATE fs_nodes SET name=?, updated_at=? WHERE id=?",
                (nm, _now(), node_id),
            )
        return self.get(node_id) or {}

    def blob_path(self, node_id: str) -> Path | None:
        node = self.get(node_id)
        if not node or node["is_dir"]:
            return None
        p = self.blobs / node_id
        return p if p.exists() else None

    def delete(self, node_id: str) -> int:
        """删除节点（目录递归）。返回删除的节点总数（含子孙）。"""
        node = self.get(node_id)
        if not node:
            return 0
        ids = self._descendant_ids(node_id)
        ids.append(node_id)
        for nid in ids:
            # 走 safe_fs：物理删除失败（文件被占用，或被运行环境的安全删除钩子
            # 拦截并抛 SystemExit）绝不能让异常外泄——本方法在请求路径上，
            # SystemExit 是 BaseException，会穿透 uvicorn 的异常处理打挂整个服务。
            purge_file(self.blobs / nid)
        with _LOCK, sqlite3.connect(self.db) as c:
            qs = ",".join("?" for _ in ids)
            c.execute(f"DELETE FROM fs_nodes WHERE id IN ({qs})", ids)
        return len(ids)

    def _descendant_ids(self, node_id: str) -> list[str]:
        out: list[str] = []
        frontier = [node_id]
        guard = 0
        while frontier and guard < 200:
            guard += 1
            qs = ",".join("?" for _ in frontier)
            with _LOCK, sqlite3.connect(self.db) as c:
                rows = c.execute(
                    f"SELECT id FROM fs_nodes WHERE parent_id IN ({qs})", frontier
                ).fetchall()
            kids = [r[0] for r in rows]
            out.extend(kids)
            frontier = kids
        return out

    def ensure_seed(self) -> None:
        """首次使用：建默认分类目录。幂等——根目录非空时不动。"""
        if self.list_children(ROOT):
            return
        for nm in SEED_FOLDERS:
            try:
                self.create_folder(ROOT, nm)
            except ValueError:
                pass


_STORE: FileStore | None = None


def get_filestore() -> FileStore:
    global _STORE
    if _STORE is None:
        _STORE = FileStore()
        try:
            _STORE.ensure_seed()
        except Exception:
            pass
    return _STORE
