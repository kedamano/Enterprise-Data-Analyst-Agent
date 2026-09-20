"""Multi-level LLM usage budget — session / user-daily / tenant-monthly.

Storage: SQLite at ``data/budget/budget.db`` (stdlib only — no new deps).

Guarantees:
* Fail-open — any internal error (DB missing, SQL error, ...) budgets are treated
  as OK so that **analysis never breaks because of the budget module**.
* Rate-limit: two ``record()`` calls for the same session_id within 100 ms are
  merged (tokens added, last caller's metadata wins) to survive concurrent
  SQLite writes.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from ...config import Settings, get_settings

logger = logging.getLogger("da.budget")

BudgetLevel = Literal["OK", "WARN", "EXCEEDED"]


@dataclass
class BudgetDecision:
    ok: bool
    level: BudgetLevel
    current: int       # tokens already consumed
    limit: int         # ceiling
    ratio: float       # current / limit
    reason: str        # "session" / "user_daily" / "tenant_monthly"
    suggested_model: Optional[str] = None  # populated on EXCEEDED + degrade policy


class BudgetExceededError(RuntimeError):
    """Raised when a budget level is EXCEEDED and policy=deny."""


# --------------------------------------------------------------------------- #
# SQL schema
# --------------------------------------------------------------------------- #
_SQL_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    stage TEXT,
    created_at TEXT NOT NULL
);
"""

