"""``schema_search`` tool – introspect tables/columns of the enterprise store."""
from __future__ import annotations

from typing import Any

from ...config import get_settings
from .dbguard import data_source_error


def run(params: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    src_err = data_source_error()
    if src_err:  # 缺库必须响亮失败，绝不静默创建空 SQLite 文件
        return {"ok": False, "error": src_err, "tables": []}
    # SEMANTIC/01：`query` 是 `keyword` 的别名（ToolSpec 历史契约）
    keyword = (params.get("keyword") or params.get("query") or "").strip().lower()
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError as exc:  # pragma: no cover
        return {"ok": False, "error": f"sqlalchemy 未安装: {exc}", "tables": []}

    # E7/01：按命名源取 DSN（不传 = 主源）
    try:
        from .datasource import resolve_source
        db_url, _dialect = resolve_source(params.get("source"))
    except KeyError as exc:
        return {"ok": False, "error": str(exc).strip(chr(39)), "tables": []}
    engine = create_engine(db_url, pool_pre_ping=True)
    insp = inspect(engine)
    table_names = insp.get_table_names()

    def _describe(t: str) -> dict[str, Any]:
        cols = insp.get_columns(t)
        col_info = [{"name": c["name"], "type": str(c["type"])} for c in cols]
        row_count = None
        try:
            with engine.connect() as conn:
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        except Exception:
            row_count = None
        return {"table": t, "columns": col_info, "row_count": row_count}

    results = []
    for t in table_names:
        blob = f"{t} " + " ".join(c["name"] for c in insp.get_columns(t))
        if keyword and keyword not in blob.lower():
            continue
        results.append(_describe(t))

    # 关键词零命中时放宽：返回前几张候选表，避免下游（dataset_profile/sql_query）
    # 因拿不到任何表而退化成 SELECT 1。典型场景：用户/LLM 用中文提指标、schema 是英文列名。
    if not results and keyword:
        results = [_describe(t) for t in table_names[:8]]
        for r in results:
            r["match"] = "keyword_fallback"

    # ATTACH/01：把该 session 用户上传的附件表并入候选。
    # 用户上传了数据时，这些表**优先于**内置企业库 —— 否则会出现
    # 「用户传了 sleep.csv，agent 却去查内置库」的错位。
    # ATTACH/02：**不做 keyword 过滤**。用户问的常是中文（"有什么规律"），
    # 而上传表的表名/列名是英文，任何 keyword 都会把用户自己的数据误杀 ——
    # 那正是「agent 去查内置库」的根因之一。上传表数量少，全量保留成本可忽略。
    try:
        from ..attachments import attached_tables
        uploaded = attached_tables(params.get("_session_id"))
        if uploaded:
            for t in uploaded:
                t["match"] = "user_upload"
            results = uploaded + results
    except Exception:
        pass

    return {"ok": True, "tables": results}
