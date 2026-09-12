"""``sql_query`` tool – read-only SQL execution against the enterprise data store.

Safety:
* When ``sql_readonly`` is enabled (default) any DML/DDL keyword aborts the call.
* **文件读写 / 存储过程 / 锁表 / DoS 原语一律拒绝**（与 ``sql_readonly`` 无关）：
  ``INTO OUTFILE``·``LOAD_FILE`` 能读写**服务器文件系统**，``CALL`` 可任意写，
  ``SLEEP``/``BENCHMARK`` 可耗尽资源——这些都不是"查询数据"，必须在工具边界拦死。
* 匹配前先**去注释 + 折叠空白**，防止 ``INTO/*x*/OUTFILE`` 这类拆分绕过；
  MySQL 可执行注释 ``/*!...*/`` 直接拒绝（其内容会被真实执行）。
* Only a single statement is permitted.
* Result rows are capped to ``sql_max_rows``.
* Uses SQLAlchemy so the same code works across sqlite / postgres / mysql.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from sqlalchemy import event

from ...config import get_settings
from .dbguard import data_source_error

_FORBIDDEN = (
    r"\b(DROP|DELETE|UPDATE|INSERT|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|MERGE|REPLACE|ATTACH|PRAGMA)\b"
)
_FORBIDDEN_RE = re.compile(_FORBIDDEN, re.IGNORECASE)
_MULTI_STMT_RE = re.compile(r";\s*\S")

# 表达式位置的文件/DoS 原语：只要出现就拒（不受 sql_readonly 开关影响）
_SIDE_EFFECT_EXPR_RE = re.compile(
    r"("
    r"\bINTO\s+(OUTFILE|DUMPFILE)\b"      # MySQL：结果写服务器文件（任意文件写）
    r"|\bLOAD_FILE\s*\("                  # MySQL：读服务器任意文件
    r"|\bSLEEP\s*\("                      # 资源耗尽
    r"|\bBENCHMARK\s*\("                  # 资源耗尽
    r"|\bINTO\s+@"                        # 变量赋值
    r")",
    re.IGNORECASE,
)
# **语句首**的副作用语句（放在语句首匹配，避免误杀名为 call/handler 的列）
_SIDE_EFFECT_STMT_RE = re.compile(
    r"^("
    r"CALL\b"                             # 存储过程：可任意写
    r"|EXECUTE\b"                         # 预处理语句执行
    r"|HANDLER\b"                         # MySQL：绕过 SQL 层直接读
    r"|(UN)?LOCK\s+TABLES?\b"             # 锁表：阻塞他人
    r"|LOAD\s+DATA\b"                     # MySQL：批量导入
    r"|SET\s+(GLOBAL|SESSION|LOCAL|NAMES|PASSWORD|TRANSACTION|AUTOCOMMIT|@)"
    r")",
    re.IGNORECASE,
)
# MySQL 版本注释：`/*!50000 ... */` 的内容会被服务端**真实执行**，直接拒绝
_MYSQL_EXEC_COMMENT_RE = re.compile(r"/\*!")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"(?:--[^\n]*)|(?:^|\s)#[^\n]*")
_WS_RE = re.compile(r"\s+")

# 语句级超时上限（§22 ToolSpec.timeout_s 同源；防止 LLM 传入超大值）
_MAX_TIMEOUT_S = 120


def _normalize_sql(sql: str) -> str:
    """去注释 + 折叠空白后的文本，仅用于**模式匹配**（不用于执行）。

    ``INTO/*x*/OUTFILE``、``INTO \\n OUTFILE`` 这类拆分写法会让朴素正则漏检，
    归一化后统一按"单词流"匹配。
    """
    s = _BLOCK_COMMENT_RE.sub(" ", sql)
    s = _LINE_COMMENT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def guard_readonly_sql(sql: str) -> str | None:
    """只读守卫（E4/01 抽出共享）：返回错误信息，或 None 表示放行。

    ``dataset_profile`` 画像 SQL 结果集时复用同一守卫，避免两处规则漂移。
    """
    # MySQL 可执行注释优先拦（其内容会被真实执行，任何开关下都不放行）
    if _MYSQL_EXEC_COMMENT_RE.search(sql):
        return "只读模式禁止执行（检测到 MySQL 可执行注释 /*!...*/）"

    norm = _normalize_sql(sql)
    if get_settings().sql_readonly and _FORBIDDEN_RE.search(norm):
        return "只读模式禁止写操作 (DROP/DELETE/UPDATE/INSERT/...)"
    if _SIDE_EFFECT_EXPR_RE.search(norm) or _SIDE_EFFECT_STMT_RE.search(norm):
        return ("只读模式禁止文件读写/存储过程/锁表等副作用语句 "
                "(INTO OUTFILE/LOAD_FILE/LOAD DATA/CALL/EXECUTE/HANDLER/LOCK TABLES/SLEEP/...)")
    if _MULTI_STMT_RE.search(norm):
        return "一次仅允许执行单条语句"
    return None


def run(params: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    sql = (params.get("sql") or "").strip()
    if not sql:
        return {"ok": False, "error": "缺少 sql 参数", "rows": []}

    src_err = data_source_error()
    if src_err:  # 缺库必须响亮失败，绝不静默创建空 SQLite 文件
        return {"ok": False, "error": src_err, "rows": []}

    guard_err = guard_readonly_sql(sql)
    if guard_err:
        return {"ok": False, "error": guard_err, "rows": []}

    # AUTH/01：用户级数据权限（表白名单 / 列黑名单 / 行过滤）——在**执行前**确定性拦下
    try:
        from ..security.auth import current_principal
        from ..security.data_guard import apply_row_filters, guard_sql

        principal = current_principal()
        perm_err = guard_sql(sql, principal)
        if perm_err:
            return {"ok": False, "error": perm_err, "error_class": "NON_RETRYABLE", "rows": []}
        sql, _filtered = apply_row_filters(sql, principal)
    except Exception:
        pass  # 鉴权层故障不得让查询崩掉（guard_sql 内部已 fail-open 于匿名）

    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:  # pragma: no cover
        return {"ok": False, "error": f"sqlalchemy 未安装: {exc}", "rows": []}

    # §22/§23：语句级超时（默认 30s，对应 ToolSpec.timeout_s；归 RETRYABLE）
    timeout_s = int(params.get("timeout_seconds", 30) or 30)
    # E7/01：按命名源取 DSN（不传 = 主源，行为不变）；未知源给出可读错误
    try:
        from .datasource import resolve_source

        db_url, _dialect = resolve_source(params.get("source"))
    except KeyError as exc:
        return {"ok": False, "error": str(exc).strip("'"), "rows": []}
    engine = _build_engine(db_url, timeout_s)

    # ATTACH/01：若该 session 有用户上传的附件表，把边车库挂到连接的 ``upload`` 库。
    # 注意：ATTACH 是 DDL 类语句，会触发只读守卫，所以**不能**拼进用户的 SQL；
    # 改为在连接建立后用独立语句执行 —— 用户 SQL 仍然走完整守卫。
    attach_sql = ""
    try:
        from ..attachments import attach_clause
        attach_sql = attach_clause(params.get("_session_id")) or ""
    except Exception:
        attach_sql = ""

    limit = settings.sql_max_rows
    lowered = sql.rstrip().rstrip(";").lower()
    if "limit" not in lowered:
        sql = f"{sql.rstrip().rstrip(';')} LIMIT {limit}"

    started = time.time()
    try:
        with engine.connect() as conn:
            # 内部 attach：仅挂载边车库，语句由服务端构造（非用户输入），安全；
            # 用户 SQL 随后照常执行（守卫已在前面把关）
            if attach_sql:
                try:
                    conn.execute(text(attach_sql))
                except Exception:
                    pass  # 已挂载或路径不可用都不应阻断主查询
            result = conn.execute(text(sql))
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
        return {
            "ok": True,
            "columns": columns,
            "row_count": len(rows),
            "rows": rows,
            "execution_time_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:  # surface DB errors to the Analyst, do not crash
        msg = str(exc)
        # progress handler 中止会被 SQLAlchemy 包装成 "interrupted"——还原超时语义，
        # 使错误分类器正确归为 RETRYABLE（§23: SQL Timeout → Retry）
        if "interrupted" in msg.lower():
            return {"ok": False, "error": f"查询超时 (statement timeout {timeout_s}s)", "rows": []}
        return {"ok": False, "error": msg, "rows": []}


def _build_engine(url: str, timeout_s: int):
    """Create the engine with a *statement-level* timeout enforced.

    * SQLite: a progress handler aborts the query once the deadline passes
      (SQLite has no native query timeout).
    * PostgreSQL: ``statement_timeout`` via connection options.
    """
    timeout = max(1, min(int(timeout_s), _MAX_TIMEOUT_S))
    from sqlalchemy import create_engine
    if url.startswith("sqlite"):
        engine = create_engine(url, pool_pre_ping=True)

        @event.listens_for(engine, "connect")
        def _install_progress_handler(dbapi_conn, _record):  # noqa: ANN001
            deadline = time.monotonic() + timeout

            def _check() -> None:
                if time.monotonic() > deadline:
                    raise sqlite3.OperationalError(f"查询超时 (statement timeout {timeout}s)")

            # 每 5000 个 VM 指令检查一次，开销可忽略
            dbapi_conn.set_progress_handler(_check, 5000)
        return engine
    # PostgreSQL / 其他方言
    connect_args = {"options": f"-c statement_timeout={timeout * 1000}"} \
        if url.startswith("postgres") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)
