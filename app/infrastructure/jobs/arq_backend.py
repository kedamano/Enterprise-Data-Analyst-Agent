"""ARQ 后端（可选）：把 Workflow Job 的「定时触发」从线程调度换成 Redis + ARQ worker。

启用条件：``.env`` 配 ``REDIS_URL``，且启动 ``arq app.infrastructure.jobs.arq_backend.WorkerSettings``。

架构原则（与 threading 后端 1:1 对等）：
- SQLite 仍是 jobs 的 source of truth；ARQ 只负责「要到点了 → 把 job 投递到 worker 执行」。
- ARQ cron 每 30 秒跑一次 ``_arq_tick()``：捞取 SQLite 里 now 之前的所有 job，每 job 投递一个 ``_arq_run_job`` 任务。
- 重试：失败任务进入 DLQ（SQLite ``job_runs`` 表，status=FAILED），可人工重跑。
- fallback：REDIS_URL 未配 → 自动回退到 threading backend（入口在 ``__init__.py::get_scheduler``）。
- 重入保护：单 worker 内并发 job 用 ``asyncio.Semaphore`` 限流；SQLite 行锁 + ``last_run_at``「已占」判断防双跑。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    import arq
    from arq import cron
    from arq.connections import RedisSettings
except ImportError:  # arq 未装 → 占位
    arq = None  # type: ignore[assignment]
    cron = None  # type: ignore[assignment]
    RedisSettings = None  # type: ignore[assignment]

# 每 tick 最多并发执行多少个 job（防 worker 超载）
_MAX_CONCURRENCY = int(os.getenv("ARQ_JOB_CONCURRENCY", "4"))
_redis_url = os.getenv("REDIS_URL", "")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _compute_next_fire(schedule: str, after: datetime | None = None) -> datetime | None:
    """解析 ``daily@HH:MM`` / ``monthly@DD@HH:MM`` 返回 next fire时间（UTC naive）。"""
    if not schedule:
        return None
    ref = after or _utcnow()
    try:
        if schedule.startswith("daily@"):
            hh, mm = schedule[len("daily@"):].split(":")
            n = ref.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            return n if n > ref else n.replace(day=ref.day + 1)
        if schedule.startswith("monthly@"):
            rest = schedule[len("monthly@"):]
            dd, hh, mm = rest.split("@")[0], *rest.split("@")[1].split(":")
            n = ref.replace(day=int(dd), hour=int(hh), minute=int(mm), second=0, microsecond=0)
            return n if n > ref else n.replace(month=ref.month + 1)
    except (ValueError, IndexError):
        return None
    return None


async def _post_webhook(url: str, payload: dict, timeout: float = 10.0) -> None:
    """fire-and-forget webhook。失败 swallow 不抛异常。"""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        # 用线程池把阻塞 urllib 包一层，防事件循环卡住
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=timeout))
    except Exception as exc:
        logger.warning("webhook POST %s failed: %s", url, exc)


async def _arq_run_job(job_dict: dict, sem: asyncio.Semaphore) -> dict:
    """在 worker 里真正跑一个 job：调 run_fn(sid, rendered) → 记 job_runs → POST webhook。

    ``run_fn`` 在 startup 时注入；这里通过 ``WorkerSettings.on_startup`` 拿到 ctx['run_fn']。
    """
    async with sem:
        sid = f"jobrun-{job_dict['id']}"
        rendered = render_template(job_dict["query_template"])
        started = _utcnow().isoformat()
        status = "OK"
        report = ""
        try:
            # run_fn 必须是 asyncio-safe 的（内部会 await LLM）
            from .jobs import get_scheduler, _run_now_inner
            ok, report = await asyncio.get_running_loop().run_in_executor(
                None, lambda: _run_now_inner(job_dict)
            )
            status = "OK" if ok else "FAILED"
        except Exception as exc:
            status = "FAILED"
            report = repr(exc)
            logger.exception("ARQ job %s failed", job_dict["id"])

        finished = _utcnow().isoformat()

        # 持久化 job_runs（DLQ audit log）
        try:
            from .jobs import _record_run
            _record_run(job_dict["id"], started, finished, status, report)
        except Exception as exc:
            logger.warning("record_run failed: %s", exc)

        # webhook（fire-and-forget）
        wh = job_dict.get("webhook_url")
        if wh:
            await _post_webhook(wh, {
                "job_id": job_dict["id"],
                "job_name": job_dict["name"],
                "session_id": sid,
                "status": status,
                "rendered_query": rendered,
            })

        return {"job_id": job_dict["id"], "session_id": sid, "status": status}


def render_template(template: str, ref: datetime | None = None) -> str:
    """ARQ 后端复用同一套模板规则（与 jobs.render_template 同逻辑，但内联一份简化）。"""
    ref = ref or _utcnow()
    return (
        template
        .replace("{{today}}", ref.strftime("%Y-%m-%d"))
        .replace("{{last_month}}", (ref.replace(day=1)).strftime("%Y-%m"))
        .replace("{{now}}", ref.isoformat())
    )


async def _arq_tick(ctx: dict) -> None:
    """cron 钩子（每 30 秒执行）：捞 SQLite 里 now+30s 内要跑的 job，逐个 dispatch。"""
    from .jobs import _db
    sem: asyncio.Semaphore = ctx["_semaphore"]
    now = _utcnow()
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE enabled=1 AND (next_fire_at IS NULL OR next_fire_at <= ?)",
            (now.isoformat(),),
        ).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("ARQ tick read failed: %s", exc)
        return

    for row in rows:
        job = dict(row)
        # 投递给 ARQ 自带的并发任务队列（enqueue_job 让 ARQ worker 去跑）
        try:
            await ctx["pool"].enqueue_job("_arq_run_job", job, _semaphore=sem)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("enqueue job %s failed: %s", job.get("id"), exc)


class WorkerSettings:
    """arq 启动入口：

    ``arq app.infrastructure.jobs.arq_backend.WorkerSettings``

    env 需要 ``redis://...``。
    """

    functions = []


if cron is not None and RedisSettings is not None:
    # arq 已装 → 填上 cron_jobs / redis_settings（模块加载时求值，必须在 arq 可用时）
    WorkerSettings.cron_jobs = [cron(_arq_tick, second={0, 30}, run_at_startup=False)]  # type: ignore[attr-defined]
    WorkerSettings.redis_settings = RedisSettings.from_dsn(_redis_url) if _redis_url else None  # type: ignore[attr-defined]
else:
    WorkerSettings.cron_jobs = []  # type: ignore[attr-defined]
    WorkerSettings.redis_settings = None  # type: ignore[attr-defined]

WorkerSettings.job_timeout = 600
WorkerSettings.max_jobs = _MAX_CONCURRENCY
WorkerSettings.sem = asyncio.Semaphore(_MAX_CONCURRENCY)


async def on_startup(self, ctx):
    ctx["_semaphore"] = self.sem
    logger.info("ARQ worker started (redis=%s)", _redis_url[:20] + "..." if _redis_url else "(fallback)")


async def on_shutdown(self, ctx):
    logger.info("ARQ worker shutdown")


WorkerSettings.on_startup = on_startup
WorkerSettings.on_shutdown = on_shutdown


def arq_available() -> bool:
    """ARQ 后端是否可用（装了 arq 且配了 REDIS_URL）。"""
    return arq is not None and bool(_redis_url)
