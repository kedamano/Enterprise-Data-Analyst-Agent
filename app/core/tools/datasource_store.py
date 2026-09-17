"""E7/02 页面「新建连接」——本地数据源存储 + DSN 构造。

Spec: docs/specs/E7/01-multi-source-deploy.md 的延伸：数据源此前只能改 `.env`
重启生效，页面上的「新建连接」需要一份**可写**的存储。

设计取舍：
- **明文密码只落本地 JSON**（``data/datasources.json``，信任边界与 ``.env`` 相同）；
  对外接口一律走 ``health._mask_dsn`` 脱敏，任何响应都不回显密码。
- 写入用 **tmp 文件 + 原子替换**，进程内加锁——分析运行中新增源不能写坏文件。
- 文件缺失 / 损坏一律当**空库**：配置问题不阻塞服务（与 DATA_SOURCES 同哲学）。
- ``default``（主源）是保留名，页面永远不能覆盖它。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

logger = logging.getLogger("da.datasource_store")

# SQLAlchemy 方言前缀（带驱动，避免依赖服务器上配置的默认驱动）
_DIALECT_PREFIX = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg2",
}
_DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432}
DEFAULT_SOURCE = "default"


def build_url(
    dialect: str,
    *,
    host: str = "",
    port: int = 0,
    database: str = "",
    username: str = "",
    password: str = "",
    path: str = "",
) -> str:
    """按页面表单字段构造 SQLAlchemy DSN。

    - sqlite：只要文件路径（反斜杠归一化为正斜杠）。
    - mysql / postgresql：host + port(默认 3306/5432) + database + 凭证；
      用户名/密码 ``quote_plus``——密码里的 ``@ : /`` 不得破坏 URL 结构。
    """
    d = (dialect or "").strip().lower()
    if d == "sqlite":
        p = (path or "").strip().replace("\\", "/")
        if not p:
            raise ValueError("SQLite 需要提供数据库文件路径")
        return f"sqlite:///{p}"
    if d in _DIALECT_PREFIX:
        host = host.strip()
        database = (database or "").strip()
        if not host:
            raise ValueError("需要提供主机地址")
        if not database:
            raise ValueError("需要提供数据库名")
        user = quote_plus(username or "")
        pwd = quote_plus(password or "")
        netloc = f"{user}:{pwd}@{host}" if (user or pwd) else host
        p = int(port) if port else _DEFAULT_PORTS[d]
        return f"{_DIALECT_PREFIX[d]}://{netloc}:{p}/{database}"
    raise ValueError(f"不支持的数据库类型：{dialect}（支持 SQLite / MySQL / PostgreSQL）")


class DataSourceStore:
    """``{name: {name, url, dialect, created_at}}`` 的 JSON 落盘存储。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- 读 --

    def list_entries(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return dict(self._read())

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("数据源存储不可读，按空库处理：%s", exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("数据源存储 JSON 损坏，按空库处理：%s", exc)
            return {}
        if not isinstance(data, list):
            return {}
        out: dict[str, dict[str, str]] = {}
        for item in data:
            if not (isinstance(item, dict) and item.get("name") and item.get("url")):
                continue
            out[str(item["name"])] = {
                "name": str(item["name"]),
                "url": str(item["url"]),
                "dialect": str(item.get("dialect") or ""),
                "created_at": str(item.get("created_at") or ""),
            }
        return out

    # -- 写 --

    def add(self, name: str, url: str, dialect: str) -> dict[str, str]:
        name = (name or "").strip()
        if not name:
            raise ValueError("连接名称不能为空")
        if name == DEFAULT_SOURCE:
            raise ValueError('"default" 是主源保留名，请换一个名称')
        with self._lock:
            entries = self._read()
            if name in entries:
                raise ValueError(f"连接名 {name!r} 已存在")
            entry = {
                "name": name,
                "url": url,
                "dialect": dialect,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            entries[name] = entry
            self._write(entries)
        return entry

    def remove(self, name: str) -> bool:
        with self._lock:
            entries = self._read()
            if name not in entries:
                return False
            del entries[name]
            self._write(entries)
        return True

    def _write(self, entries: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(list(entries.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)
