"""Error Handling Protocol (spec §23).

Classifies tool failures so the orchestration can route them:

* RETRYABLE     – transient (timeout / connection / rate limit / locked db).
                  ``execute_tool`` retries these, bounded by ``max_tool_retries``.
* NON_RETRYABLE – deterministic (bad SQL / schema / params / unauthorized).
                  Fail fast; the Reflection stage decides REPLAN vs FAIL.

Unknown errors default to NON_RETRYABLE: never loop on what we don't
understand ("禁止无限重试").
"""
from __future__ import annotations

import re
from enum import Enum


class ErrorClass(str, Enum):
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"


# 瞬态故障特征（大小写不敏感，中英文皆可）
_RETRYABLE_PATTERNS = [
    r"timeout", r"timed?\s*out", r"执行超时",
    r"connection\s*(reset|refused|closed|error|aborted)",
    r"broken\s*pipe", r"reset\s*by\s*peer",
    r"rate\s*limit", r"too\s*many\s*requests", r"429",
    r"50[234]", r"service\s*unavailable", r"bad\s*gateway",
    r"temporarily\s*unavailable", r"暂时",
    r"database\s*is\s*locked", r"database\s*table\s*is\s*locked",
    r"deadlock", r"try\s*again",
]

# 确定性故障特征
_NON_RETRYABLE_PATTERNS = [
    r"no\s*such\s*(table|column)", r"unknown\s*column", r"undefined\s*column",
    r"syntax\s*error", r"parse\s*error",
    r"只读模式禁止", r"禁止导入模块", r"禁止动态执行", r"单条语句", r"缺少\s*\w+\s*参数",
    r"permission\s*denied", r"unauthorized", r"forbidden", r"access\s*denied",
    r"unknown\s*tool", r"未知工具", r"依赖步骤未完成",
    r"data\s*type\s*mismatch", r"out\s*of\s*range",
]

_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)
_NON_RETRYABLE_RE = re.compile("|".join(_NON_RETRYABLE_PATTERNS), re.IGNORECASE)


def classify_error(error: str | None) -> ErrorClass:
    """Classify a tool error message. Unknown errors → NON_RETRYABLE (safe)."""
    if not error:
        return ErrorClass.NON_RETRYABLE
    if _NON_RETRYABLE_RE.search(error):
        return ErrorClass.NON_RETRYABLE
    if _RETRYABLE_RE.search(error):
        return ErrorClass.RETRYABLE
    return ErrorClass.NON_RETRYABLE


# §23 确定性分支路由：Invalid Column/Table → Schema Search → Retry
_SCHEMA_ERROR_RE = re.compile(
    r"no\s*such\s*(table|column)|unknown\s*column|undefined\s*column", re.IGNORECASE
)


def is_schema_error(error: str | None) -> bool:
    """True if the error is a schema mismatch (wrong table/column name)."""
    return bool(error) and bool(_SCHEMA_ERROR_RE.search(error))
