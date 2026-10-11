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

    SEC/04：改进注入位置检测，避免在子查询/CTE 场景下注入到错误位置。
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

    # SEC/04: 更安全的 WHERE 注入位置检测
    # 1. 找到最外层 SELECT 的位置
    # 2. 在该 SELECT 对应的 FROM 子句之后、GROUP BY/ORDER BY/LIMIT 之前注入 WHERE
    # 3. 如果已有 WHERE，则在最后一个 WHERE 之后追加 AND

    # 找到最外层 SELECT 的位置（跳过子查询和 CTE）
    select_pos = _find_outermost_select(body)
    if select_pos == -1:
        # 无法解析，回退到原有逻辑
        return _apply_row_filters_fallback(body, predicate), applied

    # 从最外层 SELECT 开始，找到对应的 FROM 子句
    from_pos = _find_from_after_select(body, select_pos)
    if from_pos == -1:
        return _apply_row_filters_fallback(body, predicate), applied

    # 找到 WHERE 插入点（FROM 之后，GROUP BY/ORDER BY/LIMIT 之前）
    insert_pos = _find_where_insert_position(body, from_pos)
    if insert_pos == -1:
        return _apply_row_filters_fallback(body, predicate), applied

    # 检查是否已有 WHERE
    where_match = re.search(r"\bwhere\b", body[from_pos:insert_pos], re.IGNORECASE)
    if where_match:
        # 已有 WHERE，在 WHERE 子句末尾追加 AND
        where_end = from_pos + where_match.end()
        # 找到 WHERE 子句的结束位置（GROUP BY/ORDER BY/LIMIT 之前）
        where_clause_end = _find_where_clause_end(body, where_end)
        new_sql = body[:where_clause_end] + f" AND {predicate}" + body[where_clause_end:]
    else:
        # 没有 WHERE，在 INSERT 位置插入 WHERE
        new_sql = body[:insert_pos] + f" WHERE {predicate}" + body[insert_pos:]

    return new_sql, applied


def _find_outermost_select(sql: str) -> int:
    """找到最外层 SELECT 的位置，跳过子查询和 CTE。"""
    depth = 0
    i = 0
    while i < len(sql):
        if sql[i] == '(':
            depth += 1
        elif sql[i] == ')':
            depth -= 1
        elif depth == 0:
            # 检查是否是 SELECT 关键字
            match = re.match(r"\bselect\b", sql[i:], re.IGNORECASE)
            if match:
                return i
        i += 1
    return -1


def _find_from_after_select(sql: str, select_pos: int) -> int:
    """找到 SELECT 之后的 FROM 子句位置。"""
    match = re.search(r"\bfrom\b", sql[select_pos:], re.IGNORECASE)
    if match:
        return select_pos + match.start()
    return -1


def _find_where_insert_position(sql: str, from_pos: int) -> int:
    """找到 WHERE 插入位置（FROM 之后，GROUP BY/ORDER BY/LIMIT 之前）。"""
    tail_re = re.compile(r"\b(group\s+by|order\s+by|limit|having)\b", re.IGNORECASE)
    m = tail_re.search(sql[from_pos:])
    if m:
        return from_pos + m.start()
    return len(sql)


def _find_where_clause_end(sql: str, where_start: int) -> int:
    """找到 WHERE 子句的结束位置（GROUP BY/ORDER BY/LIMIT 之前）。"""
    tail_re = re.compile(r"\b(group\s+by|order\s+by|limit|having)\b", re.IGNORECASE)
    m = tail_re.search(sql[where_start:])
    if m:
        return where_start + m.start()
    return len(sql)


def _apply_row_filters_fallback(body: str, predicate: str) -> str:
    """回退到原有的字符串拼接方式。"""
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
    return f"{new_sql} {tail}" if tail else new_sql


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
