"""E2-03：SQL 预检 —— 把"写错了"在执行前变成一句人话。

真实基线（`eval-real-20260915-d54.md`）里的两类失败**都不是模型"不会写 SQL"**：

1. **写在别的方言上**：`DATE_TRUNC` / `DATE_FORMAT` / `INTERVAL '1 month'`
   → 引擎是 SQLite，报 `no such function` / `near "'1 month'": syntax error`；
2. **编造表列**：`f.order_id` / `fs.customer_id` / `fact_traffic`
   → **而真实列就在上一轮 `schema_search` 的结果里躺着**。

`planner.md` 里"Never invent"早就写着，模型自己在 `context.assumptions` 里也写出了
真实列语义 —— **它看过 schema，生成 SQL 时仍然编**。提示词约束不住的，交给确定性检查。

**两条设计纪律**（见 `docs/specs/E2/03-sql-precheck.md`）：

* **方言提示必须看引擎**：E7 多源下 `input.source` 可能指向真 PG 库，
  那时 `DATE_TRUNC` 是**原生**写法 —— 不分引擎地"纠错"会拦掉正确的 SQL；
* **schema 检查宁漏不误**：只判**带限定符的 `alias.column`**（别名能解析到已知表），
  裸列名一律不判 —— **拦掉一条正确 SQL 比放过一条错的贵得多**（放过只是退化回现状）。

本模块是**纯函数**，不碰 state、不碰 DB、不抛异常。
"""
from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# 引擎归一
# --------------------------------------------------------------------------- #
_SQLITE = {"sqlite", "sqlite3"}
_PG = {"postgres", "postgresql", "pg"}


def normalize_engine(engine: str | None) -> str:
    e = str(engine or "").strip().lower()
    if e in _SQLITE:
        return "sqlite"
    if e in _PG:
        return "postgres"
    if e in {"mysql", "mariadb"}:
        return "mysql"
    return "unknown"


# --------------------------------------------------------------------------- #
# 一、方言预检
# --------------------------------------------------------------------------- #
# DATE_TRUNC('month', x) / DATE_TRUNC("month", x)
_DATE_TRUNC_RE = re.compile(r"\bDATE_TRUNC\s*\(\s*['\"]?(\w+)['\"]?\s*,", re.IGNORECASE)
_DATE_FORMAT_RE = re.compile(r"\bDATE_FORMAT\s*\(", re.IGNORECASE)
_INTERVAL_RE = re.compile(r"([+-]?)\s*\bINTERVAL\s+'([^']+)'", re.IGNORECASE)
_CURRENT_RE = re.compile(r"\b(CURRENT_DATE|CURRENT_TIMESTAMP)\b(?!\s*\()", re.IGNORECASE)
_SLEEP_RE = re.compile(r"\b(SLEEP|BENCHMARK)\s*\(", re.IGNORECASE)
_CAST_RE = re.compile(r"([A-Za-z_][\w.]*|'[^']*')\s*::\s*([A-Za-z_]\w*)")
_BACKTICK_RE = re.compile(r"`[^`]+`")

# `DATE_TRUNC` 的单位 → SQLite strftime 格式
_TRUNC_FMT = {
    "year": "%Y-01-01", "quarter": None, "month": "%Y-%m-01",
    "week": "%Y-W%W", "day": "%Y-%m-%d",
}

_ENGINE_NOTE = (
    "本库是 SQLite：日期用 strftime()/date()，不要用 DATE_TRUNC/DATE_FORMAT/INTERVAL；"
    '反引号标识符请改成双引号；`x::type` 请改成 CAST(x AS type)；'
    "CURRENT_DATE 请改成 date('now')。"
)


# --------------------------------------------------------------------------- #
# 一之二、方言先验（E2/04）：同一份知识的**另一个出口**
# --------------------------------------------------------------------------- #
# 错误提示（`_ENGINE_NOTE`）治的是"已经写错了"；planner 提示词要治的是"别一上来就写错"。
# 两者是**同一件事的两个时刻**，所以措辞**必须共用**——写第二份文本将来必然漂移：
# 改了一处忘另一处，就出现"提示词允许、预检拦截"这种两边口径相反的错。
#
# **每条先验必须是单行**：它按「每源一行」拼进 prompt，多行会让"源名"与"要点"
# 分家（读起来像是给所有源共用的要点，而 SQLite 与 PG 的要点恰恰相反）。
def _sqlite_brief() -> str:
    """SQLite 先验：日期替换表**从 `_TRUNC_FMT` 生成**，禁令与替代**复用 `_ENGINE_NOTE`**。"""
    fmt = "；".join(
        f"{unit}→strftime('{f}', \"列\")" for unit, f in _TRUNC_FMT.items() if f
    )
    return (
        "**sqlite** —— 按 SQLite 写（不要按 Postgres 写）。可用写法："
        f"日期聚合 {fmt}；相对日期 date(\"列\", '+7 day')。"
        + _ENGINE_NOTE
    )


