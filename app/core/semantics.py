"""SEMANTIC/01 业务语义层 — 让 Agent 看懂列的业务含义，而不是只见列名。

Spec: docs/specs/SEMANTIC/01-business-semantics.md

`region_id=1` 是"华东"、`channel_id=2` 是"合作伙伴"——这些不在 schema 里，只在维表里。
采集是**确定性、只读、有界、可缓存**的（不花 LLM），产物注入 context/planner/analyst，
使"业务词 → 列"的映射与 join 键的选择都不再靠猜。

安全前置：维表枚举**本身是数据**。E4/02（输出脱敏）尚未实现，故本模块**自带 PII 列跳过 + 审计**
（`is_pii_column`），不把姓名/手机/邮箱之类的值送进 LLM 上下文。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import get_settings
from .security.masking import SENSITIVE_COLUMN_RE as _PII_RE

# 维表的标签列候选
_LABEL_RE = re.compile(r"(_name$|^name$|_title$|^title$|_label$|^label$|名称|名$)", re.IGNORECASE)
# 维表名候选
_DIM_TABLE_RE = re.compile(r"^(dim_|d_)", re.IGNORECASE)

_MAX_DIM_TABLES = 12


def is_pii_column(name: str) -> bool:
    return bool(_PII_RE.search(str(name or "")))


@dataclass
class Semantics:
    relationships: list[dict] = field(default_factory=list)
    dimensions: dict[str, dict] = field(default_factory=dict)
    skipped_pii: list[str] = field(default_factory=list)
    source: str = "naming_convention"

    def is_empty(self) -> bool:
        return not self.relationships and not self.dimensions


def _key_col_of(columns: list[str], table: str) -> Optional[str]:
    """维表键列：优先 `<单数表名>_id`，否则首个 `*_id`，否则首列。"""
    base = re.sub(r"^(dim_|d_)", "", table.lower())
    singular = base[:-1] if base.endswith("s") else base
    preferred = f"{singular}_id"
    for c in columns:
        if c.lower() == preferred:
            return c
    for c in columns:
        if c.lower().endswith("_id") or c.lower() == "id":
            return c
    return columns[0] if columns else None


def _label_col_of(columns: list[str], key_col: Optional[str]) -> Optional[str]:
    for c in columns:
        if c != key_col and _LABEL_RE.search(c):
            return c
    return None


def infer_relationships(tables: list[dict]) -> list[dict]:
    """按**命名约定**推断维表键与多对一关系（样例库没有真实 FK 约束）。

    `confidence` 如实标注为 naming_convention——上游不得把它当事实。
    """
    rels: list[dict] = []
    dims: dict[str, tuple[str, str]] = {}  # key_col(lower) -> (table, key_col)
    for t in tables or []:
        name = str(t.get("table") or "")
        cols = [str(c.get("name")) for c in (t.get("columns") or []) if isinstance(c, dict)]
        if not name or not cols:
            continue
        label_col = _label_col_of(cols, _key_col_of(cols, name))
        if not (_DIM_TABLE_RE.match(name) or label_col):
            continue
        key_col = _key_col_of(cols, name)
        if key_col:
            dims[str(key_col).lower()] = (name, key_col)
    for t in tables or []:
        name = str(t.get("table") or "")
        for c in t.get("columns") or []:
            col = str(c.get("name") or "")
            hit = dims.get(col.lower())
            if not hit or hit[0] == name:
                continue
            dim_table, dim_key = hit
            rels.append({"from_table": name, "from_col": col,
                         "to_table": dim_table, "to_col": dim_key,
                         "kind": "many_to_one", "confidence": "naming_convention"})
    return rels


def _collect_uncached(session_id: str = "") -> Semantics:
    settings = get_settings()
    try:
        from .tools.dbguard import data_source_error

        err = data_source_error()
        if err:
            raise RuntimeError(err)
        from sqlalchemy import create_engine, inspect, text

        engine = create_engine(settings.data_db_url, pool_pre_ping=True)
        insp = inspect(engine)
        tables: list[dict] = []
        for t in insp.get_table_names():
            cols = [{"name": c["name"], "type": str(c["type"])} for c in insp.get_columns(t)]
            tables.append({"table": t, "columns": cols})
    except Exception as exc:
        sem = Semantics()
        sem.error = str(exc)  # type: ignore[attr-defined]
        return sem

    sem = Semantics(relationships=infer_relationships(tables))
    cap = int(getattr(settings, "profile_enum_max_values", 20) or 20)

    # 维表取值：键 → 标签（跳过 PII 列）
    selected = [t for t in tables if _DIM_TABLE_RE.match(str(t.get("table")))]
    for t in selected[:_MAX_DIM_TABLES]:
        name = str(t["table"])
        cols = [str(c["name"]) for c in t["columns"]]
        key_col = _key_col_of(cols, name)
        label_col = _label_col_of(cols, key_col)
        if not key_col:
            continue
        for col in [c for c in (key_col, label_col) if c]:
            if is_pii_column(col):
                sem.skipped_pii.append(f"{name}.{col}")
        if label_col and is_pii_column(label_col):
            continue  # 标签列是 PII → 整张表不采值（键本身无意义）
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(
                    f'SELECT "{key_col}"' + (f', "{label_col}"' if label_col else "") +
                    f' FROM "{name}" LIMIT {cap}')).fetchall()
        except Exception:
            continue  # 单表失败不影响其余（元数据查询绝不让分析失败）
        values = {str(r[0]): (str(r[1]) if label_col and len(r) > 1 else str(r[0])) for r in rows}
        entry = {"table": name, "key_col": key_col, "label_col": label_col, "values": values}
        for col in cols:
            if col.lower().endswith("_id") or col == key_col:
                sem.dimensions.setdefault(col, entry)
    return sem


def collect_semantics(session_id: str = "", *, use_cache: bool = True) -> Semantics:
    """采集（带 short_term 缓存）。任何失败都退化为空 Semantics，绝不抛。"""
    settings = get_settings()
    key = "semantics:" + hashlib.sha1(settings.data_db_url.encode("utf-8")).hexdigest()[:12]
    ttl = float(getattr(settings, "semantics_ttl_s", 3600) or 3600)

    if use_cache and ttl > 0:
        try:
            from .memory import short_term

            cached = short_term.get(session_id, key)
            if isinstance(cached, dict) and time.time() - float(cached.get("ts", 0)) < ttl:
                c = cached.get("payload") or {}
                return Semantics(relationships=c.get("relationships") or [],
                                 dimensions=c.get("dimensions") or {},
                                 skipped_pii=c.get("skipped_pii") or [])
        except Exception:
            pass

    sem = _collect_uncached(session_id)
    if use_cache and ttl > 0 and not sem.is_empty():
        try:
            from .memory import short_term

            short_term.put(session_id, key, {
                "ts": time.time(),
                "payload": {"relationships": sem.relationships, "dimensions": sem.dimensions,
                            "skipped_pii": sem.skipped_pii},
            })
        except Exception:
            pass
    return sem


def describe_semantics(sem: Optional[Semantics], *, max_dims: int = 6,
                       max_values: int = 8) -> str:
    """紧凑文本（有界），直接进 prompt。无可用语义时返回空串。"""
    if sem is None or sem.is_empty():
        return ""
    lines = ["[业务语义]（由表结构+维表取值自动采集；关系按命名约定推断，需与实际口径核对）"]
    for col, meta in list(sem.dimensions.items())[:max_dims]:
        vals = list((meta.get("values") or {}).items())[:max_values]
        shown = "/".join(v for _, v in vals)
        more = "" if len(meta.get("values") or {}) <= max_values else f" …共{len(meta['values'])}个"
        label = meta.get("label_col") or ""
        lines.append(f"- {meta.get('table')}: {col}" + (f" → {label}" if label else "") +
                     (f"（{shown}{more}）" if shown else ""))
    rels = sem.relationships[:max_dims]
    if rels:
        lines.append("- 关系(fk-naming)：" + "；".join(
            f"{r['from_table']}.{r['from_col']} → {r['to_table']}.{r['to_col']}" for r in rels))
    if sem.skipped_pii:
        lines.append(f"- 已跳过疑似 PII 列（未采集取值）：{'、'.join(sem.skipped_pii[:5])}")
    return "\n".join(lines)
