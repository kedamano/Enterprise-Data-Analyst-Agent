"""Feedback persistence via SQLite — user ratings + comments on analysis results.

Every completed/failed analysis can receive a single feedback record per
``session_id``.  The table is **upsertive** on ``session_id`` so re-rating
merely overwrites instead of duplicating.

All functions are safe to call from the request path: internally caught
exceptions are logged + swallowed, never bubbled up to the caller.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("da.feedback")


def _db_path() -> Path:
    """Return the SQLite database path (always under data/feedback/)."""
    base = Path("data") / "feedback"
    base.mkdir(parents=True, exist_ok=True)
    return base / "feedback.db"


def _conn() -> sqlite3.Connection:
    p = _db_path()
    conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
            comment TEXT,
            report_snippet TEXT,
            stage_breakdown TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(session_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fb_session ON feedback(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fb_rating ON feedback(rating)"
    )
    return conn


def submit_feedback(
    session_id: str,
    rating: int,
    comment: str = "",
    report_snippet: str = "",
    stage_breakdown: Optional[dict] = None,
) -> int:
    """提交 / 覆盖反馈。返回 row id。

    - rating ∉ {-1,1} → ValueError
    - comment 超 500 字 → 自动截断
    - 同 session_id 已存在 → 覆盖（upsert），不报错
    - 所有异常静默吞 + 打 logger.warning（不抛）→ 决不能因反馈接口问题打挂主流程
    """
    if rating not in (-1, 1):
        raise ValueError(f"rating 必须为 -1 或 1，收到 {rating}")
    # Defensive: don't trust callers to sanitize
    if len(comment) > 500:
        comment = comment[:500]
    if len(report_snippet) > 200:
        report_snippet = report_snippet[:200]

    stage_json = json.dumps(stage_breakdown, ensure_ascii=False) if stage_breakdown else None
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        conn = _conn()
        try:
            cur = conn.execute(
                """
                INSERT INTO feedback
                    (session_id, rating, comment, report_snippet, stage_breakdown, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    rating=excluded.rating,
                    comment=excluded.comment,
                    report_snippet=excluded.report_snippet,
                    stage_breakdown=excluded.stage_breakdown,
                    created_at=excluded.created_at
                """,
                (session_id, rating, comment, report_snippet, stage_json, now_iso),
            )
            conn.commit()
            rowid = cur.lastrowid or 0
            return rowid
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "反馈落库失败 session_id=%s rating=%s", session_id, rating, exc_info=True
        )
        return 0


def get_feedback(session_id: str) -> Optional[dict]:
    """读一条反馈（dict or None）。"""
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT * FROM feedback WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            sb = d.get("stage_breakdown")
            if sb:
                try:
                    d["stage_breakdown"] = json.loads(sb)
                except Exception:
                    pass
            return d
        finally:
            conn.close()
    except Exception:
        logger.warning("读取反馈失败 session_id=%s", session_id, exc_info=True)
        return None


def aggregate_feedback(since_ts: Optional[float] = None) -> dict:
    """聚合统计。

    since_ts 可选：只统计此时间戳之后（UTC epoch seconds）。
    """
    try:
        conn = _conn()
        try:
            if since_ts is not None:
                cutoff_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
                total_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE created_at >= ?",
                    (cutoff_iso,),
                ).fetchone()
                thumbs_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE created_at >= ? AND rating = 1",
                    (cutoff_iso,),
                ).fetchone()
                down_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE created_at >= ? AND rating = -1",
                    (cutoff_iso,),
                ).fetchone()
                recent_rows = conn.execute(
                    "SELECT session_id, rating, comment, report_snippet, created_at "
                    "FROM feedback WHERE created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 10",
                    (cutoff_iso,),
                ).fetchall()
                comment_rows = conn.execute(
                    "SELECT session_id, rating, comment, created_at "
                    "FROM feedback WHERE created_at >= ? AND comment IS NOT NULL AND comment != '' "
                    "ORDER BY created_at DESC",
                    (cutoff_iso,),
                ).fetchall()
            else:
                total_row = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()
                thumbs_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE rating = 1"
                ).fetchone()
                down_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE rating = -1"
                ).fetchone()
                recent_rows = conn.execute(
                    "SELECT session_id, rating, comment, report_snippet, created_at "
                    "FROM feedback ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
                comment_rows = conn.execute(
                    "SELECT session_id, rating, comment, created_at "
                    "FROM feedback WHERE comment IS NOT NULL AND comment != '' "
                    "ORDER BY created_at DESC"
                ).fetchall()

            total = (total_row["c"] if total_row else 0) or 0
            thumbs_up = (thumbs_row["c"] if thumbs_row else 0) or 0
            thumbs_down = (down_row["c"] if down_row else 0) or 0
            satisfaction_rate = round(thumbs_up / total, 3) if total > 0 else 0.0

            recent_10 = [dict(r) for r in (recent_rows or [])]
            comments_with_text = [dict(r) for r in (comment_rows or [])]

            return {
                "total": total,
                "thumbs_up": thumbs_up,
                "thumbs_down": thumbs_down,
                "satisfaction_rate": satisfaction_rate,
                "recent_10": recent_10,
                "comments_with_text": comments_with_text,
            }
        finally:
            conn.close()
    except Exception:
        logger.warning("聚合反馈统计失败 since_ts=%s", since_ts, exc_info=True)
        return {
            "total": 0,
            "thumbs_up": 0,
            "thumbs_down": 0,
            "satisfaction_rate": 0.0,
            "recent_10": [],
            "comments_with_text": [],
        }


def low_quality_sessions(
    since_ts: Optional[float] = None, min_rating: int = -1
) -> list[str]:
    """返回 rating=-1 的 session_id 列表（供 eval runner 自动提取 bad cases 入 eval set）。"""
    try:
        conn = _conn()
        try:
            if since_ts is not None:
                cutoff_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
                rows = conn.execute(
                    "SELECT session_id FROM feedback WHERE rating = ? AND created_at >= ? "
                    "ORDER BY created_at DESC",
                    (min_rating, cutoff_iso),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id FROM feedback WHERE rating = ? "
                    "ORDER BY created_at DESC",
                    (min_rating,),
                ).fetchall()
            return [r["session_id"] for r in rows]
        finally:
            conn.close()
    except Exception:
        logger.warning(
            "取 lower-quality sessions 失败 since_ts=%s min_rating=%s",
            since_ts, min_rating, exc_info=True,
        )
        return []
