"""ARQ 后端测试。

ARQ 后端的可用性取决于 env 与 redis：
- arq 未装 / REDIS_URL 未配  → arq_available() = False → 测试跳过（fail-open）
- arq 已装 + REDIS_URL 已配  → 跑真实 ARQ tick
- 单测聚焦无网络：只验 _compute_next_fire_at / render_template（jobs 侧） + arq 可用性探测。
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


def test_compute_next_fire_daily():
    from app.infrastructure.jobs import _compute_next_fire_at
    r = _compute_next_fire_at("daily@08:00")
    assert r is not None
    assert "T08:00" in r


def test_compute_next_fire_monthly():
    from app.infrastructure.jobs import _compute_next_fire_at
    r = _compute_next_fire_at("monthly@01@09:00")
    assert r is not None
    assert "T09:00" in r


def test_compute_next_fire_run_once_at():
    from app.infrastructure.jobs import _compute_next_fire_at
    r = _compute_next_fire_at("", run_once_at="2030-01-01T00:00:00")
    assert r == "2030-01-01T00:00:00"


def test_compute_next_fire_invalid():
    from app.infrastructure.jobs import _compute_next_fire_at
    assert _compute_next_fire_at("garbage") is None
    assert _compute_next_fire_at("") is None


def test_arq_available_is_bool(monkeypatch):
    """arq_available() 始终返回 bool；env 缺 REDIS_URL 时保守返回 False。"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app.infrastructure.jobs.arq_backend import arq_available
    assert arq_available() is False


def test_record_run_and_get_runs(tmp_path, monkeypatch):
    """_record_run + get_job_runs 端到端。"""
    import tempfile
    from app.infrastructure.jobs import init_db_path, _record_run, get_job_runs
    import app.infrastructure.jobs.jobs as jm
    jm._instance = None
    init_db_path(str(tmp_path / "j.db"))
    _record_run("job1", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z",
               "OK", "", 1000)
    _record_run("job1", "2026-01-01T00:01:01Z", "2026-01-01T00:02:01Z",
               "ERROR", "boom", 59000)
    runs = get_job_runs("job1")
    assert len(runs) == 2
    # 按 id DESC 排列，第一条是最新的
    assert runs[0]["status"] == "ERROR"
    assert runs[0]["job_id"] == "job1"


def test_record_run_trims_to_limit(tmp_path, monkeypatch):
    """job_runs 单 job 超 max_runs_log 条应自动截尾。"""
    from app.infrastructure.jobs import init_db_path, _record_run, get_job_runs
    import app.infrastructure.jobs.jobs as jm
    jm._instance = None
    init_db_path(str(tmp_path / "j.db"))
    # 写 250 条（默认限制 200）
    for i in range(250):
        _record_run("j", f"2026-01-01T00:00:{i:02d}Z",
                    f"2026-01-01T00:00:{i+1:02d}Z",
                    "OK", None, 100, max_runs_log=200)
    runs = get_job_runs("j", limit=300)
    assert len(runs) == 200
