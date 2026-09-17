"""MCP **客户端**配置存储。

方向说明（**别和 `../mcp_server.py` 搞混**）：

| 模块 | 方向 | 作用 |
|---|---|---|
| `integrations/mcp_server.py` | 出 | 把**我们的**只读工具按 MCP 协议**暴露**给外部客户端 |
| `integrations/mcp_client/` | 入 | 管理**我们要去连的**外部 MCP server（context7/fetch/time…） |

存储：单个 JSON 文件（默认 `data/mcp_servers.json`），结构 ``{"servers": [...]}``。
之所以用 JSON 而不是复用 SQLite：这份配置**要能手改、能进 git、能一眼看完**，
且规模极小（通常 <20 条），没有事务需求。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_TRANSPORTS = {"stdio", "sse", "http"}
_SLUG_RE = re.compile(r"[^a-z0-9\-]+")

# 敏感字段：返回给前端时**不脱敏**（用户自己配的、要能编辑），
# 但审计/日志里必须遮蔽。见 `redact()`。
_SECRET_KEYS = ("token", "password", "api_key", "apikey", "secret", "authorization")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _slugify(name: str) -> str:
    base = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    if not base:
        base = "server-" + uuid.uuid4().hex[:8]
    return base[:48]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # 支持界面里的"一行一个"文本框
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _as_dict(value: Any) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    out: dict[str, str] = {}
    if isinstance(value, str):
        for ln in value.splitlines():
            ln = ln.strip()
            if not ln or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def redact(server: dict[str, Any]) -> dict[str, Any]:
    """遮蔽敏感字段（供日志/审计）。**不用于 API 响应**。"""
    out = dict(server)
    for key in ("env", "headers"):
        if isinstance(out.get(key), dict):
            out[key] = {k: ("***" if any(s in k.lower() for s in _SECRET_KEYS) else v)
                        for k, v in out[key].items()}
    return out


def normalize_server(data: dict[str, Any]) -> dict[str, Any]:
    """把界面/API 传来的松散字段归一成规范 server 配置（**纯函数，不落盘**）。

    抽成模块级是为了让"未保存前的连接测试"也能复用同一套校验，
    避免"测试通过、保存报错"这种两套规则不一致的割裂。
    """
    transport = str(data.get("transport") or "stdio").strip().lower()
    if transport not in _TRANSPORTS:
        raise ValueError(f"不支持的 transport: {transport}（可选 {'/'.join(sorted(_TRANSPORTS))}）")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    server: dict[str, Any] = {
        "name": name,
        "transport": transport,
        "enabled": bool(data.get("enabled", True)),
    }
    if transport == "stdio":
        command = str(data.get("command") or "").strip()
        if not command:
            raise ValueError("stdio 传输必须提供 command")
        server["command"] = command
        server["args"] = _as_list(data.get("args"))
        server["env"] = _as_dict(data.get("env"))
        server["url"] = ""
        server["headers"] = {}
    else:
        url = str(data.get("url") or "").strip()
        if not url:
            raise ValueError(f"{transport} 传输必须提供 url")
        server["url"] = url
        server["headers"] = _as_dict(data.get("headers"))
        server["command"] = ""
        server["args"] = []
        server["env"] = {}
    if data.get("description"):
        server["description"] = str(data["description"]).strip()
    return server


class MCPServerStore:
    """JSON 文件型 MCP server 配置仓库。root 可注入（测试用临时文件）。"""
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            try:
                from ....config import get_settings

                path = getattr(get_settings(), "mcp_servers_path", "data/mcp_servers.json")
            except Exception:
                path = "data/mcp_servers.json"
        self.path = Path(path)

    # -- 读写 --------------------------------------------------------------- #
    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(raw, dict):
            servers = raw.get("servers")
        else:
            servers = raw
        return [s for s in (servers or []) if isinstance(s, dict)]

    def _write_all(self, servers: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)  # 原子替换：写一半崩溃不毁配置

    def _unique_id(self, base: str, existing: set[str]) -> str:
        candidate, n = base, 2
        while candidate in existing:
            candidate = f"{base}-{n}"
            n += 1
        return candidate

    # -- 归一化 ------------------------------------------------------------- #
    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        return normalize_server(data)

    # -- CRUD --------------------------------------------------------------- #
    def list(self) -> list[dict[str, Any]]:
        return self._read_all()

    def get(self, server_id: str) -> Optional[dict[str, Any]]:
        for s in self._read_all():
            if s.get("id") == server_id:
                return s
        return None

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        servers = self._read_all()
        existing = {s.get("id") for s in servers}
        item = self._normalize(data)
        item["id"] = self._unique_id(_slugify(str(data.get("id") or item["name"])), existing)
        now = _now_iso()
        item["created_at"] = now
        item["updated_at"] = now
        servers.append(item)
        self._write_all(servers)
        return item

    def update(self, server_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        servers = self._read_all()
        for i, s in enumerate(servers):
            if s.get("id") != server_id:
                continue
            merged = {**s, **data}
            normalized = self._normalize(merged)
            normalized["id"] = server_id
            normalized["created_at"] = s.get("created_at") or _now_iso()
            normalized["updated_at"] = _now_iso()
            servers[i] = normalized
            self._write_all(servers)
            return normalized
        return None

    def set_enabled(self, server_id: str, enabled: bool) -> Optional[dict[str, Any]]:
        servers = self._read_all()
        for i, s in enumerate(servers):
            if s.get("id") != server_id:
                continue
            servers[i] = {**s, "enabled": bool(enabled), "updated_at": _now_iso()}
            self._write_all(servers)
            return servers[i]
        return None

    def delete(self, server_id: str) -> bool:
        servers = self._read_all()
        kept = [s for s in servers if s.get("id") != server_id]
        if len(kept) == len(servers):
            return False
        self._write_all(kept)
        return True


_STORE: MCPServerStore | None = None
_STORE_PATH: str | None = None


def get_mcp_server_store() -> MCPServerStore:
    """进程级单例；配置路径变化时重建（测试常切临时文件）。"""
    global _STORE, _STORE_PATH
    try:
        from ....config import get_settings

        path = str(getattr(get_settings(), "mcp_servers_path", "") or "data/mcp_servers.json")
    except Exception:
        path = "data/mcp_servers.json"
    if _STORE is None or _STORE_PATH != path:
        _STORE = MCPServerStore(path)
        _STORE_PATH = path
    return _STORE
