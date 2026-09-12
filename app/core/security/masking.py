"""E4/02 输出脱敏 — 把"能不能进 LLM 上下文"变成可配置、可测、可审计的边界。

Spec: docs/specs/E4/02-masking.md

手机号/邮箱/身份证/姓名/地址一旦进 LLM 上下文，就等于**出境且不可撤回**。
本模块是**单一收口**：所有工具结果在进上下文前过一遍（`tools._to_result`），
而 `csv_path`/`artifacts`（分析师本机产物）**不脱敏**。

与 `SEMANTIC/01` 共用同一份敏感列模式表（`is_sensitive_column` 即 `semantics.is_pii_column`），
避免两处规则漂移——维表枚举与 profile enums 都复用这里。

默认开。**失败即关闭**：脱敏出错时宁可丢掉行样本，也不把原始数据发出去。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ...config import get_settings

# 敏感列模式（唯一来源；semantics 委托到这里）
SENSITIVE_COLUMN_RE = re.compile(
    r"(phone|mobile|tel|email|mail|id_card|idcard|passport|address|birth|"
    r"(user|customer|client|real|full|contact|person|employee|member|staff|patient)_?name|"
    r"姓名|名字|手机|电话|邮箱|身份证|地址|生日|住址)",
    re.IGNORECASE,
)
# 值形态的 PII（列名可能很"无辜"，如 `remark` / `note` / `value`）
_VALUE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^1[3-9]\d{9}$"), "phone"),
    (re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$"), "email"),
    (re.compile(r"^\d{17}[\dXx]$"), "id_card"),
    (re.compile(r"^\d{16,19}$"), "bank_card"),
)
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

MASKING_AUDIT_LOG = Path("data/audit/masking.jsonl")
LEVELS = ("none", "sample", "strict")


def is_sensitive_column(name: str) -> bool:
    """列名是否敏感（E4/02 与 SEMANTIC/01 的唯一判断入口）。"""
    return bool(SENSITIVE_COLUMN_RE.search(str(name or "")))


def looks_like_pii(value: Any) -> bool:
    """值本身是否形如 PII（列名没命中时的兜底）。"""
    if not isinstance(value, str):
        return False
    v = value.strip()
    return any(p.match(v) for p, _ in _VALUE_PATTERNS)


def _kind_of(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    v = value.strip()
    for pattern, kind in _VALUE_PATTERNS:
        if pattern.match(v):
            return kind
    return None


def mask_value(value: Any) -> str:
    """确定性掩码：电话留前 3 后 4、邮箱留首字母与域名、其余等长星号（不泄漏长度）。

    刻意**不做哈希**：低基数下哈希可反推（如性别/地区），且对模型无信息增益。
    """
    if value is None:
        return ""
    text = str(value)
    kind = _kind_of(text)
    if kind == "phone" and len(text) >= 7:
        return f"{text[:3]}****{text[-4:]}"
    if kind == "email":
        local, _, domain = text.partition("@")
        return f"{(local[:1] or '*')}***@{domain}"
    if kind in ("id_card", "bank_card") and len(text) > 6:
        return f"{text[:3]}{'*' * (len(text) - 7)}{text[-4:]}"
    return "***"


def _mask_embedded(text: str) -> str:
    """把字符串**内部**出现的电话/邮箱也掩掉（如备注列里夹着一个手机号）。"""
    out = _PHONE_RE.sub(lambda m: mask_value(m.group(0)), text)
    out = _EMAIL_RE.sub(lambda m: mask_value(m.group(0)), out)
    return out


def mask_rows(rows: Sequence[Any], *, level: str = "sample",
              extra_columns: Iterable[str] = ()) -> tuple[list[Any], list[str]]:
    """按级别脱敏样本行。返回 ``(新行, 被脱敏的列名)``。

    - ``sample``：敏感列值替换为掩码；列名未命中但值形如 PII 的**也**掩（兜底）
    - ``strict``：敏感列**整列剔除**（连列名都不出现）
    - 数值列不动（除非显式列入 ``extra_columns``）
    """
    extra = {str(c) for c in (extra_columns or [])}
    masked_cols: set[str] = set()
    out: list[Any] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        if "_truncated" in row:  # 上下文预算哨兵，不是数据
            out.append(row)
            continue
        new: dict[str, Any] = {}
        for col, val in row.items():
            column_hit = is_sensitive_column(col) or str(col) in extra
            value_hit = looks_like_pii(val)
            if level == "strict" and column_hit:
                masked_cols.add(str(col))
                continue  # 整列剔除
            if column_hit:
                masked_cols.add(str(col))
                new[col] = mask_value(val)
            elif value_hit:
                masked_cols.add(str(col))
                new[col] = mask_value(val)
            elif isinstance(val, str) and (_PHONE_RE.search(val) or _EMAIL_RE.search(val)):
                masked_cols.add(str(col))
                new[col] = _mask_embedded(val)
            else:
                new[col] = val
        out.append(new)
    return out, sorted(masked_cols)


def mask_structured(output: dict, *, level: str,
                    extra_columns: Iterable[str] = ()) -> tuple[dict, list[str]]:
    """脱敏结构化输出：`rows` / `enums` / `columns`（profile）三类。

    - `enums`（E4/01 + SEMANTIC/01）里敏感列的**取值**同样要掩 —— 枚举值本身是数据。
    - `strict`：`columns` 里也不给 `distinct`（防止"只有 1 个不同值"反推）。
    """
    if not isinstance(output, dict) or level == "none":
        return output, []
    extra = {str(c) for c in (extra_columns or ())}
    masked: set[str] = set()

    if isinstance(output.get("rows"), list):
        rows, cols = mask_rows(output["rows"], level=level, extra_columns=extra)
        output["rows"] = rows
        masked |= set(cols)

    enums = output.get("enums")
    if isinstance(enums, dict):
        new_enums: dict[str, Any] = {}
        for col, values in enums.items():
            if is_sensitive_column(col) or str(col) in extra:
                masked.add(str(col))
                if level == "strict":
                    continue
                new_enums[col] = [mask_value(v) for v in (values or [])]
            else:
                new_enums[col] = values
        output["enums"] = new_enums

    cols_meta = output.get("columns")
    if isinstance(cols_meta, dict):
        for col in list(cols_meta.keys()):
            if is_sensitive_column(col) or str(col) in extra:
                masked.add(str(col))
                if level == "strict":
                    cols_meta.pop(col, None)  # 连 distinct 都不给
    return output, sorted(masked)


def _audit(session_id: str, tool: str, step_id: str, columns: list[str], level: str) -> None:
    """审计留痕（含"显式关闭脱敏"这一事实）。"""
    try:
        MASKING_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "session_id": session_id,
                  "tool": tool, "step_id": step_id, "level": level,
                  "columns_masked": columns}
        with MASKING_AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass  # 审计失败绝不打断工具调用


def apply_masking(output: dict, *, tool: str, step_id: str, session_id: str = "",
                  level: Optional[str] = None) -> tuple[dict, list[str]]:
    """单一收口：工具结果进上下文前调用。

    配置关闭（`mask_pii_enabled=false` 或 `mask_level=none`）→ 不脱敏，但**记一条审计**
    （"关闭"这件事本身必须留痕）。**脱敏出错则失败即关闭**：丢掉行样本，绝不放行原始数据。
    """
    settings = get_settings()
    enabled = bool(getattr(settings, "mask_pii_enabled", True))
    lvl = str(level or getattr(settings, "mask_level", "sample") or "sample").lower()
    if lvl not in LEVELS:
        lvl = "sample"

    if not enabled or lvl == "none":
        _audit(session_id, tool, step_id, [], "none" if enabled else "disabled")
        return output, []

    extra = [c.strip() for c in
             str(getattr(settings, "mask_pii_columns", "") or "").split(",") if c.strip()]
    try:
        masked_out, cols = mask_structured(output, level=lvl, extra_columns=extra)
    except Exception as exc:  # **失败即关闭**：隐私控制不允许 fail-open
        _audit(session_id, tool, step_id, ["<masking_error>"], lvl)
        if isinstance(output, dict):
            output["rows"] = []
            output["masking_error"] = str(exc)[:200]
        return output, []
    if cols:
        _audit(session_id, tool, step_id, cols, lvl)
        masked_out["masked_columns"] = cols  # 观测：本次掩了哪些列
    return masked_out, cols
