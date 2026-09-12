"""AUTH/01 用户级鉴权与数据权限。

Spec: docs/specs/AUTH/01-user-auth-and-data-permissions.md

现状是**零 API 鉴权**：任何能访问端口的人都能查全部数据、下载任意会话的导出。
已有的权限只到**工具级**（`ToolSpec.permission` + 高危工具 + 审计 + 限流），
缺"谁在调、能看哪些表/列/行、能不能读别人的会话"这三问。

设计要点：
- **默认关**（`AUTH_ENABLED=false`）→ 匿名 principal、权限全开：既有的本地开发与
  600+ 用例行为完全不变；开了才强制 key。
- 身份用 `X-API-Key` 查 `AUTH_KEYS`；**开了但没配 key → 业务端点 503**（配置错误要吵，
  绝不静默放开）。
- 行/列/表三层权限**全部确定性**、在工具执行前拦下，不经过 LLM。
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from ...config import get_settings
from ..tools.specs import roles_to_permissions, ToolPermission

logger = logging.getLogger("da.auth")

AUTH_AUDIT_LOG = Path("data/audit/auth.jsonl")

# 当前请求的 principal（API 依赖设置，工具层读取）——与 tracing.current_run_id 同一范式
_current_principal: contextvars.ContextVar = contextvars.ContextVar("da_principal", default=None)

_ANONYMOUS = "anonymous"


class Principal(BaseModel):
    """调用方身份与数据权限。字段语义见规格 §1。"""

    user_id: str = _ANONYMOUS
    tenant: str = ""
    roles: list[str] = Field(default_factory=list)
    allowed_tables: list[str] = Field(default_factory=list)   # 空 = 不限
    denied_columns: list[str] = Field(default_factory=list)   # 列级黑名单（按列名匹配）
    row_filters: dict[str, str] = Field(default_factory=dict)  # 表 → 条件片段
    quota_per_min: int = 0                                     # 0 = 不限

    @property
    def is_anonymous(self) -> bool:
        return self.user_id == _ANONYMOUS

    @field_validator("row_filters")
    @classmethod
    def _validate_filters(cls, v: dict[str, str]) -> dict[str, str]:
        """过滤片段由配置方提供，但**仍要过白名单**——否则等于把 SQL 注入写进配置。"""
        for table, cond in (v or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table or "")):
                raise ValueError(f"row_filters 表名非法: {table!r}")
            err = validate_filter_fragment(str(cond or ""))
            if err:
                raise ValueError(f"row_filters[{table}] {err}")
        return v


# 过滤片段里绝不允许出现的东西（违规即启动报错，不静默）
_FILTER_FORBIDDEN = (
    re.compile(r";"),                      # 多语句
    re.compile(r"--|/\*|\*/"),             # 注释
    re.compile(r"\b(select|insert|update|delete|drop|union|attach)\b", re.IGNORECASE),
)


def validate_filter_fragment(cond: str) -> Optional[str]:
    """返回错误信息或 None。空片段合法（等于不过滤）。"""
    text = (cond or "").strip()
    if not text:
        return None
    for pattern in _FILTER_FORBIDDEN:
        if pattern.search(text):
            return f"含禁止内容 {pattern.pattern!r}（只允许简单谓词，如 region_id IN (1,2)）"
    if text.count("(") != text.count(")"):
        return "括号不配平"
    return None


def parse_keys(raw: str) -> list[tuple[str, Principal]]:
    """把 `AUTH_KEYS` 解析成 [(key, principal)]。坏配置 → 抛（启动即暴露）。"""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AUTH_KEYS 不是合法 JSON: {exc}") from exc
    if not isinstance(items, list):
        raise ValueError("AUTH_KEYS 必须是数组")
    out: list[tuple[str, Principal]] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("key"):
            raise ValueError("AUTH_KEYS 每项必须含 key")
        key = str(item["key"])
        payload = {k: v for k, v in item.items() if k != "key"}
        out.append((key, Principal.model_validate(payload)))
    return out


def enabled() -> bool:
    return bool(getattr(get_settings(), "auth_enabled", False))


def effective_permissions(principal: "Principal") -> set[str]:
    """该 principal 经 RBAC 展开后的**权限字符串集合**（用于工具级门禁）。

    `Principal.roles` 会被展开为 `ToolPermission` 并集；鉴权关闭时调用方不应使用
    本函数（门禁整体跳过，保持既有全开行为）。未知角色被 `roles_to_permissions` 忽略。
    """
    perms = roles_to_permissions(principal.roles)
    return {p.value for p in perms}


def anonymous_principal() -> Principal:
    return Principal(user_id=_ANONYMOUS, tenant=str(getattr(get_settings(), "auth_anonymous_tenant", "") or ""))


def resolve_key(api_key: Optional[str]) -> Optional[Principal]:
    """key → Principal；未知 key 返回 None（**不回显差异**，防枚举）。"""
    if not api_key:
        return None
    for key, principal in parse_keys(getattr(get_settings(), "auth_keys", "") or ""):
        if api_key == key:
            return principal
    return None


# --------------------------------------------------------------------------- #
# 配额（per principal，滑动窗口）
# --------------------------------------------------------------------------- #
_QUOTA: dict[str, list[float]] = defaultdict(list)


def check_quota(principal: Principal, *, now: Optional[float] = None) -> bool:
    """是否还在配额内（超了返回 False）。`quota_per_min<=0` 表示不限。"""
    if principal.quota_per_min <= 0:
        return True
    ts = now if now is not None else time.time()
    window = _QUOTA[principal.user_id]
    while window and ts - window[0] > 60.0:
        window.pop(0)
    if len(window) >= principal.quota_per_min:
        return False
    window.append(ts)
    return True


def reset_quota() -> None:
    _QUOTA.clear()


# --------------------------------------------------------------------------- #
# 审计（ALLOW 与 DENY 都记 —— 只记拒绝无法复盘）
# --------------------------------------------------------------------------- #
def audit(principal: Optional[Principal], endpoint: str, decision: str,
          reason: str = "", session_id: str = "") -> None:
    try:
        AUTH_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user_id": getattr(principal, "user_id", _ANONYMOUS),
            "tenant": getattr(principal, "tenant", ""),
            "endpoint": endpoint,
            "decision": decision,
            "reason": reason,
            "session_id": session_id,
        }
        with AUTH_AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 审计失败绝不打断请求


# --------------------------------------------------------------------------- #
# 请求上下文
# --------------------------------------------------------------------------- #
def set_current(principal: Optional[Principal]):
    return _current_principal.set(principal)


def reset_current(token) -> None:
    try:
        _current_principal.reset(token)
    except Exception:
        pass


def current_principal() -> Principal:
    """当前请求的 principal；无上下文（离线脚本/单测）→ 匿名全权限。"""
    return _current_principal.get() or anonymous_principal()


# --------------------------------------------------------------------------- #
# 会话归属（防越权读别人的会话）
# --------------------------------------------------------------------------- #
def _owner_key(session_id: str) -> str:
    from ..memory import short_term

    return short_term.get(f"owner:{session_id}", "principal", None)


def record_session_owner(session_id: str, principal: Principal) -> None:
    if not session_id or not enabled():
        return
    try:
        from ..memory import short_term

        short_term.put(f"owner:{session_id}", "principal",
                       {"user_id": principal.user_id, "tenant": principal.tenant})
    except Exception:
        pass


def owns_session(session_id: str, principal: Principal) -> bool:
    """AUTH 关闭 → 不校验（保持既有行为）；开启 → 必须匹配，历史无归属的会话**拒绝**。"""
    if not enabled():
        return True
    owner = _owner_key(session_id)
    if not owner:
        return False  # 宁可吵，不默认放开
    return (owner.get("user_id") == principal.user_id
            and (owner.get("tenant") or "") == (principal.tenant or ""))
