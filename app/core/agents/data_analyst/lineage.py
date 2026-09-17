"""D47：**指标血缘** —— metric → 计算口径 → 源列/表。

缺口（`docs/开发计划_企业化.md` §8.4 C-1）
------------------------------------------
> 指标语义层（重版）：**指标血缘**、口径注册表、同环比基线自动判定
> —— 当前只做了"维表枚举+键推断"轻版。

分析师问"这个营收数是怎么算出来的"，现在的答案只能到 E1 的 `[src:step_id]`（哪条 SQL），
**再往下就断了**：那条 SQL 读了哪些表、哪些列，没人说得清。本模块把链子补到列级。

两条诚实纪律（沿用 SEMANTIC/01 的 `confidence` 范式）
----------------------------------------------------
1. **解析出来的 ≠ 事实**：表名/列名是从 SQL **文本**解析的，标
   `confidence="parsed_from_sql"`；上游不得当成权威元数据
   （真实血缘应读 `information_schema` 或查询计划，那是另一件事）。
2. **没有就说没有**：`sql_id` 缺失/解析不到 → `traced=False` + 可读原因，
   **绝不从别处"借"一个来源**——编一条血缘比没有血缘更坏。

纯函数：不调 LLM、不发查询、不写状态（与 `gate.py` / `caliber.py` 同范式）。
"""
from __future__ import annotations

import re
from typing import Any, Optional

# 复用 gate 的表名解析（同口径，避免两处漂移）
from .gate import _FROM_CLAUSE_RE, _FROM_ITEM_RE, _TABLE_RE

# SQL 关键字/函数名：**不是列名**，必须排除（否则 SELECT SUM(...) 会把 sum 当列）
_KEYWORDS = frozenset("""
select from where group by order having limit union join inner left right outer on
as and or not in like between is null desc asc distinct case when then else end
sum avg count min max total round cast coalesce abs date datetime strftime substr
""".split())

# 列引用：限定名 `t.col` 或裸名 `col`
_COL_RE = re.compile(r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z_][A-Za-z0-9_]*)\b")
_SELECT_RE = re.compile(r"\bselect\b(.*?)\bfrom\b", re.IGNORECASE | re.DOTALL)
_ALIAS_AS_RE = re.compile(r"\bas\s+[A-Za-z_][A-Za-z0-9_]*\s*$", re.IGNORECASE)
_TRAILING_ALIAS_RE = re.compile(r"\s+[A-Za-z_][A-Za-z0-9_]*\s*$")


def parse_sql_sources(sql: Any) -> tuple[list[str], list[str]]:
    """从 SQL 文本解析 ``(表名, 列名)`` —— **保序、去重、小写归一**。

    ⚠️ 这是**文本解析**，不是元数据；调用方应把它标为 `parsed_from_sql`。
    解析不出就返回空列表（宁缺勿滥）。
    """
    text = str(sql or "").strip()
    if not text:
        return [], []

    tables: list[str] = [m.lower() for m in _TABLE_RE.findall(text)]
    clause = _FROM_CLAUSE_RE.search(text)
    if clause:
        tables += [t.lower() for t in _FROM_ITEM_RE.findall(clause.group(1))]
    tables = _dedup(tables)

    columns: list[str] = []
    select = _SELECT_RE.search(text)
    if select:
        for item in _split_top_level(select.group(1)):
            columns += _columns_of(item)
    for kw in ("group by", "order by", "having"):
        for grp in re.findall(rf"\b{kw}\b(.*?)(?=\bwhere\b|\bgroup\s+by\b|\border\s+by\b|"
                              rf"\bhaving\b|\blimit\b|\bunion\b|$)", text, re.IGNORECASE | re.DOTALL):
            for item in _split_top_level(grp):
                columns += _columns_of(item)
    where = re.search(r"\bwhere\b(.*?)(?=\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|$)",
                      text, re.IGNORECASE | re.DOTALL)
    if where:
        # WHERE 里的字面量（数字/引号串）先抹掉，否则会把值当列
        literal_free = re.sub(r"'[^']*'", " ", where.group(1))
        literal_free = re.sub(r"\b\d+(?:\.\d+)?\b", " ", literal_free)
        columns += _columns_of(literal_free)

    return tables, _dedup([c for c in columns if c not in _KEYWORDS and not c.isdigit()])


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分（跳过括号内），避免 `SUM(a, b)` / 函数参数被切碎。"""
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _columns_of(item: str) -> list[str]:
    """一个 SELECT/GROUP BY 片段里的列名（剥掉 `AS 别名` 与末尾隐式别名）。"""
    frag = item.strip()
    frag = _ALIAS_AS_RE.sub(" ", frag)
    if not re.search(r"[(),]", frag):        # 无函数/表达式时，末尾裸标识符是别名
        frag = _TRAILING_ALIAS_RE.sub(" ", frag)
    return [m.lower() for m in _COL_RE.findall(frag)]


def _metric_field(metric: Any, key: str, default: str = "") -> str:
    if isinstance(metric, dict):
        value = metric.get(key)
    else:                                     # pydantic 实例或其它对象
        value = getattr(metric, key, None)
    return str(value) if value is not None else default


def metric_lineage(analysis: Any, results: list[Any]) -> list[dict]:
    """每个指标的来源链。**畸形输入不抛**（血缘是只读辅助，不是关键路径）。"""
    from .sources import sql_of_step

    out: list[dict] = []
    for metric in (getattr(analysis, "metrics", None) or []):
        try:
            name = _metric_field(metric, "name")
            definition = _metric_field(metric, "definition")
            sql_id = _metric_field(metric, "sql_id") or None
            entry: dict[str, Any] = {
                "metric": name, "definition": definition, "sql_id": sql_id,
                "sql": None, "tables": [], "columns": [],
                "traced": False, "confidence": "unresolved", "note": "",
            }
            if not sql_id:
                entry["note"] = "该指标未标注 sql_id（模型未给来源），无法继续溯源"
                out.append(entry)
                continue
            sql, _hash, _rows = sql_of_step(sql_id, results)
            if not sql:
                entry["note"] = f"sql_id={sql_id} 解析不到对应的 SQL 步骤"
                out.append(entry)
                continue
            tables, columns = parse_sql_sources(sql)
            entry.update({"sql": sql, "tables": tables, "columns": columns,
                          "traced": True, "confidence": "parsed_from_sql",
                          "note": "表/列由 SQL 文本解析，非权威元数据"})
            out.append(entry)
        except Exception:  # noqa: BLE001 - 单个指标解析失败不影响其余
            out.append({"metric": _metric_field(metric, "name"), "definition": "",
                        "sql_id": None, "sql": None, "tables": [], "columns": [],
                        "traced": False, "confidence": "unresolved",
                        "note": "解析异常（已跳过）"})
    return out


def lineage_summary(lineages: list[dict]) -> dict:
    """汇总：指标数 / 已溯源数 / 覆盖率（**零指标 → None，不是 0**）。"""
    total = len(lineages or [])
    traced = sum(1 for x in (lineages or []) if x.get("traced"))
    return {"metrics": total, "traced": traced,
            "coverage": round(traced / total, 4) if total else None}