_SQLITE_BRIEF = _sqlite_brief()
_PG_BRIEF = (
    "**postgres** —— 按 Postgres 写，本源的方言**比 SQLite 宽**："
    "DATE_TRUNC('month', \"列\")、INTERVAL '1 month'、x::type "
    "**都是原生写法，可以直接用**；标识符用双引号 \"列\"；当天用 CURRENT_DATE；"
    "分页用 LIMIT n OFFSET m。"
)
_MYSQL_BRIEF = (
    "**mysql** —— 按 MySQL 写：DATE_FORMAT(\"列\", '%Y-%m') 与 INTERVAL '1 month' "
    "**都是原生写法，可以直接用**；标识符用反引号；当天用 CURDATE()；"
    "分页用 LIMIT n OFFSET m。"
)

# 引擎（`normalize_engine` 的取值）→ 先验。**判不出来就不说**：猜一个引擎
# 等于把先验变成误导（`unknown` 不在表里 → `""`）。
_DIALECT_BRIEFS: dict[str, str] = {
    "sqlite": _SQLITE_BRIEF,
    "postgres": _PG_BRIEF,
    "mysql": _MYSQL_BRIEF,
}


def dialect_brief(engine: str | None) -> str:
    """该引擎的**方言要点**（给 planner 的先验）。未知引擎 → `""`。

    与 `dialect_hints` **同源**：SQLite 一条明确复用错误提示的措辞（`_ENGINE_NOTE`）
    与替换表（`_TRUNC_FMT`），因此不可能出现"提示词让写 X、预检拦 X"。
    分方向是**故意的**：PG/MySQL 上 `DATE_TRUNC`/`DATE_FORMAT` 是原生写法，
    只给 SQLite 的"禁令清单"等于换个方向犯同一个错。

    **返回单行**（调用方按「每源一行」拼装）：多行会让源名与要点分家。
    """
    return _DIALECT_BRIEFS.get(normalize_engine(engine), "")


def dialect_hints(sql: str | None, *, engine: str | None = "sqlite") -> list[str]:
    """该 SQL 里**在目标引擎上跑不通**的构造 → 可操作的替换提示（无问题返回 `[]`）。"""
    eng = normalize_engine(engine)
    if eng != "sqlite" or not sql:
        return []
    hints: list[str] = []

    for unit in _DATE_TRUNC_RE.findall(sql):
        fmt = _TRUNC_FMT.get(unit.lower())
        if fmt is None:
            hints.append(f"SQLite 没有 DATE_TRUNC('{unit}', ...)；"
                         "请改用 strftime() 或 date()，并按需要的精度聚合")
        else:
            hints.append(f"SQLite 没有 DATE_TRUNC('{unit}', ...)；"
                         f'改用 strftime(\'{fmt}\', "列名")')

    if _DATE_FORMAT_RE.search(sql):
        hints.append("SQLite 没有 DATE_FORMAT()；改用 strftime('%Y-%m', \"列名\")")

    for sign, span in _INTERVAL_RE.findall(sql):
        if sign.strip() in {"-", "+"}:
            delta = f"{sign}{span}"
            hints.append(f"SQLite 没有 INTERVAL；改用 date(\"列名\", '{delta}')")
        else:
            hints.append(f"SQLite 没有 INTERVAL；改用 date(\"列名\", '+{span}')")

    if _CURRENT_RE.search(sql):
        hints.append("CURRENT_DATE/CURRENT_TIMESTAMP → date('now') / datetime('now')")

    if _SLEEP_RE.search(sql):
        hints.append("SLEEP/BENCHMARK 会被工具直接拒绝（资源耗尽原语）")

    if _CAST_RE.search(sql):
        hints.append('`x::type` → CAST(x AS type)')

    if _BACKTICK_RE.search(sql):
        hints.append("反引号标识符 → 双引号（SQLite 用 \"col\"）")

    # 去重保序（同一条 SQL 里可能出现多次同种构造）
    return list(dict.fromkeys(hints))


# --------------------------------------------------------------------------- #
# 二、schema 预检
# --------------------------------------------------------------------------- #
# FROM/JOIN 后的表名（含 `upload.x` 这类前缀）；`FROM (` 是子查询，不匹配
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?!\()([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)",
    re.IGNORECASE,
)
_ALIAS_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)"
    r"(?:\s+AS\s+([A-Za-z_]\w*)|\s+([A-Za-z_]\w*))?",
    re.IGNORECASE,
)
_CTE_RE = re.compile(r"\b(?:WITH|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", re.IGNORECASE)
_QUALIFIED_RE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")

# 会被 `FROM x <word>` 误当成别名的关键字（含 `FROM t WHERE` / `FROM t GROUP BY`）
_NOT_AN_ALIAS = {
    "where", "group", "order", "limit", "having", "union", "join", "inner",
    "left", "right", "full", "outer", "cross", "on", "using", "as", "set",
    "select", "from", "window", "qualify", "lateral", "offset", "fetch",
    "and", "or", "when", "then", "else", "end", "by", "distinct", "all",
}


