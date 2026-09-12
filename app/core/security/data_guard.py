"""AUTH/01 数据权限的三个确定性闸门：表级 / 列级 / 行级。

Spec: docs/specs/AUTH/01-user-auth-and-data-permissions.md §3

全部在**工具执行前**判定，不经过 LLM；用户 SQL 无法绕过（行级过滤由服务端追加）。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .auth import Principal

# FROM / JOIN 后的表名（跳过子查询括号）
_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# 标识符（用于列级检查）：双引号包裹 或 裸词
_IDENT_RE = re.compile(r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')
# 这些是 SQL 关键字/函数，不当列名看
_SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "by", "having", "limit", "offset",
    "join", "inner", "left", "right", "full", "outer", "on", "as", "and", "or", "not",
    "in", "is", "null", "like", "between", "case", "when", "then", "else", "end",
    "count", "sum", "avg", "min", "max", "distinct", "cast", "coalesce", "round",
    "date", "strftime", "substr", "lower", "upper", "abs", "with", "union", "all",
    "asc", "desc", "true", "false", "exists", "interval", "over", "partition",
    "row_number", "rank", "dense_rank", "current_date", "extract", "to_char",
}


def referenced_tables(sql: str) -> set[str]:
    return {m.lower() for m in _TABLE_RE.findall(sql or "")}


def referenced_columns(sql: str) -> set[str]:
    """SQL 里出现的标识符（排除关键字/函数）。用于列级黑名单检查。"""
    cols: set[str] = set()
    for quoted, bare in _IDENT_RE.findall(sql or ""):
        name = quoted or bare
        if not name or name.lower() in _SQL_KEYWORDS:
            continue
        cols.add(name.lower())
    return cols


def guard_sql(sql: str, principal: Optional[Principal]) -> Optional[str]:
    """表级 + 列级检查。返回错误信息（调用方转成 NON_RETRYABLE 失败）或 None。"""
    if principal is None or principal.is_anonymous and not principal.allowed_tables \
            and not principal.denied_columns:
        return None

    # 表级白名单
    if principal.allowed_tables:
        allowed = {t.lower() for t in principal.allowed_tables}
        # 内置维表/事实表也必须在白名单里 —— 白名单就是白名单
        for table in referenced_tables(sql):
            if table not in allowed:
                return f"无权访问表 {table}（allowed_tables 白名单）"

    # 列级黑名单：SQL 直接引用禁列 → 拒绝（而不是悄悄返回空）
    if principal.denied_columns:
        denied = {c.lower() for c in principal.denied_columns}
        hit = denied & referenced_columns(sql)
        if hit:
            return f"无权访问列 {'、'.join(sorted(hit))}（denied_columns 黑名单）"
    return None


def apply_row_filters(sql: str, principal: Optional[Principal]) -> tuple[str, list[str]]:
    """对 SQL 涉及的授权表追加行级谓词。返回 ``(新 SQL, 生效的表)``。

    实现选择：**追加 WHERE 谓词**（而不是把表包成子查询）——后者会破坏别名语义
    （`FROM fact_sales f` 包一层后 `f.col` 就失效了）。
    """
    if principal is None or not principal.row_filters:
        return sql, []
    tables = referenced_tables(sql)
    conds: list[str] = []
    applied: list[str] = []
    for table in sorted(tables):
        cond = (principal.row_filters or {}).get(table)
        if cond and str(cond).strip():
            conds.append(f"({cond})")
            applied.append(table)
    if not conds:
        return sql, []

    body = (sql or "").rstrip().rstrip(";")
    predicate = " AND ".join(conds)
    # 谓词必须插在 WHERE 位置、**不能**拼到 GROUP BY / ORDER BY / LIMIT 之后
    tail_re = re.compile(r"\b(group\s+by|order\s+by|limit|having)\b", re.IGNORECASE)
    m = tail_re.search(body)
    if m:
        head, tail = body[:m.start()].rstrip(), body[m.start():]
    else:
        head, tail = body, ""

    if re.search(r"\bwhere\b", head, re.IGNORECASE):
        new_sql = f"{head} AND {predicate}"
    else:
        new_sql = f"{head} WHERE {predicate}"
    return (f"{new_sql} {tail}" if tail else new_sql), applied


def drop_denied_columns(output: dict, principal: Optional[Principal]) -> tuple[dict, list[str]]:
    """结果里出现的禁列**整列剔除**（与 E4/02 的 strict 同思路：连列名都不给）。"""
    if not isinstance(output, dict) or principal is None or not principal.denied_columns:
        return output, []
    denied = {c.lower() for c in principal.denied_columns}
    dropped: set[str] = set()

    rows = output.get("rows")
    if isinstance(rows, list):
        new_rows = []
        for row in rows:
            if isinstance(row, dict):
                kept = {}
                for k, v in row.items():
                    if str(k).lower() in denied:
                        dropped.add(str(k))
                        continue
                    kept[k] = v
                new_rows.append(kept)
            else:
                new_rows.append(row)
        output["rows"] = new_rows

    cols_meta = output.get("columns")
    if isinstance(cols_meta, dict):
        for col in list(cols_meta.keys()):
            if str(col).lower() in denied:
                dropped.add(str(col))
                cols_meta.pop(col, None)
    return output, sorted(dropped)
