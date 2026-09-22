"""Workflow job 调度模块。

- 默认: 线程调度 + SQLite 持久化 (MVP, 零外部依赖)
- 选装: ARQ + Redis (``REDIS_URL`` 已配 + 起 ``arq app.infrastructure.jobs.arq_backend.WorkerSettings``)
- 降级: arq 未装 / REDIS_URL 未配 → 自动回退到线程调度 (fail-open)
"""
from .jobs import (
    JobScheduler,
    WorkflowJob,
    _compute_next_fire_at,
    _instance,
    _record_run,
    _run_now_inner,
    get_job_runs,
    get_scheduler,
    init_db_path,
)

__all__ = [
    "JobScheduler", "WorkflowJob", "get_scheduler", "init_db_path",
    "_instance", "_run_now_inner", "_compute_next_fire_at", "_record_run",
    "get_job_runs",
]

try:
    from .arq_backend import arq_available, render_template as _arq_render_template
    __all__.extend(["arq_available"])
except ImportError:  # arq 未装
    def arq_available() -> bool:
        return False
