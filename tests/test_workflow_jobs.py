"""Workflow Job 测试。覆盖 CRUD、租户隔离、模板渲染、手动触发。"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

import app.infrastructure.jobs.jobs as _jobs_mod
from app.infrastructure.jobs import get_scheduler, init_db_path


@pytest.fixture(autouse=True)
def _tmp_jobs_db():
    tmpdir = tempfile.mkdtemp()
    _jobs_mod._instance = None
    _jobs_mod._DB_PATH = ""
    init_db_path(str(pathlib.Path(tmpdir) / "jobs.db"))
    yield tmpdir
    _jobs_mod._instance = None
    _jobs_mod._DB_PATH = ""
    shutil.rmtree(tmpdir, ignore_errors=True)


def _reset_singleton():
    _jobs_mod._instance = None
    _jobs_mod._DB_PATH = ""


def test_create_and_list_job():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s = get_scheduler(run_fn=lambda sid, q: f"report for {q}")
    job = s.add_job("alice", "t1", "月度 GMV 回顾", "分析 {{last_month}} GMV")
    jobs = s.list_jobs("alice")
    assert len(jobs) == 1
    assert jobs[0].id == job.id


def test_create_job_validation():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s = get_scheduler()
    job = s.add_job("alice", "t1", "x", "q")
    assert job.id
    assert job.enabled is True


def test_tenant_isolation():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s = get_scheduler()
    s.add_job("alice", "t1", "a1", "q1")
    s.add_job("alice", "t1", "a2", "q2")
    s.add_job("bob", "t2", "b1", "q3")
    assert len(s.list_jobs("alice")) == 2
    assert len(s.list_jobs("bob")) == 1


def test_toggle_job():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s = get_scheduler()
    job = s.add_job("alice", "t1", "a", "q")
    patched = s.update_job(job.id, {"enabled": False})
    assert patched.enabled is False


def test_delete_job():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s = get_scheduler()
    job = s.add_job("alice", "t1", "a", "q")
    s.delete_job(job.id)
    assert s.get_job(job.id) is None


def test_render_template_replaces_placeholders():
    from datetime import date
    from app.infrastructure.jobs.jobs import render_template
    tpl = "分析 {{today}} vs {{last_month}} 数据"
    out = render_template(tpl, ref=date(2026, 9, 18))
    assert "{{" not in out
    assert "2026-09-18" in out
    assert "2026-08" in out


def test_job_run_now_triggers_run():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    calls = []

    def fake_run(sid, q):
        calls.append((sid, q))
        return f"report for {q}"

    s = get_scheduler(run_fn=fake_run)
    job = s.add_job("alice", "t1", "a", "KPI 分析")
    result = s.run_now(job.id)
    assert result["status"] == "OK"
    assert len(calls) == 1


def test_webhook_error_is_swallowed(monkeypatch):
    import urllib.error
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))

    def fail_urlopen(req, timeout=10):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)
    s = get_scheduler(run_fn=lambda sid, q: "ok")
    job = s.add_job("alice", "t1", "a", "q", webhook_url="http://localhost:9/x")
    result = s.run_now(job.id)
    assert result["status"] == "OK"
    assert s.get_job(job.id).last_status == "OK"


def test_scheduler_singleton():
    _reset_singleton()
    init_db_path(str(pathlib.Path(tempfile.mkdtemp()) / "j.db"))
    s1 = get_scheduler()
    s2 = get_scheduler()
    assert s1 is s2
