"""Workflow Job 调度模块（MVP）。
设计取舍：
- 调度：threading.Timer（单进程内，零外部依赖）；多 worker 期可换 ARQ/Celery
- 持久化：SQLite (data/jobs/jobs.db)；重启恢复 job 列表
- 模板：{{today}} / {{last_month}} 占位符渲染
- webhook：fire-and-forget；失败 swallow 不污染 job 记录
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DB_PATH: str = ""  # 由 init_db_path 在 app startup 注入
_SCHEMA_ENSURED = False


def init_db_path(path: str) -> None:
    global _DB_PATH, _SCHEMA_ENSURED
    _DB_PATH = path
    _SCHEMA_ENSURED = False
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)


def _db() -> sqlite3.Connection:
    if not _DB_PATH:
        raise RuntimeError("Job DB path not initialized — call init_db_path() first")
    global _SCHEMA_ENSURED
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if not _SCHEMA_ENSURED:
        _ensure_schema(conn)
        _SCHEMA_ENSURED = True
    return conn


def _ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                owner_sub TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                query_template TEXT NOT NULL,
                schedule TEXT,
                run_once_at TEXT,
                next_fire_at TEXT,
                webhook_url TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                last_status TEXT,
                last_error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner_sub);
            CREATE INDEX IF NOT EXISTS idx_jobs_tenant ON jobs(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_next_fire ON jobs(enabled, next_fire_at);
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                error TEXT,
                duration_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job_id, id DESC);
            """
        )
        # 历史 jobs 表升级（加新列）—— 忽略已存在的列错误
        for col, typ in [
            ("next_fire_at", "TEXT"),
            ("last_error", "TEXT"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_retries", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        conn.commit()
    finally:
        if should_close:
            conn.close()


def _record_run(
    job_id: str,
    started_at: str,
    finished_at: str,
    status: str,
    error: str = "",
    duration_ms: int | None = None,
    max_runs_log: int = 200,
) -> None:
    """往 job_runs 表追加一条执行记录（DLQ audit log + 重试轨迹）。"""
    with _db() as c:
        c.execute(
            """INSERT INTO job_runs (job_id, started_at, finished_at, status, error, duration_ms)
               VALUES (?,?,?,?,?,?)""",
            (job_id, started_at, finished_at, status, error, duration_ms),
        )
        # 单 job 最近 N 条截尾，防无限增长
        c.execute(
            """DELETE FROM job_runs WHERE id NOT IN
               (SELECT id FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?)
               AND job_id=?""",
            (job_id, max_runs_log, job_id),
        )


def _compute_next_fire_at(schedule: str, run_once_at: str | None = None) -> str | None:
    """计算下次触发时间（ISO8601）。"""
    if run_once_at:
        return run_once_at
    if not schedule:
        return None
    now = datetime.now(timezone.utc)
    try:
        if schedule.startswith("daily@"):
            hh, mm = schedule[len("daily@"):].split(":")
            t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if t <= now:
                t = t.replace(day=now.day + 1)
            return t.isoformat()
        if schedule.startswith("monthly@"):
            rest = schedule[len("monthly@"):]
            dd, hm = rest.split("@", 1)
            hh, mm = hm.split(":")
            t = now.replace(day=int(dd), hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if t <= now:
                m, y = (t.month + 1, t.year) if t.month < 12 else (1, t.year + 1)
                t = t.replace(month=m, year=y)
            return t.isoformat()
    except (ValueError, IndexError, AttributeError):
        return None
    return None

@dataclass
class WorkflowJob:
    id: str
    owner_sub: str
    tenant_id: str
    name: str
    query_template: str
    schedule: Optional[str] = None       # cron-like MVP: "daily@09:00" / "monthly@1@09:00"
    run_once_at: Optional[str] = None     # ISO8601
    webhook_url: Optional[str] = None
    enabled: bool = True
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None    # "OK" / "ERROR"
    last_error: Optional[str] = None
    next_fire_at: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkflowJob":
        return cls(**{k: row[k] for k in row.keys()})

    def to_dict(self) -> dict:
        return asdict(self)


def render_template(template: str, ref: Optional[date] = None) -> str:
    """替换 {{today}} / {{last_month}} / {{now}} 占位符。"""
    ref = ref or date.today()
    last_month = (date(ref.year - 1, 12, 1) if ref.month == 1
                  else date(ref.year, ref.month - 1, 1))
    return (
        template
        .replace("{{today}}", ref.isoformat())
        .replace("{{last_month}}", last_month.strftime("%Y-%m"))
        .replace("{{now}}", _utcnow_iso())
    )


def _run_now_inner(
    job: "WorkflowJob | dict",
    *,
    run_fn: Callable[[str, str], str] | None = None,
) -> tuple[bool, dict]:
    """核心执行逻辑——调度器 _execute 与 ARQ backend ``_arq_run_job`` 共用。

    入参 ``job`` 可以是 ``WorkflowJob`` 或 ``dict``（sqlite3.Row 解包即可）。
    返回 ``(ok, {session_id, error, report})``；不抛异常——错误全收进返回值。
    """
    jid = job["id"] if isinstance(job, dict) else job.id
    rendered = render_template(
        job["query_template"] if isinstance(job, dict) else job.query_template
    )
    sid = f"job-{jid}-{int(time.time())}"
    started = datetime.now(timezone.utc)
    err_msg = ""
    report = ""
    try:
        if run_fn is None:
            raise RuntimeError("run_fn must be provided")
        report = run_fn(sid, rendered)
        status = "OK"
    except Exception as exc:
        err_msg = repr(exc)[:2000]
        logger.error("Job %s run failed: %s", jid, exc)
        status = "ERROR"
    finished = datetime.now(timezone.utc)
    duration_ms = int((finished - started).total_seconds() * 1000)

    ts = _utcnow_iso()
    last_err: str | None = err_msg or None
    retry_inc = 1 if status == "ERROR" else 0
    with _db() as c:
        c.execute(
            """UPDATE jobs SET last_run_at=?, last_status=?, last_error=?,
                retry_count = COALESCE(retry_count,0) + ?
               WHERE id=?""",
            (ts, status, last_err, retry_inc, jid),
        )
    # webhook
    wh = job.get("webhook_url") if isinstance(job, dict) else getattr(job, "webhook_url", None)
    jname = job.get("name", "") if isinstance(job, dict) else getattr(job, "name", "")
    if wh:
        try:
            JobScheduler._fire_webhook(
                _PseudoJob(jid, jname, wh), sid, status, report
            )
        except Exception as exc:
            logger.warning("webhook failed for job %s: %s", jid, exc)
    _record_run(jid, started.isoformat(), finished.isoformat(),
                status, err_msg, duration_ms)
    return status == "OK", {
        "session_id": sid,
        "error": err_msg,
        "report": report[:500],
    }


class _PseudoJob:
    """给 JobScheduler._fire_webhook 做 duck-typing 占位。"""

    def __init__(self, id: str, name: str, webhook_url: str):
        self.id = id
        self.name = name
        self.webhook_url = webhook_url


def get_job_runs(job_id: str, limit: int = 50) -> list[dict]:
    """查一个 job 最近 N 次执行记录（DLQ 重试轨迹 / 审计 log）。"""
    with _db() as c:
        rows = c.execute(
            """SELECT id, job_id, started_at, finished_at, status, error, duration_ms
               FROM job_runs WHERE job_id=? ORDER BY id DESC LIMIT ?""",
            (job_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


class JobScheduler:
    """进程内调度器：维护 Timer 线程列表；每次 start / add_job / update_job 时
    重建调度。生产部署期建议升级到 APScheduler / ARQ。
    """

    def __init__(
        self,
        run_fn: Callable[[str, str], str],
        max_workers: int = 4,
    ) -> None:
        self._run_fn = run_fn          # (session_id, rendered_query) -> report_text
        self._max_workers = max_workers
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._started = False

    # ---- lifecycle ----
    def start(self) -> None:
        if self._started:
            return
        _ensure_schema()
        self._started = True
        # 启动时恢复持久化 job 中启用的任务
        for job in self._list_all_enabled():
            self._arm_job(job)
        logger.info("JobScheduler started (%d jobs armed)",
                     len(self._timers))

    def shutdown(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()
            self._started = False

    # ---- CRUD ----
    def add_job(
        self,
        owner_sub: str,
        tenant_id: str,
        name: str,
        query_template: str,
        *,
        schedule: Optional[str] = None,
        run_once_at: Optional[str] = None,
        webhook_url: Optional[str] = None,
        enabled: bool = True,
        max_retries: int = 0,
    ) -> WorkflowJob:
        nfire = _compute_next_fire_at(schedule, run_once_at or None)
        job = WorkflowJob(
            id=uuid.uuid4().hex[:12],
            owner_sub=owner_sub,
            tenant_id=tenant_id,
            name=name,
            query_template=query_template,
            schedule=schedule,
            run_once_at=run_once_at or None,
            webhook_url=webhook_url,
            enabled=enabled,
            created_at=_utcnow_iso(),
            **{"max_retries": max_retries, "retry_count": 0},
        )
        with _db() as c:
            c.execute(
                """INSERT INTO jobs
                   (id, owner_sub, tenant_id, name, query_template, schedule,
                    run_once_at, next_fire_at, webhook_url, enabled, max_retries,
                    retry_count, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.owner_sub, job.tenant_id, job.name,
                 job.query_template, job.schedule, job.run_once_at,
                 nfire, job.webhook_url, int(job.enabled),
                 max_retries, 0, job.created_at),
            )
        if enabled:
            self._arm_job(job)
        return job

    def list_jobs(self, owner_sub: str) -> list[WorkflowJob]:
        with _db() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE owner_sub=? ORDER BY created_at DESC",
                (owner_sub,),
            ).fetchall()
        return [WorkflowJob.from_row(r) for r in rows]

    def get_job(self, job_id: str) -> Optional[WorkflowJob]:
        with _db() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return WorkflowJob.from_row(row) if row else None

    def update_job(self, job_id: str, patch: dict) -> Optional[WorkflowJob]:
        job = self.get_job(job_id)
        if not job:
            return None
        for k, v in patch.items():
            if k == "enabled":
                v = bool(v)
            if hasattr(job, k):
                setattr(job, k, v)
        # schedule / run_once_at 变更后重算 next_fire_at
        nfire = _compute_next_fire_at(job.schedule or "", job.run_once_at)
        with _db() as c:
            c.execute(
                """UPDATE jobs SET
                    name=?, query_template=?, schedule=?, run_once_at=?,
                    next_fire_at=?, webhook_url=?, enabled=?
                   WHERE id=?""",
                (job.name, job.query_template, job.schedule, job.run_once_at,
                 nfire,
                 job.webhook_url, int(job.enabled), job.id),
            )
        # 重建调度
        self._disarm_job(job_id)
        if job.enabled:
            self._arm_job(job)
        return job

    def delete_job(self, job_id: str) -> bool:
        self._disarm_job(job_id)
        with _db() as c:
            cur = c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return cur.rowcount > 0

    def run_now(self, job_id: str) -> dict:
        """同步触发（POST /{id}/run 端点用）；返回 {job_id, session_id, status, error?}。"""
        job = self.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        ok, result = _run_now_inner(job, run_fn=self._run_fn)
        return {"job_id": job.id, "session_id": result.get("session_id", ""),
                "status": "OK" if ok else "ERROR", "error": result.get("error", "")}

    # ---- internal ----
    def _list_all_enabled(self) -> list[WorkflowJob]:
        with _db() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE enabled=1"
            ).fetchall()
        return [WorkflowJob.from_row(r) for r in rows]

    def _arm_job(self, job: WorkflowJob) -> None:
        """根据 schedule / run_once_at 设定 Timer。"""
        with self._lock:
            self._disarm_job(job.id)      # 防重复
            delay = self._compute_delay(job)
            if delay is None:
                return
            t = threading.Timer(delay, self._execute, args=(job,))
            t.daemon = True
            self._timers[job.id] = t
            t.start()

    def _disarm_job(self, job_id: str) -> None:
        t = self._timers.pop(job_id, None)
        if t:
            t.cancel()

    def _compute_delay(self, job: WorkflowJob) -> Optional[float]:
        """计算距下次执行的秒数。MVP 仅解析 daily@HH:MM / monthly@DD@HH:MM。"""
        if not job.enabled:
            return None
        if job.run_once_at:
            try:
                ts = datetime.fromisoformat(job.run_once_at.replace("Z", "+00:00"))
                delta = (ts.timestamp() - time.time())
                return max(delta, 0.0)
            except (ValueError, OSError):
                return None
        if not job.schedule:
            return None
        try:
            kind, rest = job.schedule.split("@", 1)
            if kind == "daily":
                hh, mm = map(int, rest.split(":"))
                return self._seconds_until(hh, mm)
            if kind == "monthly":
                day_str, time_str = rest.split("@")
                day = int(day_str)
                hh, mm = map(int, time_str.split(":"))
                return self._seconds_until_monthly(day, hh, mm)
        except (ValueError, AttributeError):
            logger.warning("Invalid schedule expression: %s", job.schedule)
        return None

    @staticmethod
    def _seconds_until(target_h: int, target_m: int) -> float:
        now = datetime.now()
        tgt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if tgt <= now:
            tgt = tgt.replace(day=tgt.day + 1)
        return (tgt - now).total_seconds()

    @staticmethod
    def _seconds_until_monthly(day: int, hh: int, mm: int) -> float:
        now = datetime.now()
        try:
            tgt = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            tgt = now.replace(day=1, hour=hh, minute=mm)
        if tgt <= now:
            m = tgt.month + 1
            y = tgt.year
            if m > 12:
                m = 1
                y += 1
            tgt = tgt.replace(year=y, month=m)
        return (tgt - now).total_seconds()

    def _execute(self, job: WorkflowJob) -> dict:
        """调度器 timer 走的入口；委托给 _run_now_inner 保证 ARQ 与调度器同路径。"""
        ok, result = _run_now_inner(job, run_fn=self._run_fn)
        return {"job_id": job.id, "session_id": result.get("session_id", ""),
                "status": "OK" if ok else "ERROR", "error": result.get("error", "")}

    @staticmethod
    def _fire_webhook(job: WorkflowJob, sid: str, status: str, report: str) -> None:
        import urllib.request
        import urllib.error
        body = json.dumps({
            "job_id": job.id,
            "name": job.name,
            "session_id": sid,
            "status": status,
            "report_snippet": report[:500] if report else "",
        }).encode()
        req = urllib.request.Request(
            job.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as _:
                pass
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Webhook POST failed for job %s: %s", job.id, exc)


# 全局单例
_instance: Optional[JobScheduler] = None


def get_scheduler(
    run_fn: Optional[Callable[[str, str], str]] = None,
) -> JobScheduler:
    global _instance
    if _instance is None:
        if run_fn is None:

            def run_fn(sid, query):
                return ""
        _instance = JobScheduler(run_fn=run_fn)
    return _instance
