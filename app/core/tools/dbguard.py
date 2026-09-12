"""Data-source guard: fail loudly when the configured DB is missing.

SQLite silently creates an empty database file on first connection. For an
analytical agent that is a nasty failure mode: every table lookup fails with
"no such table", the executor degrades to ``SELECT 1`` fallbacks, and the
report looks fabricated. The guard rejects a missing SQLite file *before*
any engine is created, so no empty file is ever written.
"""
from __future__ import annotations

from pathlib import Path

from ...config import get_settings


def data_source_error() -> str | None:
    """Return an error message if the configured data source is missing."""
    url = get_settings().data_db_url
    if not url.startswith("sqlite"):
        return None  # postgres/mysql 连接串由服务端校验，无本地文件可查
    # sqlite:///./data/x.db → ./data/x.db；sqlite:////abs/path → /abs/path
    path = url.split("///", 1)[-1] if "///" in url else ""
    if path and not Path(path).exists():
        return (
            f"数据源不存在: {path}。"
            "请从项目根目录运行（相对路径基于工作目录解析），"
            "或检查 DATA_DB_URL 配置 / 先执行 scripts/generate_sample.py。"
        )
    return None
