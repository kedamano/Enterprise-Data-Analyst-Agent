"""D45：**两步授权（HITL）** —— 高危动作需二次确认。

缺口（`docs/对标企业级Gap.md` 五）
----------------------------------
> 缺 **两步授权 / 高危确认 HITL** 策略引擎。

现状是"要么全放、要么硬拦"：读库有只读守卫、写码有 AST + 沙箱、导出有路径白名单——
但**没有"这一步值得让人看一眼再放行"的机制**。而恰恰有几类动作在合规上需要人过目。

**默认关**（`HITL_ENABLED=false`）：既有 950+ 用例与本地开发行为**零影响**。

两条安全纪律（与 E4/02 脱敏同源）
--------------------------------
1. **失败即关闭（fail-closed）**：策略引擎自己出错时**判定"需要确认"**，而不是放行。
   安全控制与其他降级不同——不能"退化为可用"。
2. **未知动作默认需确认**：新动作在明确登记前默认走确认流程。
   默认放行会把"忘了登记"变成"静默开了口子"。

凭证用 ``secrets.token_urlsafe``：**不可从 session 推导**，否则调用方能自己伪造"已确认"。

流转与 CLARIFY 同构：pending 存 ``short_term``（跨请求、进程内/Redis 皆可），
确认后清除；**允许与拒绝都写审计**（只记拒绝无法复盘，同 AUTH/01 的取舍）。
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("da.security")

# 需要人工确认的动作 → 给**人看**的理由（要能回答"为什么值得看一眼"）
_RULES: dict[str, str] = {
    "export_raw": ("导出物**保留未脱敏的原始值**（E4/02 只约束进入 LLM 上下文的那一份），"
                   "对外分享前需要人工确认"),
    "deliver_python": ("交付的 Python 脚本由模型生成并在沙箱内执行，"
                       "属任意代码执行面，需要人工过目"),
    "masking_disabled": "关闭输出脱敏会让敏感值进入 LLM 上下文，需要人工确认",
}

_PENDING_KEY = "pending_confirmation"
_DEFAULT_AUDIT = Path("data/audit/hitl.jsonl")

# **明确登记为"无需确认"的低危动作**。
#
# 策略是"默认拒绝 + 显式放行"，而不是"默认放行 + 黑名单"：
# 前者在**忘记登记**时偏保守（多问一句），后者偏危险（静默开口子）。
# 所以调用点只需要问 `requires_confirmation(action)`，不必逐个维护危险清单。
_SAFE: frozenset[str] = frozenset({
    "run_sql_readonly", "schema_search", "dataset_profile", "knowledge_search",
})


@dataclass
class RiskAction:
    action: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def _enabled() -> bool:
    from ...config import get_settings

    return bool(getattr(get_settings(), "hitl_enabled", False))


def requires_confirmation(action: str, detail: Optional[dict] = None) -> Optional[RiskAction]:
    """该动作是否需要二次确认？``None`` = 可直接执行。

    **fail-closed**：任何内部异常都判定"需要确认"。安全控制不能 fail-open。
    """
    try:
        if not _enabled():
            return None
        if action in _SAFE:
            return None
        rules = _RULES
        if action in rules:
            return RiskAction(action=action, reason=rules[action], detail=dict(detail or {}))
        # 未登记动作 → 默认需确认（"默认拒绝 + 显式放行"，登记之前不放行）
        return RiskAction(
            action=action,
            reason=f"未经登记的动作 {action!r}，按最小权限默认要求人工确认"
                   f"（确认低危请加入 hitl._SAFE 并说明理由）",
            detail=dict(detail or {}))
    except Exception as exc:  # noqa: BLE001 - fail-closed
        logger.error("HITL 策略判定异常，按 fail-closed 处理：%s", exc)
        return RiskAction(action=action, reason=f"策略判定异常（fail-closed）: {exc}",
                          detail=dict(detail or {}))


def _audit_path() -> Path:
    from ...config import get_settings

    configured = getattr(get_settings(), "hitl_audit_log", "")
    return Path(configured) if configured else _DEFAULT_AUDIT


def _audit(session_id: str, action: str, decision: str, reason: str) -> None:
    """审计失败**绝不打断主流程**（与工具审计同一条纪律）。"""
    try:
        from .audit_store import record as _store_record

        entry = {"ts": datetime.now(timezone.utc).isoformat(), "session": session_id,
                 "action": action, "decision": decision, "reason": reason}
        # D46：统一经审计存储（默认仍写 hitl.jsonl；可切 sqlite/postgres）
        _store_record("hitl", entry, path=_audit_path())
    except Exception:  # pragma: no cover - 审计故障不得影响业务
        logger.warning("HITL 审计写入失败（已忽略）", exc_info=True)


def begin(session_id: str, action: str, detail: Optional[dict] = None) -> dict:
    """挂起一个待确认动作（返回 pending 结构，含**不可推导**的凭证）。"""
    from ..memory import short_term

    pending = {
        "action": action,
        "token": secrets.token_urlsafe(24),
        "detail": dict(detail or {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    short_term.put(session_id, _PENDING_KEY, pending)
    return pending


def get_pending(session_id: str) -> Optional[dict]:
    from ..memory import short_term

    pending = short_term.get(session_id, _PENDING_KEY)
    return pending if isinstance(pending, dict) and pending.get("token") else None


def decide(session_id: str, token: str, *, approved: bool) -> tuple[bool, str]:
    """校验凭证并落定 → ``(是否放行, 说明)``。**允许与拒绝都写审计**。"""
    from ..memory import short_term

    pending = get_pending(session_id)
    if not pending:
        _audit(session_id, "?", "DENY", "没有待确认的动作")
        return False, "没有待确认的动作"
    action = str(pending.get("action") or "?")

    # 凭证必须**常量时间比较**：普通 == 会因短路而泄露前缀信息
    if not secrets.compare_digest(str(token or ""), str(pending.get("token") or "")):
        _audit(session_id, action, "DENY", "凭证不匹配（可能被伪造）")
        return False, "凭证不匹配：拒绝"

    if not approved:
        short_term.put(session_id, _PENDING_KEY, None)
        _audit(session_id, action, "DENY", "人工拒绝")
        return False, f"已拒绝 {action}"

    short_term.put(session_id, _PENDING_KEY, None)
    # **一次性放行**：确认之后必须让"同一个动作的下一次尝试"能过，
    # 否则调用方重试会被反复拦住 → 死循环。
    # 用一次性（取走即失效）而不是长期开关：确认的是**这一次**，不是永久授权。
    grants = _grants(session_id)
    grants[action] = True
    short_term.put(session_id, _GRANTS_KEY, grants)
    _audit(session_id, action, "ALLOW", "人工确认")
    return True, f"已确认 {action}"


def _grant_key(action: str) -> str:
    return _GRANTS_KEY


_GRANTS_KEY = "confirm_grants"


def _grants(session_id: str) -> dict:
    from ..memory import short_term

    value = short_term.get(session_id, _GRANTS_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


def take_grant(session_id: str, action: str) -> bool:
    """取走一次性放行（**取走即失效**）。没有则返回 False。

    所有放行存在**同一个键**（`confirm_grants` 字典）里而不是按动作铺开：
    键会随动作数增长，且测试/运维无法一次清干净（实测被上一个用例的授权漏进来污染）。
    """
    from ..memory import short_term

    grants = _grants(session_id)
    if not grants.pop(action, False):
        return False
    short_term.put(session_id, _GRANTS_KEY, grants or None)
    return True


def reset(session_id: str) -> None:
    """清掉该会话的 pending 与放行（测试/运维用）。"""
    from ..memory import short_term

    short_term.put(session_id, _PENDING_KEY, None)
    short_term.put(session_id, _GRANTS_KEY, None)
