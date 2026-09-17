"""``schema_search`` tool – introspect tables/columns of the enterprise store."""
from __future__ import annotations

import re
from typing import Any

from ...config import get_settings
from .dbguard import data_source_error


def _keyword_tokens(keyword: str) -> list[str]:
    """关键词拆 token：整串子串匹配对「aoa_dept 行数」这类**混合短语**必然失配
    （"aoa_dept 行数" 不是任何表 blob 的子串），拆出词后任一命中才算发现。

    只留有区分度的 token：ASCII 标识符（≥3 字符，避开 of/the 这类）与中文词（≥2 字）。
    """
    if not keyword:
        return []
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", keyword)


def _hit(blob: str, keyword: str) -> bool:
    """表名+列名 blob 是否命中关键词：整串子串 OR 任一 token 命中。

    空 keyword 恒真（＝不过滤，兼容既有「keyword 缺省回全部表」的行为）。
    """
    if not keyword:
        return True
    if keyword in blob:
        return True
    return any(tok in blob for tok in _keyword_tokens(keyword))


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
        from .datasource import DEFAULT_SOURCE, resolve_source
        requested = (str(params.get("source") or "").strip() or DEFAULT_SOURCE)
        db_url, _dialect = resolve_source(requested)
    except KeyError as exc:
        return {"ok": False, "error": str(exc).strip(chr(39)), "tables": []}
    engine = create_engine(db_url, pool_pre_ping=True)
    insp = inspect(engine)
    table_names = insp.get_table_names()

    def _describe(t: str, source_name: str, inspector=insp,
                  eng=engine) -> dict[str, Any]:
        cols = inspector.get_columns(t)
        col_info = [{"name": c["name"], "type": str(c["type"])} for c in cols]
        row_count = None
        try:
            with eng.connect() as conn:
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
        except Exception:
            row_count = None
        return {"table": t, "columns": col_info, "row_count": row_count,
                "source": source_name}

    results = []
    for t in table_names:
        blob = f"{t} " + " ".join(c["name"] for c in insp.get_columns(t))
        if _hit(blob.lower(), keyword):
            results.append(_describe(t, requested))

    # E7/02 跨源发现：指定源零命中 ≠ 表不存在——它可能就在另一个命名源里
    # （真实场景：用户接了 oasys(MySQL)，问「aoa_dept 行数」；此前只搜主源，
    # 报 no such table，明明页面里连接是成功的）。扫描其余命名源并合并命中，
    # 每张表带 `source` 标记，下游据此把 sql_query 路由到正确的源。
    # 注意：**只要带了 keyword 就扫描命名源**，而非仅当主源零命中时才扫——
    # 否则一旦主源也返回了表（含 keyword_fallback 兜底），`results` 非空会把
    # 跨源扫描整个跳过，aoa_dept 就再也发现不了（线上实测复现过）。
    if keyword:
        try:
            from .datasource import sources as _all_sources
            others = [n for n in _all_sources() if n != requested]
        except Exception:
            others = []
        for name in others:
            try:
                ourl, _od = resolve_source(name)
                oengine = create_engine(ourl, pool_pre_ping=True)
                oinsp = inspect(oengine)
                for t in oinsp.get_table_names():
                    blob = f"{t} " + " ".join(c["name"] for c in oinsp.get_columns(t))
                    if _hit(blob.lower(), keyword):
                        d = _describe(t, name, inspector=oinsp, eng=oengine)
                        d["match"] = "cross_source"
                        results.append(d)
            except Exception:
                continue  # 单个命名源连不上不能拖垮整体发现

    # 关键词零命中时放宽：返回前几张候选表，避免下游（dataset_profile/sql_query）
    # 因拿不到任何表而退化成 SELECT 1。典型场景：用户/LLM 用中文提指标、schema 是英文列名。
    if not results and keyword:
        results = [_describe(t, requested) for t in table_names[:8]]
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
