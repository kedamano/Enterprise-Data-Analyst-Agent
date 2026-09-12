"""会话级附件暂存 + 可查询落地。

上传的 CSV / Excel / JSON 会被解析成「可查询的数据集」，并绑定到 session_id；
后续该 session 的分析请求可以把 session 里已上传的数据集信息注入 agent context，
让 Planner/Executor 知道「用户手上有什么数据」。

**ATTACH/01（关键修复）**：光把「列名+行数+样例」塞进提示词是不够的 —— 计划层依然会
去查内置库，导致「用户上传了 A，agent 却分析了 B」。因此这里额外把表格类附件
**物化成 SQLite 边车库**（``data/uploads/<session>/upload.db``），并在工具层用
``ATTACH DATABASE`` 挂载成 ``upload.<表名>``，让 ``sql_query`` / ``dataset_profile`` /
``python_analysis`` 都能直接寻址到用户的真实数据。

**ATTACH/02（持久化真相源）**：内存 dict 一重启就空，多 worker 之间也不共享，
于是 ``attached_tables()`` 会「看不见」已经落地的边车库，schema 发现与 SQL 挂载
两边不一致（SQL 能查到 ``upload.sleep``，Planner 却以为只有内置表）。
因此 **sidecar 文件是唯一真相源**：内存只做加速缓存，缓存未命中就从
``upload.db`` 反射表结构还原元数据。``upload.db`` 内额外维护一张 ``_meta`` 表，
记录每个上传表的源文件名/原始列序，保证跨进程也能还原完整上下文。

设计取舍：
- 内存 dict 只作缓存；真相在 ``data/uploads/<session>/upload.db``（可跨进程/重启）。
- 只保留结构化摘要（列名、行数、前 N 行样例），不保留整个 DataFrame，
  避免长会话把内存吃满；完整数据放在边车库里按需查询。
- 同名文件按 session 覆盖，保证「重新上传 = 更新」的直觉语义。
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 边车库的文件名；session 目录挂在 data/uploads/<session_id>/ 下
SIDECAR_NAME = "upload.db"
UPLOAD_DIR = Path("data") / "uploads"
# 边车库内的元数据表：记录每个上传表的源文件名 / 原始列序（跨进程还原用）
META_TABLE = "_meta"
# SQLite 并发等待窗口（秒）：并发物化/查询同一 sidecar 时避免 immediate 锁失败
_SQLITE_BUSY_TIMEOUT_S = 10.0

# --------------------------------------------------------------------------- #
# P2-1：小文件「全文进上下文」阈值
#
# 命中（字节数 + 行数都低于阈值）的表格附件，把**整张表**直接渲染进 prompt，
# 让模型可以就地计算，省去 SQL 往返。大文件仍走边车库 + SQL 路径（上下文装不下）。
# --------------------------------------------------------------------------- #
SMALL_FILE_MAX_BYTES = 50_000
SMALL_FILE_MAX_ROWS = 2_000
# 上下文里全文块的安全上限（双保险：即便越过上面阈值也不致撑爆 prompt）
_FULL_DATA_CONTEXT_CAP = 40_000


@dataclass
class DatasetPreview:
    """结构化数据集的内存摘要。"""

    name: str
    kind: str = "table"  # table | text
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)
    # 文本类附件（md/txt/pdf/代码）保留截断正文，供 LLM 参考
    excerpt: str = ""
    bytes: int = 0
    # ATTACH/01：表格类附件的可查询表名（不含 ``upload.`` 前缀），空表示未落地
    table: str = ""
    # 完整行数据（仅表格类，用于落地到边车库；不参与序列化/持久化）
    all_rows: list[dict[str, Any]] = field(default_factory=list)
    # P2-2：图片类附件落地到磁盘的绝对路径（image_analyze 工具据此读取原图）。
    # 文本/表格类为空。
    path: str = ""

    @property
    def is_small(self) -> bool:
        """P2-1：是否命中「小文件全文进上下文」阈值。

        同时卡字节数与行数——只满足其一不算（比如 1 行但 60KB 的宽表不应灌进 prompt）。
        """
        return (
            self.kind == "table"
            and self.rows > 0
            and self.bytes <= SMALL_FILE_MAX_BYTES
            and self.rows <= SMALL_FILE_MAX_ROWS
        )

    def to_context(self, max_sample: int = 5) -> str:
        """渲染成给 LLM 读的紧凑上下文片段。"""
        head = f"- 文件 `{self.name}`"
        if self.kind == "table":
            head += f"（表格，{self.rows} 行 × {len(self.columns)} 列）"
            if self.table:
                # 明确告诉模型「用 SQL 查这张表」，而不是只看样例
                head += f"\n  **可查询**：`SELECT ... FROM upload.{self.table}`"
                head += f"\n  （用户数据已落地，必须用 SQL/Python 查这张表拿真实数值，不要查其他表）"
            head += f"\n  列名：{', '.join(self.columns) or '(未识别)'}"
            if self.sample:
                rows = self.sample[:max_sample]
                head += "\n  前几行："
                for r in rows:
                    head += "\n    " + json.dumps(r, ensure_ascii=False, default=str)
            # P2-1：小文件把整张表直接灌进上下文，模型可就地计算，无需 SQL 往返
            if self.is_small:
                full = self._render_full_data()
                if full:
                    head += (
                        "\n  **全文数据**（小文件已直接注入上下文，可直接据此计算，"
                        "无需再走 SQL）：\n" + full
                    )
        elif self.kind == "text":
            head += "（文本）"
            if self.excerpt:
                head += f"\n  摘录：{self.excerpt[:400]}"
        elif self.kind == "image":
            head += f"（图片，{self.bytes} 字节）"
            if self.path:
                head += f"\n  本地路径：`{self.path}`"
            # 明确告诉模型：图片必须用 image_analyze 实际读取，不能只凭文件名臆测内容
            head += (
                "\n  **视觉解析**：调用 `image_analyze` 工具，传入本图文件名与你的分析问题，"
                "模型会识别图中的图表/数据/文字并返回结构化结果，可作为证据使用。"
                "（不要只凭文件名或猜测回答，必须用 image_analyze 实际读取图片）"
            )
        return head

    def _render_full_data(self) -> str:
        """把整张表渲染成 CSV 代码块（带引号转义）。空表返回空串。"""
        rows = self.all_rows or self.sample
        if not rows or not self.columns:
            return ""
        lines = [",".join(self.columns)]
        for r in rows:
            esc: list[str] = []
            for c in self.columns:
                v = r.get(c)
                if v is None:
                    v = ""
                s = str(v)
                if any(ch in s for ch in (",", '"', "\n", "\r")):
                    s = '"' + s.replace('"', '""') + '"'
                esc.append(s)
            lines.append(",".join(esc))
        text = "\n".join(lines)
        if len(text) > _FULL_DATA_CONTEXT_CAP:
            text = text[:_FULL_DATA_CONTEXT_CAP] + "\n...(数据过长已截断)"
        return "```csv\n" + text + "\n```"


def _slug(name: str) -> str:
    """把文件名转成合法 SQLite 表名标识符。"""
    stem = Path(name).stem.lower()
    ident = re.sub(r"[^a-z0-9_]+", "_", stem).strip("_")
    if not ident or ident[0].isdigit():
        ident = f"t_{ident}" if ident else "uploaded"
    return ident[:50]


class AttachmentStore:
    """session_id -> {文件名 -> DatasetPreview}"""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, DatasetPreview]] = {}

    def put(self, session_id: str, preview: DatasetPreview) -> None:
        """存入预览。

        ATTACH/01：表格类附件**在此自动落地**成边车库表，避免调用方忘记先
        ``materialize_table()`` 就 ``put()``，导致 ``preview.table`` 为空、
        上下文里写不出 ``upload.<表名>``、模型只能去猜数据在哪。
        """
        sid = session_id or "default"
        if preview.kind == "table" and not preview.table:
            materialize_table(preview, sid)
        bucket = self._data.setdefault(sid, {})
        bucket[preview.name] = preview

    def get(self, session_id: str | None) -> dict[str, DatasetPreview]:
        return dict(self._data.get(session_id or "default", {}))

    def clear(self, session_id: str | None) -> None:
        self._data.pop(session_id or "default", {})

    def describe(self, session_id: str | None) -> str:
        """把该 session 所有附件渲染成一段上下文文本。空则返回空串。"""
        items = self.get(session_id)
        if not items:
            return ""
        lines = ["【用户已上传的数据附件】"]
        lines += [p.to_context() for p in items.values()]
        tables = [p.table for p in items.values() if p.table]
        if tables:
            lines.append(
                "\n⚠️ 本轮分析必须以上述上传数据为准："
                f"用 `upload.{tables[0]}` 等表名查询，"
                "不要查询内置企业库的表（那是无关数据）。"
            )
        return "\n".join(lines)


_STORE = AttachmentStore()


def get_attachment_store() -> AttachmentStore:
    return _STORE


# ---------------------------------------------------------------- 边车库落地

def sidecar_path(session_id: str | None) -> Path:
    """该 session 的边车库路径（不保证存在）。"""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", (session_id or "default"))[:60] or "default"
    return (UPLOAD_DIR / sid / SIDECAR_NAME).resolve()


def materialize_table(preview: DatasetPreview, session_id: str | None = None) -> str:
    """把表格类附件写成边车库里的真实表，返回表名（失败返回空串）。

    这样 ``sql_query`` 用 ``ATTACH DATABASE`` 挂载后即可直接查询，
    Planner 也能通过 schema 发现它。
    """
    if preview.kind != "table" or not preview.columns:
        return ""
    # 完整行数据优先；否则退回 sample（不完整，仅兜底）
    rows: list[dict[str, Any]] = list(preview.all_rows or [])
    if not rows:
        rows = list(preview.sample or [])
    if not rows:
        return ""

    sid = session_id or getattr(preview, "session_id", None)
    path = sidecar_path(sid)
    table = _slug(preview.name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=_SQLITE_BUSY_TIMEOUT_S)
        # 并发物化同一 sidecar（多 worker / 并发上传 / 并行测试）会短暂持锁；
        # 给足等待窗口，避免 transient 的 "database is locked" 变成数据缺失。
        try:
            con.execute(f"PRAGMA busy_timeout = {int(_SQLITE_BUSY_TIMEOUT_S * 1000)}")
        except Exception:
            pass
        try:
            cols = [c for c in preview.columns if c]
            # 全部按 TEXT 存，查询侧用 CAST(... AS REAL) 做数值运算 ——
            # 避免「1.2%」「130/85」这类脏值让整列导入失败
            ddl_cols = ", ".join(f'"{c}" TEXT' for c in cols)
            con.execute(f'DROP TABLE IF EXISTS "{table}"')
            con.execute(f'CREATE TABLE "{table}" ({ddl_cols})')
            placeholders = ", ".join("?" for _ in cols)
            qcols = ", ".join(f'"{c}"' for c in cols)
            con.executemany(
                f'INSERT INTO "{table}" ({qcols}) VALUES ({placeholders})',
                [[_cell(r.get(c)) for c in cols] for r in rows],
            )
            _write_meta(con, table, preview)
            con.commit()
        finally:
            con.close()
        preview.table = table
        return table
    except Exception as exc:
        # 落地失败必须留痕：静默 return "" 会让上层以为「没有可查询表」，
        # 进而回退到内置库 —— 那正是「传了 A 却分析 B」的故障起点。
        import logging
        logging.getLogger(__name__).warning(
            "materialize_table 落地失败 session=%s table=%s: %s: %s",
            sid, table, type(exc).__name__, exc,
        )
        return ""


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, float)):
        return v if not isinstance(v, str) else v.strip()
    return json.dumps(v, ensure_ascii=False, default=str)


def attached_tables(session_id: str | None) -> list[dict[str, Any]]:
    """列出该 session 边车库里的表（供 schema_search 合并）。

    ATTACH/02：内存缓存未命中（重启 / 换 worker / 独立进程）时，回退到
    **反射 sidecar 文件**，保证「SQL 能挂上表」与「schema 能发现表」永远一致。
    """
    items = get_attachment_store().get(session_id)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in items.values():
        if p.kind == "table" and p.table:
            seen.add(p.table)
            out.append({
                "table": p.table,
                "source_file": p.name,
                "row_count": p.rows,
                "columns": [{"name": c, "type": "TEXT"} for c in p.columns],
                "origin": "user_upload",
            })
    out += _reflect_sidecar(session_id, skip=seen)
    return out


def _reflect_sidecar(session_id: str | None, skip: set[str] | None = None) -> list[dict[str, Any]]:
    """从边车库文件直接反射表结构（跨进程可靠）。失败一律返回空列表。"""
    path = sidecar_path(session_id)
    if not path.exists():
        return []
    skip = skip or set()
    meta = _read_meta(path)
    out: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            for (tname,) in rows:
                if tname in skip or tname == META_TABLE or tname.startswith("_"):
                    continue
                cols = [c[1] for c in con.execute(f'PRAGMA table_info("{tname}")').fetchall()]
                try:
                    _r = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()
                    n = int(_r[0]) if _r else 0
                except Exception:
                    n = 0
                m = meta.get(tname, {})
                src_cols = m.get("columns") or cols
                out.append({
                    "table": tname,
                    "source_file": m.get("source_file") or f"{tname}.csv",
                    "row_count": int(n),
                    "columns": [{"name": c, "type": "TEXT"} for c in src_cols],
                    "origin": "user_upload",
                })
        finally:
            con.close()
    except Exception:
        return out
    return out


def _read_meta(path: Path) -> dict[str, dict[str, Any]]:
    """读取边车库里的 ``_meta`` 表（源文件名 / 原始列序）。缺失则返回空。"""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(f'SELECT table_name, payload FROM "{META_TABLE}"').fetchall()
        finally:
            con.close()
        out: dict[str, dict[str, Any]] = {}
        for tname, payload in rows:
            try:
                out[tname] = json.loads(payload)
            except Exception:
                out[tname] = {}
        return out
    except Exception:
        return {}


def _write_meta(con: sqlite3.Connection, table: str, preview: DatasetPreview) -> None:
    """把「源文件名 + 原始列序」写入 ``_meta``，供跨进程反射还原。"""
    try:
        con.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" '
            "(table_name TEXT PRIMARY KEY, payload TEXT)"
        )
        payload = json.dumps(
            {"source_file": preview.name, "columns": list(preview.columns)},
            ensure_ascii=False,
        )
        con.execute(
            f'INSERT INTO "{META_TABLE}" (table_name, payload) VALUES (?, ?) '
            "ON CONFLICT(table_name) DO UPDATE SET payload=excluded.payload",
            (table, payload),
        )
    except Exception:
        pass  # 元数据是锦上添花，绝不阻断物化


# --------------------------------------------------------------------------- #
# P2-2：图片附件落地与发现
# --------------------------------------------------------------------------- #

def image_dir(session_id: str | None) -> Path:
    """该 session 的图片落地目录（不保证存在）。"""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", (session_id or "default"))[:60] or "default"
    return (UPLOAD_DIR / sid / "images").resolve()


def _safe_filename(name: str) -> str:
    """把上传文件名转成可安全落盘的文件名（去路径分隔与非常规字符，保留扩展名）。"""
    stem = Path(name).stem
    suffix = Path(name).suffix
    ident = re.sub(r"[^A-Za-z0-9_.\-]", "_", stem).strip("_")
    ident = ident[:80] or "image"
    return ident + suffix.lower()


def attached_images(session_id: str | None) -> list[dict[str, Any]]:
    """列出该 session 已上传的图片附件（供 Planner 感知多模态输入）。

    返回 ``{name, bytes, path}``；与 ``attached_tables`` 平行，但图片不走边车库，
    而是直接落盘原图、由 ``image_analyze`` 工具读取。
    """
    items = get_attachment_store().get(session_id)
    out: list[dict[str, Any]] = []
    for p in items.values():
        if p.kind == "image":
            out.append({"name": p.name, "bytes": p.bytes, "path": p.path})
    return out


def attach_clause(session_id: str | None) -> str | None:
    """返回可直接执行的 ``ATTACH DATABASE '...' AS upload`` 语句，或 None。

    **安全**：路径来自服务端按 session_id 计算的 ``sidecar_path()``，不含用户输入，
    且 session_id 已经过字符白名单过滤；单引号再转义一次以杜绝注入。

    **注意**：SQLite 的 ``ATTACH`` 不接受绑定参数（``?``），只能字面量拼接 ——
    这也是为什么这里返回完整语句而不是参数对。
    """
    path = sidecar_path(session_id)
    if not path.exists():
        return None
    literal = str(path).replace("'", "''")
    return f"ATTACH DATABASE '{literal}' AS upload"


# ---------------------------------------------------------------- 解析

def parse_table(raw: bytes, filename: str) -> DatasetPreview:
    """把 CSV / TSV / JSON 解析为 DatasetPreview（不依赖 pandas）。"""
    suffix = Path(filename).suffix.lower()
    text = _decode(raw)

    if suffix in {".json", ".jsonl"}:
        return _parse_json(text, filename, len(raw))

    # CSV / TSV / xlsx（xlsx 走 pandas 可选路径）
    if suffix in {".xlsx", ".xls"}:
        prev = _parse_excel(raw, filename)
        if prev is not None:
            return prev
        # 没有 pandas/openpyxl —— 当二进制文本处理会乱码，直接给提示
        return DatasetPreview(
            name=filename,
            kind="text",
            bytes=len(raw),
            excerpt=f"[未能解析 Excel：当前环境缺少 pandas/openpyxl。文件大小 {len(raw)} 字节]",
        )

    delimiter = "\t" if suffix == ".tsv" else ","
    return _parse_delimited(text, filename, delimiter, len(raw))


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _parse_delimited(
    text: str, filename: str, delimiter: str, nbytes: int
) -> DatasetPreview:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return DatasetPreview(name=filename, kind="table", bytes=nbytes)
    header = [h.strip() for h in rows[0]]
    body = rows[1:]

    def _rec(r: list[str]) -> dict[str, Any]:
        return {header[i] if i < len(header) else f"c{i}": (r[i] if i < len(r) else "")
                for i in range(len(header))}

    sample = [_rec(r) for r in body[:20]]
    return DatasetPreview(
        name=filename,
        kind="table",
        rows=len(body),
        columns=header,
        sample=sample,
        bytes=nbytes,
        all_rows=[_rec(r) for r in body],
    )


def _parse_json(text: str, filename: str, nbytes: int) -> DatasetPreview:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # JSONL
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            records = [json.loads(ln) for ln in lines[:500]]
        except json.JSONDecodeError:
            return DatasetPreview(
                name=filename, kind="text", bytes=nbytes, excerpt=text[:800]
            )
        return _records_to_preview(records, filename, nbytes)

    if isinstance(obj, list):
        return _records_to_preview(obj[:500], filename, nbytes)
    if isinstance(obj, dict):
        return _records_to_preview([obj], filename, nbytes)
    return DatasetPreview(name=filename, kind="text", bytes=nbytes, excerpt=text[:800])


def _records_to_preview(
    records: list[Any], filename: str, nbytes: int
) -> DatasetPreview:
    dicts = [r for r in records if isinstance(r, dict)]
    if not dicts:
        return DatasetPreview(
            name=filename,
            kind="table",
            rows=len(records),
            columns=["value"],
            sample=[{"value": r} for r in records[:20]],
            all_rows=[{"value": r} for r in records],
            bytes=nbytes,
        )
    cols: list[str] = []
    for r in dicts:
        for k in r:
            if k not in cols:
                cols.append(k)
    return DatasetPreview(
        name=filename,
        kind="table",
        rows=len(dicts),
        columns=cols,
        sample=dicts[:20],
        all_rows=dicts,
        bytes=nbytes,
    )


def _parse_excel(raw: bytes, filename: str) -> DatasetPreview | None:
    """尝试用 pandas 读取 Excel；环境缺依赖时返回 None。"""
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        return None
    try:
        df = pd.read_excel(io.BytesIO(raw))
    except Exception:
        return None
    df = df.fillna("")
    return DatasetPreview(
        name=filename,
        kind="table",
        rows=int(len(df)),
        columns=[str(c) for c in df.columns],
        sample=df.head(20).to_dict(orient="records"),
        bytes=len(raw),
    )


def parse_text_like(raw: bytes, filename: str) -> DatasetPreview:
    """md / txt / pdf / 代码等文本类附件。"""
    suffix = Path(filename).suffix.lower()
    text = ""
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io as _io

            text = "\n".join(
                page.extract_text() or ""
                for page in PdfReader(_io.BytesIO(raw)).pages
            )
        except Exception:
            text = ""
    if not text:
        text = _decode(raw)
    return DatasetPreview(
        name=filename,
        kind="text",
        bytes=len(raw),
        excerpt=text[:2000],
    )


TABLE_SUFFIXES = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}


def build_preview(raw: bytes, filename: str) -> DatasetPreview:
    suffix = Path(filename).suffix.lower()
    if suffix in TABLE_SUFFIXES:
        return parse_table(raw, filename)
    if is_image(filename):
        # P2-2：图片不解析内容（内容需经视觉模型识别），只登记元信息与字节数；
        # 落地原图由上传路由负责（它持有 session_id，知道存哪）。
        return DatasetPreview(name=filename, kind="image", bytes=len(raw))
    return parse_text_like(raw, filename)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_SUFFIXES
