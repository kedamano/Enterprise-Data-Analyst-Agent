"""``dataset_profile`` tool – data-quality profiling for a table **or a query result**.

E4/01 质量基元（spec: docs/specs/E4/01-profile-quality.md）——回答分析师的四个前置问题，
它们正是"结论翻车"的常见来源：

1. **主键唯不唯一**（重复计数）
2. **join 有没有放大**（放大后求和）
3. **一行是什么粒度**（粒度误读）
4. **日期连不连续**（稀疏日期当连续）

安全：`table` / `key` / `base_table` / `date_column` 全部走标识符白名单，**绝不拼进 SQL**；
`sql` 复用 `sql_tool.guard_readonly_sql()`（只读、单语句）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Callable, Optional

from ...config import get_settings
from .dbguard import data_source_error
from .sql_tool import guard_readonly_sql

# 标识符白名单：只允许字母/数字/下划线（表名与列名都过这一关）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 像业务键的候选：xxx_id / id / xxx_key / xxx_no
_ID_LIKE_RE = re.compile(r"(^id$|_id$|^key$|_key$|_no$)", re.IGNORECASE)
# 日期列候选名
_DATE_NAME_RE = re.compile(r"(date|time|日期|时间|month|_dt)", re.IGNORECASE)

# join 放大阈值：结果行数 / 基表行数 ≥ 此值即判放大
_DEFAULT_AMP_THRESHOLD = 1.5
_GAP_SAMPLES = 3


def _bad_ident(name: str) -> bool:
    return not bool(_IDENT_RE.match(name or ""))


def _dialect_of(conn) -> str:
    """连接自己的方言（比 `DATA_DB_DIALECT` 可信：那是配置，这是事实）。"""
    try:
        return str(conn.engine.dialect.name or "sqlite").lower()
    except Exception:
        return "sqlite"


def _q(name: str, dialect: str = "sqlite") -> str:
    """标识符引用（**方言感知**）。

    ⚠️ ANSI 双引号**不是**通用写法：MySQL 默认 sql_mode 下 `"..."` 是**字符串字面量**
    而非标识符引号（除非开了 ANSI_QUOTES），`SELECT COUNT(*) FROM "bench_fact"` 直接
    ERROR 1064 语法错误。MySQL 必须用反引号。
    此前这里硬编码双引号 → `dataset_profile` 在 MySQL 上**必然失败**
    （SQLite/PG 都接受双引号，所以样例库与 PG live 一直是绿的）。
    见 tests/test_profile_cross_dialect.py。
    """
    q = "`" if str(dialect or "sqlite").lower() == "mysql" else '"'
    return f"{q}{name}{q}"


def _q_of(conn) -> Callable[[str], str]:
    """绑定到某个连接方言的引用器。

    各 helper 在函数内 `_q = _q_of(conn)` **遮蔽**模块级 `_q`，调用点无需改动。
    刻意**不**用模块级可变全局记录"当前方言"：并行执行器是裸 `ThreadPoolExecutor`
    （见 nodes.py），`ContextVar` 不会自动传播到 worker 线程；两个不同方言的
    `dataset_profile` 并发时会互相串掉引号 → 拼出方言不匹配的 SQL。
    """
    return partial(_q, dialect=_dialect_of(conn))


def _table_columns(conn, text, table: str, dialect: str) -> list[str]:
    """按方言读取表的列名（保持库内顺序）。

    两个**只在真库上才暴露**的跨方言坑（都源于 SQLAlchemy ``Row`` / information_schema）：

    ① ``text()`` 查询返回的 ``Row`` **不支持字符串下标**：``r["column_name"]`` 抛
       ``TypeError: tuple indices must be integers or slices, not str``。
    ② 即便改用 ``r._mapping["column_name"]``，**MySQL 的 information_schema 列名是
       大写 ``COLUMN_NAME``**（PG 是小写 ``column_name``），而 ``Row._mapping`` 下标
       **区分大小写** → MySQL 抛 ``Could not locate column in row for column
       'column_name'``。

    这里按**位置**取值（``SELECT`` 只此一列）彻底规避标识符大小写折叠差异。
    SQLite 走 ``PRAGMA table_info`` 分支，所以内置样例库长期是绿的、
    以上两坑要到接真库才炸。见 tests/test_profile_cross_dialect.py。
    """
    if dialect == "sqlite":
        # PRAGMA table_info 行结构：(cid, name, type, notnull, dflt_value, pk)
        return [r[1] for r in conn.execute(
            text(f"PRAGMA table_info({table})")).fetchall()]
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns "
             "WHERE table_name = :t ORDER BY ordinal_position"),
        {"t": table}).fetchall()
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:  # 同名表可能存在于多个 schema → 按 ordinal_position 去重
        name = str(r[0])
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def _to_date(value: Any) -> Optional[date]:
    """把聚合回来的 min/max 解析为 date（sqlite 返回字符串，其他方言可能返回 datetime）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_batch(value: Any) -> int:
    """批量大小归一：配置写坏（0/负数/None/非数）→ 退化为 1（逐列，仍然正确）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _column_stats(conn, text, source: str, columns: list[str], batch: Any,
                  total: Optional[int] = None) -> dict[str, dict]:
    """逐列 null/distinct —— **按批合并聚合**，而不是每列一条查询。

    原实现每列发一条 `SELECT COUNT(c), COUNT(DISTINCT c) FROM src`：
    N 列 = **N 次全表扫描**，随**列数**线性恶化（实测 1M×9 列 SQLite 2.87s /
    PG 3.68s / MySQL 12.57s；100 列外推 ≈22s）。

    改为把同一批列合进一条 SQL 的多个聚合：

        SELECT COUNT(c1), COUNT(DISTINCT c1), COUNT(c2), COUNT(DISTINCT c2) FROM src

    → N 列只需 `ceil(N/batch)` 次扫描。**结果完全等价**（非抽样、非近似去重、
    不改语义），因此输出不需要标 `sampled`，也不会把"非唯一列在抽样下看着唯一"
    这种假信号带进 `_key_uniqueness`（它的判据正是 ``distinct == row_count``）。
    """
    _q = _q_of(conn)   # 绑定本连接方言（线程安全）
    out: dict[str, dict] = {}
    if not columns:
        return out
    if total is None:
        total = int(conn.execute(text(f"SELECT COUNT(*) FROM {source}")).scalar() or 0)
    step = _normalize_batch(batch)
    for i in range(0, len(columns), step):
        chunk = columns[i:i + step]
        select = ", ".join(f"COUNT({_q(c)}), COUNT(DISTINCT {_q(c)})" for c in chunk)
        row = conn.execute(text(f"SELECT {select} FROM {source}")).fetchone()
        for j, col in enumerate(chunk):
            non_null = int(row[2 * j] or 0)
            distinct = int(row[2 * j + 1] or 0)
            nulls = total - non_null
            out[col] = {
                "null_count": nulls,
                "null_ratio": round(nulls / total, 4) if total else 0.0,
                "distinct": distinct,
            }
    return out


def _prioritize_columns(columns: list[str], keys: list[str], date_column: str) -> list[str]:
    """画像列的**优先级排序**（供超宽表截断时"先保有用的"）。

    顺序：声明的键 > 声明的日期列 > 名似主键 > 名似日期 > 其余（保持库内顺序）。
    组内保持原序；**不丢列、不重复**（去重但保留首次出现的位置语义）。
    """
    seen: set[str] = set()
    def _uniq(seq):
        out = []
        for c in seq:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    declared_keys = [k for k in keys if k in columns]
    declared_date = [date_column] if date_column and date_column in columns else []
    id_like = [c for c in columns if _ID_LIKE_RE.search(c)]
    date_like = [c for c in columns if _DATE_NAME_RE.search(c)]
    return (_uniq(declared_keys) + _uniq(declared_date) + _uniq(id_like)
            + _uniq(date_like) + _uniq(columns))


def run(params: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    src_err = data_source_error()
    if src_err:  # 缺库必须响亮失败，绝不静默创建空 SQLite 文件
        return {"ok": False, "error": src_err}

    table = (params.get("table") or "").strip()
    sql = (params.get("sql") or "").strip()
    if not table and not sql:
        return {"ok": False, "error": "缺少 table 或 sql 参数（二选一）"}
    if table and _bad_ident(table):
        return {"ok": False, "error": f"非法标识符: {table!r}（只允许字母/数字/下划线）"}

    keys = _as_list(params.get("key"))
    for col in keys:
        if _bad_ident(col):
            return {"ok": False, "error": f"非法标识符: {col!r}（只允许字母/数字/下划线）"}
    base_table = (params.get("base_table") or "").strip()
    if base_table and _bad_ident(base_table):
        return {"ok": False, "error": f"非法标识符: {base_table!r}（只允许字母/数字/下划线）"}
    date_column = (params.get("date_column") or "").strip()
    if date_column and _bad_ident(date_column):
        return {"ok": False, "error": f"非法标识符: {date_column!r}（只允许字母/数字/下划线）"}

    if sql:
        guard_err = guard_readonly_sql(sql)
        if guard_err:
            return {"ok": False, "error": guard_err}
        source = f"({sql.rstrip().rstrip(';')}) AS _src"   # 画像查询结果集（只读、单语句）
    else:
        source = ""   # 引擎建好、方言确定后再引用表名（见下方 _q = _q_of(engine)）

    try:
        from sqlalchemy import create_engine, text
    except ImportError as exc:  # pragma: no cover
        return {"ok": False, "error": f"sqlalchemy 未安装: {exc}"}

    # ATTACH/01：上传的表格类附件落在 session 边车库（upload.<表>），不在内置企业库。
    # dataset_profile 原本只查内置库 → 对上传表报 no such table。这里先判是否命中上传表，
    # 命中则直接连边车库文件（表在该库内就是裸名，upload. 前缀仅跨库 ATTACH 时存在）。
    engine = None
    if table and not sql:
        try:
            from ..attachments import attached_tables, sidecar_path

            sid = params.get("_session_id")
            uploaded = attached_tables(sid)
            if any(t.get("table") == table for t in uploaded):
                sp = sidecar_path(sid)
                if sp.exists():
                    engine = create_engine(f"sqlite:///{sp}?timeout=10")
        except Exception:
            engine = None
    if engine is None:
        try:
            from .datasource import resolve_source

            db_url, _dialect = resolve_source(params.get("source"))
        except KeyError as exc:
            return {"ok": False, "error": str(exc).strip("'")}
        engine = create_engine(db_url, pool_pre_ping=True)

    # 方言确定后才谈得上“正确的标识符引号”（MySQL 反引号 / 其余双引号）。
    # 直接问 engine 自己最可靠：不依赖 DATA_DB_DIALECT 有没有被正确配置。
    # 用**局部**遮蔽模块级 `_q`（线程安全；见 _q_of 的说明）。
    _q = _q_of(engine)
    if not sql:
        source = _q(table)

    # --- 列名 ---
    columns: list[str] = []
    if sql:
        try:
            with engine.connect() as conn:
                probe = conn.execute(text(f"SELECT * FROM {source} LIMIT 0"))
                columns = [str(c) for c in probe.keys()]
        except Exception as exc:
            return {"ok": False, "error": f"无法执行被画像的 SQL: {exc}"}
    else:
        try:
            with engine.connect() as conn:
                columns = _table_columns(conn, text, table, _dialect_of(conn))
        except Exception as exc:
            return {"ok": False, "error": f"无法读取表结构: {exc}"}

    # --- 宽表上界：按优先级截断被画像的列 ---
    # 实测：批量化只省"重复扫表"（PG 1M×9 列 2.06s→1.52s，1.35×），**省不掉每列
    # COUNT(DISTINCT) 的去重开销** → 列数因子依然线性。故宽表必须封顶列数。
    # 声明的键/日期列被优先保全，否则 key_uniqueness / date_continuity 会失效。
    cap = int(getattr(settings, "profile_max_columns", 0) or 0)
    if cap > 0 and len(columns) > cap:
        # 仅**真正截断时**才重排：否则会改变 `columns` 的键序，
        # 破坏"画像保持库内列序"这一既有契约（被 test_profile_cross_dialect 钉住）。
        profiled = _prioritize_columns(columns, keys, date_column)[:cap]
    else:
        profiled = list(columns)
    skipped = [c for c in columns if c not in set(profiled)]

    # --- 行数与逐列 null/distinct ---
    profile: dict[str, Any] = {"table": table or None, "sql": sql or None, "columns": {},
                               "columns_total": len(columns), "columns_skipped": skipped}
    try:
        with engine.connect() as conn:
            total = conn.execute(text(f"SELECT COUNT(*) FROM {source}")).scalar() or 0
            profile["row_count"] = total
            # 按批合并聚合（原为每列一条查询 → N 次全表扫描，见 _column_stats）
            profile["columns"] = _column_stats(
                conn, text, source, profiled, settings.profile_column_batch, total)
    except Exception as exc:
        return {"ok": False, "error": f"画像失败: {exc}"}

    # --- E4/01 质量基元 ---
    try:
        with engine.connect() as conn:
            profile["key_uniqueness"] = _key_uniqueness(conn, text, source, total,
                                                        profile["columns"], keys)
            profile["join_amplification"] = _join_amplification(conn, text, total, base_table,
                                                               settings)
            profile["date_continuity"] = _date_continuity(conn, text, source, total,
                                                          profile["columns"], date_column)
            profile["enums"], profile["enums_skipped"] = _enums(
                conn, text, source, profile["columns"], settings)
    except Exception as exc:
        return {"ok": False, "error": f"质量基元计算失败: {exc}"}

    return {"ok": True, **profile}


def _enums(conn, text, source: str, columns: dict, settings) -> tuple[dict, list[str]]:
    """SEMANTIC/01：低基数文本列的取值枚举（供"业务词→值"的映射）。

    **跳过疑似 PII 列**：枚举值本身是数据，是 E4/02（脱敏）之外的新出口，
    而 E4/02 尚未实现 —— 这里必须自带跳过，并把跳过的列记入 ``enums_skipped`` 供审计。
    """
    _q = _q_of(conn)   # 绑定本连接方言（线程安全；不依赖模块级全局）
    try:
        from ...core.semantics import is_pii_column  # app.core.tools → app.core
    except Exception:
        # **失败即关闭**：检测不出 PII 时宁可一条枚举都不采，也不把可能是
        # 手机号/姓名的值送进上下文（此前这里静默 return False，等于关掉了隐私保护）
        return {}, list((columns or {}).keys())

    cap = int(getattr(settings, "profile_enum_max_cardinality", 20) or 20)
    out: dict[str, list] = {}
    skipped: list[str] = []
    for col, meta in (columns or {}).items():
        if is_pii_column(col):
            skipped.append(col)
            continue
        distinct = meta.get("distinct") or 0
        if not distinct or distinct > cap:
            continue
        try:
            rows = conn.execute(text(
                f"SELECT DISTINCT {_q(col)} FROM {source} "
                f"WHERE {_q(col)} IS NOT NULL LIMIT {cap + 1}")).fetchall()
        except Exception:
            continue  # 单列失败不影响整体画像
        vals = [r[0] for r in rows]
        if vals and len(vals) <= cap:
            out[col] = vals
    return out, skipped

def _key_uniqueness(conn, text, source: str, total: int, columns: dict, keys: list[str]) -> dict:
    """主键唯一性 / 粒度。空表 → 全部未知（不能把"0 行"误判成"每列都唯一"）。"""
    _q = _q_of(conn)   # 绑定本连接方言（线程安全）
    if total <= 0:
        return {"candidate_keys": [], "likely_key": None, "declared_key": list(keys),
                "is_unique": None, "duplicate_rows": None, "duplicate_ratio": None,
                "grain": "unknown"}

    candidate = [c for c, meta in columns.items()
                 if meta["null_count"] == 0 and meta["distinct"] == total]
    likely = next((c for c in candidate if _ID_LIKE_RE.search(c)), None)
    declared = list(keys) if keys else ([likely] if likely else [])

    is_unique: Optional[bool] = None
    duplicate_rows: Optional[int] = None
    duplicate_ratio: Optional[float] = None
    if declared:
        if len(declared) == 1:
            distinct_key = conn.execute(
                text(f"SELECT COUNT(DISTINCT {_q(declared[0])}) FROM {source}")).scalar() or 0
        else:  # 联合键：子查询计数，避开 || 拼接的类型陷阱
            cols = ", ".join(_q(c) for c in declared)
            distinct_key = conn.execute(
                text(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {source}) AS _k")
            ).scalar() or 0
        duplicate_rows = total - distinct_key
        is_unique = duplicate_rows == 0
        duplicate_ratio = round(duplicate_rows / total, 4)

    return {"candidate_keys": candidate, "likely_key": likely, "declared_key": declared,
            "is_unique": is_unique, "duplicate_rows": duplicate_rows,
            "duplicate_ratio": duplicate_ratio,
            # 粒度看**能不能指出一个键**，而不是"有没有唯一列"：分组结果的度量列
            # （如 20 组恰好全不同的 SUM(revenue)）会误报成行粒度，见 spec §3。
            "grain": "row" if declared else "aggregated"}


def _join_amplification(conn, text, result_rows: int, base_table: str, settings) -> Optional[dict]:
    """结果行数 / 基表行数。只在给了 base_table 时判定（否则无对照基准）。"""
    _q = _q_of(conn)   # 绑定本连接方言（线程安全）
    if not base_table:
        return None
    base_rows = conn.execute(text(f"SELECT COUNT(*) FROM {_q(base_table)}")).scalar() or 0
    threshold = float(settings.profile_join_amp_threshold or _DEFAULT_AMP_THRESHOLD)
    factor = round(result_rows / base_rows, 4) if base_rows else None
    return {"base_table": base_table, "base_rows": base_rows, "result_rows": result_rows,
            "factor": factor, "amplified": bool(factor and factor >= threshold),
            "threshold": threshold}


def _date_continuity(conn, text, source: str, total: int, columns: dict,
                     date_column: str) -> Optional[dict]:
    """日期连续性：应有天数 vs 实际天数 → 稀疏提示 + 断档样例。"""
    _q = _q_of(conn)   # 绑定本连接方言（线程安全）
    col = date_column or next((c for c in columns if _DATE_NAME_RE.search(c)), "")
    if not col or col not in columns:
        return None
    lo_raw, hi_raw, distinct_days = conn.execute(
        text(f"SELECT MIN({_q(col)}), MAX({_q(col)}), COUNT(DISTINCT {_q(col)}) FROM {source}")
    ).fetchone()
    lo, hi = _to_date(lo_raw), _to_date(hi_raw)
    if lo is None or hi is None or hi < lo:
        return None
    expected = (hi - lo).days + 1
    distinct_days = int(distinct_days or 0)
    missing = max(0, expected - distinct_days)
    coverage = round(distinct_days / expected, 4) if expected else 0.0

    gaps: list[str] = []
    if missing:
        present = {str(r[0])[:10] for r in conn.execute(
            text(f"SELECT DISTINCT {_q(col)} FROM {source}")).fetchall()}
        cursor = lo
        while cursor <= hi and len(gaps) < _GAP_SAMPLES:
            if cursor.isoformat() not in present:
                gaps.append(cursor.isoformat())
            cursor += timedelta(days=1)

    return {"column": col, "min": lo.isoformat(), "max": hi.isoformat(),
            "distinct_days": distinct_days, "expected_days": expected, "missing_days": missing,
            "coverage_ratio": coverage, "sparse": coverage < 0.9, "gap_samples": gaps}
