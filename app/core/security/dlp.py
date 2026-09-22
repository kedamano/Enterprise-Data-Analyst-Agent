"""E4/06 DLP 细粒度脱敏 — 角色/字段级策略 — D49。

Spec: docs/specs/E4/06-dlp-field-level.md §1

E4/02 的脱敏是**全局一档**（`mask_level` 一个值管所有人），且只约束"进 LLM 上下文"
的那一份。这里把脱敏做成**角色×字段**：不同角色对同一列看到不同的级别。

两条纪律
--------
1. **权限高于策略**：`Principal.denied_columns` 永远 ``strict``——权限模型已声明
   "不可见"的字段，角色策略不得放宽（否则等于用策略绕过鉴权）。
2. **默认零影响**：`DLP_POLICY` 为空 → 回退全局 `mask_level`，导出行为与 E4/02 一字不变。

解析异常一律**回退全局**（不猜、不 fail-open）并记 warning。
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Callable, Optional

from .masking import is_sensitive_column, looks_like_pii, mask_value

logger = logging.getLogger("da.dlp")

LEVELS = ("none", "sample", "strict")


def _settings() -> Any:
    from ...config import get_settings

    return get_settings()


def _global_level() -> str:
    lvl = str(getattr(_settings(), "mask_level", "sample") or "sample").lower()
    return lvl if lvl in LEVELS else "sample"


def policy_cfg() -> dict[str, dict]:
    """读 ``DLP_POLICY``；空 / 非法 JSON → ``{}``（不激活，回退全局）。"""
    raw = str(getattr(_settings(), "dlp_policy", "") or "").strip()
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("DLP_POLICY 不是合法 JSON，已忽略（回退全局 mask_level）")
        return {}
    return cfg if isinstance(cfg, dict) else {}


def policy_active() -> bool:
    """策略是否激活（导出端据此决定 `masked` 的缺省值）。"""
    return bool(policy_cfg())


def _match(denied: Any, column: str) -> bool:
    if not column:
        return False
    col = column.lower()
    return any(str(d).lower() == col for d in (denied or []))


def resolve_level(principal: Any, column: str) -> str:
    """该角色对某列的生效脱敏级别。解析优先级见模块 docstring。"""
    try:
        col = str(column or "")
        # ① 权限高于策略
        if _match(getattr(principal, "denied_columns", None), col):
            return "strict"

        cfg = policy_cfg()
        role_cfg = None
        for role in (getattr(principal, "roles", None) or []):
            rc = cfg.get(role)
            if rc is not None:
                role_cfg = rc
                break

        if isinstance(role_cfg, dict):
            # ② 角色 deny_columns → 强制剔除
            if _match(role_cfg.get("deny_columns"), col):
                return "strict"
            # ③ column_levels：最长 key 优先（`customer_phone` 胜过 `phone`）
            best_key: Optional[str] = None
            best_level: Optional[str] = None
            levels = role_cfg.get("column_levels") or {}
            if isinstance(levels, dict):
                for key, lvl in levels.items():
                    if not key or not col:
                        continue
                    if str(key).lower() in col.lower():
                        if best_key is None or len(str(key)) > len(str(best_key)):
                            best_key, best_level = str(key), str(lvl)
            if best_level in LEVELS:
                return best_level
            # ④ 角色 default_level
            default = str(role_cfg.get("default_level") or "")
            if default in LEVELS:
                return default

        # ⑤ 回退全局（角色未登记 / 策略为空 / 值非法）
        return _global_level()
    except Exception as exc:
        logger.warning("DLP 级别解析异常，回退全局 mask_level：%s", exc)
        return _global_level()


def make_resolver(principal: Any) -> Callable[[str], str]:
    """把 principal 绑成一个 ``resolve(column) -> level`` 的闭包（便于传入 CSV 脱敏）。"""
    return lambda col: resolve_level(principal, col)


def mask_csv_text(text: str, resolve: Callable[[str], str]) -> str:
    """按**每列独立级别**脱敏 CSV 文本。

    - ``strict`` → 整列剔除（**连表头都不出现**，与 E4/02 strict 同语义）
    - ``sample`` → 敏感列或值形如 PII 的按值掩码；其余原样
    - ``none``   → 原样
    """
    if not text:
        return text or ""
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    keep = [c for c in fieldnames if resolve(c) != "strict"]
    out: list[dict] = []
    for row in reader:
        new: dict[str, Any] = {}
        for c in keep:
            val = row.get(c)
            lvl = resolve(c)
            if lvl == "sample" and val is not None and (is_sensitive_column(c) or looks_like_pii(val)):
                new[c] = mask_value(val)
            else:
                new[c] = val
        out.append(new)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=keep, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(out)
    return buf.getvalue()
