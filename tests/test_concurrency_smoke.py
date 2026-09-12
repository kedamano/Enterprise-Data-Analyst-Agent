"""CONC/01 并发冒烟：并发出错是**必测项**，不能只靠手动压测。

Spec/基线：docs/progress/perf-baseline.md

这里只放**小规模、确定性**的自动化门禁（大压测在 `scripts/bench_concurrency.py`，
属手动/发布前动作）：并发下不能死锁、不能串会话、不能有非预期错误码。
"""
from __future__ import annotations

import threading

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def conc_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def test_concurrent_requests_no_deadlock_or_crosstalk(conc_env):
    """8 线程各打 2 次：全部成功、每个会话只拿到自己的结果。"""
    from fastapi.testclient import TestClient

    from app.main import app

    results: list[dict] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        client = TestClient(app)
        for j in range(2):
            sid = f"conc_{i}"
            q = f"分析各区域营收 {j}"          # 同会话不同问题 → 不命中缓存
            r = client.post("/api/v1/chat/analyze", json={"query": q, "session_id": sid})
            with lock:
                results.append({"i": i, "code": r.status_code,
                                "body": r.json() if r.status_code == 200 else {}})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert len(results) == 16, f"有线程未完成（疑似死锁）：{len(results)}/16"
    assert all(r["code"] == 200 for r in results), \
        [r["code"] for r in results if r["code"] != 200]
    assert all(r["body"].get("status") == "FINISH" for r in results), \
        [(r["body"].get("status"), r["body"].get("error")) for r in results]

    # 会话隔离：每条响应必须回它自己的 session_id
    for r in results:
        assert r["body"]["session_id"] == f"conc_{r['i']}", \
            f"会话串了：期望 conc_{r['i']}，得到 {r['body']['session_id']}"


def test_cache_is_consistent_under_concurrency(conc_env):
    """并发打同一会话同一问题：结果一致（要么都命中缓存、要么各自算，但不能互相污染）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    reports: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        client = TestClient(app)
        r = client.post("/api/v1/chat/analyze",
                        json={"query": "分析各区域营收", "session_id": "conc_same"})
        with lock:
            reports.append(r.json().get("report", ""))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert len(reports) == 6
    assert len(set(reports)) == 1, "同一会话同一问题的报告应完全一致（缓存或同路计算）"
