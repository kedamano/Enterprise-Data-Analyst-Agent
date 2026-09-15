"""E7/01 命名数据源解析 — 单一入口。

Spec: docs/specs/E7/01-multi-source-deploy.md

- **不传 = 主源**（`DATA_DB_URL`），既有行为完全不变。
- 命名源来自 `DATA_SOURCES`（JSON 数组或 `name=url` 逗号列表）。
- 配置写坏**不阻塞启动**：忽略坏条目并 warning（配置错误不该让服务起不来）。
- **明确不做跨源 JOIN/联邦查询**（需要联邦引擎与下推优化，属另一量级）——
  需要跨源就在各自源取数后用 `python_analysis` 合并。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...config import get_settings

logger = logging.getLogger("da.datasource")

DEFAULT_SOURCE = "default"


def _parse_sources(raw: Any) -> dict[str, dict[str, str]]:
    """把配置解析成 ``{name: {url, dialect}}``。坏条目忽略并 warning。"""
    if not raw:
        return {}
    items: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        if text[0] in "[{":
            try:
                items = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning("DATA_SOURCES JSON 解析失败，已忽略：%s", exc)
                return {}
        else:  # name=url,name2=url2
            items = []
            for part in text.split(","):
                if "=" in part:
                    name, _, url = part.partition("=")
                    items.append({"name": name.strip(), "url": url.strip()})
    elif isinstance(raw, list):
        items = raw
    else:
        return {}

    out: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            logger.warning("DATA_SOURCES 条目不是对象，已忽略：%r", item)
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url or name == DEFAULT_SOURCE:
            logger.warning("DATA_SOURCES 条目缺少 name/url 或与主源重名，已忽略：%r", item)
            continue
        out[name] = {"url": url, "dialect": str(item.get("dialect") or "").strip()
                     or _guess_dialect(url)}
    return out


def _guess_dialect(url: str) -> str:
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("postgres"):
        return "postgresql"
    if url.startswith("mysql"):
        return "mysql"
    return "unknown"


def sources() -> dict[str, dict[str, str]]:
    """全部可用源（含主源，名为 ``default``）。

    三层合并：主源（``DATA_DB_URL``）→ env 命名源（``DATA_SOURCES``）→
    页面「新建连接」的本地存储（``datasource_store_path``）。local 同名**覆盖**
    env 源（页面是最新意图）；``default`` 主源永远不可被覆盖。
    """
    settings = get_settings()
    out = {DEFAULT_SOURCE: {"url": settings.data_db_url,
                            "dialect": settings.data_db_dialect or _guess_dialect(settings.data_db_url)}}
    out.update(_parse_sources(getattr(settings, "data_sources", "")))
    try:
        from .datasource_store import DataSourceStore

        local = DataSourceStore(settings.datasource_store_path).list_entries()
    except Exception as exc:  # 存储故障不阻塞源解析
        logger.warning("本地数据源存储读取失败，忽略页面新建的源：%s", exc)
        local = {}
    for name, entry in local.items():
        if name == DEFAULT_SOURCE:
            continue
        out[name] = {"url": entry["url"],
                     "dialect": entry.get("dialect") or _guess_dialect(entry["url"])}
    return out


def local_source_names() -> list[str]:
    """页面「新建连接」落盘的源名——只有这些可从页面删除。"""
    try:
        from .datasource_store import DataSourceStore

        return list(DataSourceStore(get_settings().datasource_store_path).list_entries())
    except Exception:
        return []


def available_sources() -> list[str]:
    """可用源名（**只有名字**，绝不回 DSN——health/日志用它）。"""
    return [DEFAULT_SOURCE] + [n for n in sources() if n != DEFAULT_SOURCE]


def resolve_source(name: Optional[str] = None) -> tuple[str, str]:
    """``name`` → ``(url, dialect)``。缺省用主源；未知源抛可读 ``KeyError``。"""
    all_sources = sources()
    key = str(name or "").strip() or DEFAULT_SOURCE
    if key not in all_sources:
        raise KeyError(
            f"未知数据源 {key!r}；可用源：{'、'.join(available_sources())}。"
            "（如需跨源分析，请在各源分别取数后用 python_analysis 合并——本系统不做跨源 JOIN）")
    entry = all_sources[key]
    return entry["url"], entry["dialect"]