def _schema_index(schema: dict[str, list[str]] | None) -> dict[str, tuple[str, set[str]]]:
    """`{小写表名: (原表名, {小写列名})}`。"""
    out: dict[str, tuple[str, set[str]]] = {}
    for table, cols in (schema or {}).items():
        key = str(table or "").strip().lower()
        if key:
            out[key] = (str(table), {str(c).lower() for c in (cols or [])})
    return out


def unknown_references(sql: str | None,
                       schema: dict[str, list[str]] | None
                       ) -> tuple[list[str], list[str]]:
    """`(未知表, 未知列)`，各自去重保序。

    **只判能证明的**：表取 `FROM/JOIN` 之后的标识符；列取 `alias.column` 且
    `alias` 能解析到一张已知表。**裸列名一律不判**（无法与函数名/字符串/别名区分）。
    schema 为空 / SQL 为空 → `([], [])`（**不因"不知道"而报错**）。
    """
    if not sql or not schema:
        return [], []
    idx = _schema_index(schema)
    if not idx:
        return [], []

    ctes = {m.lower() for m in _CTE_RE.findall(sql)}

    # 别名 → 表（用于列判定）；同时收集表引用
    alias_to_table: dict[str, str] = {}
    unknown_tables: list[str] = []
    for m in _ALIAS_RE.finditer(sql):
        raw = m.group(1)
        if not raw:
            continue
        alias = (m.group(2) or m.group(3) or "").strip().lower()
        key = raw.lower()
        if key in ctes:
            continue  # CTE 名不是表
        entry = idx.get(key)
        if entry is None:
            # `upload.x` 未登记也算未知（但已登记的上传表会在 schema 里）
            if key not in unknown_tables:
                unknown_tables.append(raw)
            continue
        real_table = entry[0]
        if alias and alias not in _NOT_AN_ALIAS:
            alias_to_table[alias] = real_table
        else:
            # 无别名时，表名自身也可作限定符（`fact_sales.revenue`）
            alias_to_table.setdefault(key, real_table)

    unknown_cols: list[str] = []
    for qualifier, col in _QUALIFIED_RE.findall(sql):
        table = alias_to_table.get(qualifier.lower())
        if table is None:
            continue  # 前缀不是已知表的别名 → 不判（防假红）
        known_cols = idx.get(table.lower(), ("", set()))[1]
        if col.lower() not in known_cols and col not in unknown_cols:
            unknown_cols.append(col)
    return unknown_tables, unknown_cols


def known_schema(state: Any) -> dict[str, list[str]]:
    """从**最近一次成功 `schema_search`** 构造 `{表: [列]}`（纯读 state，不抛）。"""
    try:
        from .nodes import _last_result  # 延迟导入：避免与 nodes 循环依赖

        res = _last_result(state, "schema_search")
        if not res:
            return {}
        tables = (getattr(res, "output", None) or {}).get("tables") or []
        out: dict[str, list[str]] = {}
        for t in tables:
            name = str(t.get("table") or "")
            if name:
                out[name] = [str(c.get("name") or "") for c in (t.get("columns") or [])]
        return out
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 三、错误消息
# --------------------------------------------------------------------------- #
def format_error(sql: str | None,
                 schema: dict[str, list[str]] | None,
                 *,
                 message: str | None = None,
                 engine: str | None = "sqlite",
                 hints: list[str] | None = None) -> str:
    """把"原始报错 + 哪个标识符错 + 真实列清单 + 引擎提示"拼成**一段模型能照做的**错误。

    原始报错**必须保留**（它是事实）；提示是补充，不是替换。
    """
    eng = normalize_engine(engine)
    parts: list[str] = [str(message or "").strip() or "SQL 执行失败"]

    try:
        unknown_tables, unknown_cols = unknown_references(sql, schema)
    except Exception:
        unknown_tables, unknown_cols = [], []

    for col in unknown_cols:
        owner = _owner_of(sql, col, schema)
        if owner and schema.get(owner):
            cols = ", ".join(schema[owner])
            parts.append(f"列 `{col}` 不存在。`{owner}` 的真实列：{cols}")
        else:
            parts.append(f"列 `{col}` 不存在。可用列见上一步 schema_search 的结果。")

    for table in unknown_tables:
        names = ", ".join(sorted(schema.keys())) if schema else "（尚未发现任何表）"
        parts.append(f"表 `{table}` 不存在。已发现的表：{names}")

    parts.extend(str(h) for h in (hints or []))

    if eng == "sqlite":
        parts.append(_ENGINE_NOTE)
    return "\n".join(p for p in parts if p)


def _owner_of(sql: str, col: str, schema: dict[str, list[str]] | None) -> str | None:
    """反查该列是跟着哪个别名出现的 → 它对应的真实表名。"""
    for qualifier, name in _QUALIFIED_RE.findall(sql):
        if name != col:
            continue
        for m in _ALIAS_RE.finditer(sql):
            alias = (m.group(2) or m.group(3) or m.group(1) or "").lower()
            if alias == qualifier.lower():
                return m.group(1)
        return qualifier
    return None