_SQL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_events(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_events(user_id, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage_events(tenant_id, created_at);",
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tenant_default(settings: Settings) -> str:
    return (settings.default_tenant or "").strip() or "_default"


class BudgetManager:
    """Three-tier LLM token budget: session / user-daily / tenant-monthly.

    Parameters override ``Settings`` at construction — tests can pass small limits
    without touching global config.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        db_path: Optional[str] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))))),
            "data", "budget", "budget.db",
        )
        # in-flight dedup buffer: session_id -> (user_id, tenant_id, prompt_tokens, completion_tokens, cost_usd, stage, timestamp)
        self._pending: dict[str, tuple[str, str, int, int, float, Optional[str], float]] = {}
        self._lock = threading.Lock()
        self._init_db()

    # ------------------------------------------------------------------ #
    # DB helpers
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite.connect:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            with self._connect() as conn:
                conn.execute(_SQL_CREATE_TABLE)
                for stmt in _SQL_INDEXES:
                    conn.execute(stmt)
        except Exception:
            logger.exception("budget: DB init failed — budgets will be fail-open")

    # ------------------------------------------------------------------ #
    # Core: record
    # ------------------------------------------------------------------ #
    def record(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        stage: Optional[str] = None,
    ) -> None:
        """Record a usage event. Coalesced within 100 ms per session_id."""
        if not self._settings.budget_enabled:
            return
        now = time.monotonic()
        with self._lock:
            if session_id in self._pending:
                prev = self._pending[session_id]
                if now - prev[6] < 0.1:  # < 100 ms → merge
                    self._pending[session_id] = (
                        prev[0],  # user_id preserved
                        prev[1],  # tenant_id preserved
                        prev[2] + int(prompt_tokens),
                        prev[3] + int(completion_tokens),
                        prev[4] + float(cost_usd),
                        stage,  # last caller's stage wins
                        now,    # refresh timestamp
                    )
                    return
            # flush existing pending first (if any)
            self._flush_unlocked(session_id)
            # keep in _pending — actual flush happens on flush() or next merge window
            self._pending[session_id] = (
                user_id, tenant_id,
                int(prompt_tokens), int(completion_tokens), float(cost_usd), stage, now,
            )

    def flush(self) -> None:
        """Force-write all pending events to the DB."""
        with self._lock:
            for sid in list(self._pending):
                self._flush_unlocked(sid)

    def _flush_unlocked(self, session_id: str) -> None:
        if session_id not in self._pending:
            return
        entry = self._pending.pop(session_id)
        # entry = (user_id, tenant_id, prompt_tokens, completion_tokens, cost_usd, stage, ts)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO usage_events "
                    "(session_id, user_id, tenant_id, prompt_tokens, completion_tokens,"
                    " cost_usd, stage, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, entry[0], entry[1],
                     entry[2], entry[3], entry[4], entry[5], _iso_now()),
                )
        except Exception:
            logger.exception("budget: record flush failed (fail-open)")

    # ------------------------------------------------------------------ #
    # Aggregation helpers
    # ------------------------------------------------------------------ #
    def _sum_tokens_since(self, column: str, key: str, since_iso: str) -> int:
        """Sum prompt_tokens + completion_events for ``column=key`` since timestamp."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                    f"FROM usage_events WHERE {column}=? AND created_at >= ?",
                    (key, since_iso),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            logger.exception("budget: aggregation failed (fail-open → 0)")
            return 0

    def _session_total(self, session_id: str) -> int:
        """All-time tokens for this session (session budgets are per-run, not time-windowed)."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                    "FROM usage_events WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # Check methods — all fail-open (return OK on error)
    # ------------------------------------------------------------------ #
    def check_session(self, session_id: str, estimated_prompt_tokens: int = 0) -> BudgetDecision:
        limit = self._settings.budget_per_session_tokens
        if not self._settings.budget_enabled or limit <= 0:
            return BudgetDecision(ok=True, level="OK", current=0, limit=0, ratio=0.0, reason="session")
        try:
            current = self._session_total(session_id)
            return self._decision(current, limit, "session", session_id)
        except Exception:
            logger.exception("budget: check_session failed (fail-open)")
            return BudgetDecision(ok=True, level="OK", current=0, limit=limit, ratio=0.0, reason="session")

    def check_user_daily(self, user_id: str, estimated: int = 0) -> BudgetDecision:
        limit = self._settings.budget_per_user_daily_tokens
        if not self._settings.budget_enabled or limit <= 0:
            return BudgetDecision(ok=True, level="OK", current=0, limit=0, ratio=0.0, reason="user_daily")
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            since = today + "T00:00:00+00:00"
            current = self._sum_tokens_since("user_id", user_id, since)
            return self._decision(current, limit, "user_daily", user_id)
        except Exception:
            logger.exception("budget: check_user_daily failed (fail-open)")
            return BudgetDecision(ok=True, level="OK", current=0, limit=limit, ratio=0.0, reason="user_daily")

    def check_tenant_monthly(self, tenant_id: str, estimated: int = 0) -> BudgetDecision:
        limit = self._settings.budget_per_tenant_monthly_tokens
        if not self._settings.budget_enabled or limit <= 0:
            return BudgetDecision(ok=True, level="OK", current=0, limit=0, ratio=0.0, reason="tenant_monthly")
        try:
            month_start = datetime.now(timezone.utc).strftime("%Y-%m")
            since = month_start + "-01T00:00:00+00:00"
            current = self._sum_tokens_since("tenant_id", tenant_id, since)
            return self._decision(current, limit, "tenant_monthly", tenant_id)
        except Exception:
            logger.exception("budget: check_tenant_monthly failed (fail-open)")
            return BudgetDecision(ok=True, level="OK", current=0, limit=limit, ratio=0.0, reason="tenant_monthly")

    def _decision(
        self, current: int, limit: int, reason: str, _key: str = "",
    ) -> BudgetDecision:
        ratio = current / limit if limit > 0 else 0.0
        if ratio >= 1.0:
            level: BudgetLevel = "EXCEEDED"
        elif ratio >= 0.8:
            level = "WARN"
        else:
            level = "OK"
        suggested = None
        if level == "EXCEEDED" and self._settings.budget_overrun_policy == "degrade":
            suggested = self._suggest_degraded_model()
        return BudgetDecision(
            ok=level != "EXCEEDED",
            level=level,
            current=current,
            limit=limit,
            ratio=round(ratio, 4),
            reason=reason,
            suggested_model=suggested,
        )

    # ------------------------------------------------------------------ #
    # Degrade / usage summary
    # ------------------------------------------------------------------ #
    def suggest_degraded_model(self, user_id: str = "", tenant_id: str = "") -> Optional[str]:
        return self._suggest_degraded_model()

    def _suggest_degraded_model(self) -> Optional[str]:
        policy = self._settings.budget_overrun_policy
        if policy == "degrade":
            # prefer explicit config, otherwise fall back to configured llm_fallback_models
            if self._settings.budget_degraded_model:
                return self._settings.budget_degraded_model
            fallbacks = list(self._settings.llm_fallback_models or [])
            if fallbacks:
                fb = fallbacks[0]
                if isinstance(fb, dict):
                    return fb.get("model")
                return str(fb)
        return None

    def get_usage_summary(self, user_id: str, tenant_id: str) -> dict:
        """Return current usage vs. limits for front-end progress bars."""
        tenant_id = tenant_id or _tenant_default(self._settings)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        since_user = today + "T00:00:00+00:00"
        month_start = datetime.now(timezone.utc).strftime("%Y-%m")
        since_tenant = month_start + "-01T00:00:00+00:00"
        try:
            user_daily = self._sum_tokens_since("user_id", user_id, since_user)
        except Exception:
            user_daily = 0
        try:
            tenant_monthly = self._sum_tokens_since("tenant_id", tenant_id, since_tenant)
        except Exception:
            tenant_monthly = 0
        return {
            "session_ratio": 0.0,  # not exposed in summary (per-session is run-scoped)
            "user_daily_ratio": round(user_daily / self._settings.budget_per_user_daily_tokens, 4)
            if self._settings.budget_per_user_daily_tokens > 0 else 0.0,
            "tenant_monthly_ratio": round(tenant_monthly / self._settings.budget_per_tenant_monthly_tokens, 4)
            if self._settings.budget_per_tenant_monthly_tokens > 0 else 0.0,
            "user_daily_used": user_daily,
            "user_daily_limit": self._settings.budget_per_user_daily_tokens,
            "tenant_monthly_used": tenant_monthly,
            "tenant_monthly_limit": self._settings.budget_per_tenant_monthly_tokens,
            "overrun_policy": self._settings.budget_overrun_policy,
        }


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #
_budget_manager: Optional[BudgetManager] = None
_budget_lock = threading.Lock()


def get_budget_manager(settings: Optional[Settings] = None) -> BudgetManager:
    global _budget_manager
    if _budget_manager is None:
        with _budget_lock:
            if _budget_manager is None:
                _budget_manager = BudgetManager(settings=settings)
    return _budget_manager


def reset_budget_manager() -> None:
    """Test helper: clear the singleton so a fresh instance is built on next call."""
    global _budget_manager
    _budget_manager = None
