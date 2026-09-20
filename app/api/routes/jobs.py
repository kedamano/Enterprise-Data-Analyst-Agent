"""Workflow Job REST API。

端点：
  POST   /api/v1/jobs                     创建 job
  GET    /api/v1/jobs                      列出当前用户 jobs
  GET    /api/v1/jobs/{id}                 单个详情
  PATCH  /api/v1/jobs/{id}                 更新 enabled / query / schedule
  DELETE /api/v1/jobs/{id}                 删除
  POST   /api/v1/jobs/{id}/run            手动触发一次 run now
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ...config import get_settings
from ...infrastructure.jobs import JobScheduler, WorkflowJob, get_scheduler
from ...core.security.auth import Principal, current_principal

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------- schemas ----------
class JobIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    query_template: str = Field(..., min_length=1, max_length=2000)
    schedule: Optional[str] = None          # "daily@09:00" / "monthly@1@09:00"
    run_once_at: Optional[str] = None       # ISO8601
    webhook_url: Optional[str] = None
    enabled: bool = True


class JobPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    query_template: Optional[str] = Field(None, min_length=1, max_length=2000)
    schedule: Optional[str] = None          # 传空字符串可清除
    run_once_at: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: Optional[bool] = None


class JobOut(BaseModel):
    id: str
    owner_sub: str
    tenant_id: str
    name: str
    query_template: str
    schedule: Optional[str] = None
    run_once_at: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: bool = True
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: str = ""


# ---------- helpers ----------
def _job_or_404(job_id: str, principal: Principal) -> WorkflowJob:
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.owner_sub != principal.sub and "admin" not in principal.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not job owner")
    return job


def _to_out(job: WorkflowJob) -> JobOut:
    return JobOut(**job.to_dict())


# ---------- routes ----------
@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobIn, principal: Principal = Depends(current_principal)):
    settings = get_settings()
    if not settings.workflow_jobs_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Workflow jobs disabled")

    scheduler = get_scheduler()
    jobs = scheduler.list_jobs(principal.sub)
    if len(jobs) >= settings.workflow_jobs_max_per_user:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Max {settings.workflow_jobs_max_per_user} jobs per user",
        )
    job = scheduler.add_job(
        owner_sub=principal.sub,
        tenant_id=principal.tenant_id,
        name=payload.name,
        query_template=payload.query_template,
        schedule=payload.schedule,
        run_once_at=payload.run_once_at,
        webhook_url=payload.webhook_url,
        enabled=payload.enabled,
    )
    return _to_out(job)


@router.get("", response_model=list[JobOut])
def list_jobs(principal: Principal = Depends(current_principal)):
    scheduler = get_scheduler()
    return [_to_out(j) for j in scheduler.list_jobs(principal.sub)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, principal: Principal = Depends(current_principal)):
    job = _job_or_404(job_id, principal)
    return _to_out(job)


@router.patch("/{job_id}", response_model=JobOut)
def patch_job(
    job_id: str,
    payload: JobPatch,
    principal: Principal = Depends(current_principal),
):
    _job_or_404(job_id, principal)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    # schedule="" 表示清除 → 用 sentinel
    if "schedule" in patch and patch["schedule"] == "":
        patch["schedule"] = None
    if "run_once_at" in patch and patch["run_once_at"] == "":
        patch["run_once_at"] = None
    if "webhook_url" in patch and patch["webhook_url"] == "":
        patch["webhook_url"] = None
    scheduler = get_scheduler()
    job = scheduler.update_job(job_id, patch)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return _to_out(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, principal: Principal = Depends(current_principal)):
    _job_or_404(job_id, principal)
    scheduler = get_scheduler()
    scheduler.delete_job(job_id)
    return None


@router.post("/{job_id}/run")
def run_job_now(job_id: str, principal: Principal = Depends(current_principal)):
    _job_or_404(job_id, principal)
    scheduler = get_scheduler()
    try:
        result = scheduler.run_now(job_id)
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc))
    return result
