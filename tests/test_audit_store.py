"""D46：**审计落库**（SQLite/PG）+ SQL 查询。

缺口（Gap 五）
-------------
> 审计为文件非**不可篡改库**、无 **SQL 审计查询**。

四条审计流（`tool_audit` / `auth` / `masking` / `hitl`）原本各写各的 JSONL：
复盘"谁在什么时候导出了什么"只能 grep，而且**文件可被就地改写**——出了争议没有证据力。

本日：统一走 `security/audit_store.py`，后端三选一（`jsonl` 默认 / `sqlite` / `postgres`），
并提供查询与历史导入。

**兼容性是硬要求**：默认 `jsonl` → 四个文件与内容**一字不变**，
既有用例（`test_audit` / `test_auth_permissions` / `test_masking` / `test_hitl`）零影响。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.security import audit_store


@pytest.fixture
def sqlite_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "sqlite")
    monkeypatch.setenv("AUDIT_DB_URL", f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    get_settings.cache_clear()
    yield tmp_path / "audit.db"
    get_settings.cache_clear()


@pytest.fixture
def jsonl_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "jsonl")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 一、默认后端 = jsonl：既有行为**一字不变**
# --------------------------------------------------------------------------- #
def test_default_backend_is_jsonl():
    assert get_settings().audit_backend == "jsonl"


def test_jsonl_backend_keeps_writing_the_same_file(jsonl_backend):
    """path 覆盖必须生效——各模块传自己的常量，测试里的 monkeypatch 才继续有效。"""
    target = jsonl_backend / "custom.jsonl"
    audit_store.record("tool", {"ts": "t", "session_id": "s"}, path=target)
    lines = [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines()]
    assert lines == [{"ts": "t", "session_id": "s"}]


def test_jsonl_query_returns_empty_not_error(jsonl_backend):
    """`jsonl` 后端不支持查询——**返回空**而不是抛（调用方不必分支）。"""
    assert audit_store.query(kind="tool") == []


# --------------------------------------------------------------------------- #
# 二、落库 + 查询
# --------------------------------------------------------------------------- #
def test_sqlite_backend_persists(sqlite_backend):
    audit_store.record("tool", {"ts": "2026-09-14T10:00:00", "session_id": "s1", "tool": "sql_query"})
    audit_store.record("auth", {"ts": "2026-09-14T10:01:00", "session_id": "s1", "decision": "ALLOW"})
    rows = audit_store.query()
    assert len(rows) == 2
    assert {r["kind"] for r in rows} == {"tool", "auth"}
    assert any(r["entry"].get("tool") == "sql_query" for r in rows)


def test_query_filters_by_kind_session_and_since(sqlite_backend):
    audit_store.record("tool", {"ts": "2026-09-14T09:00:00", "session_id": "s1"})
    audit_store.record("tool", {"ts": "2026-09-14T11:00:00", "session_id": "s2"})
    audit_store.record("auth", {"ts": "2026-09-14T11:00:00", "session_id": "s1"})

    assert len(audit_store.query(kind="tool")) == 2
    assert len(audit_store.query(session_id="s1")) == 2
    assert len(audit_store.query(kind="tool", session_id="s2")) == 1
    assert len(audit_store.query(since="2026-09-14T10:00:00")) == 2


def test_query_limit_is_bounded(sqlite_backend):
    for i in range(5):
        audit_store.record("tool", {"ts": f"2026-09-14T10:0{i}:00", "session_id": "s"})
    assert len(audit_store.query(limit=2)) == 2


# --------------------------------------------------------------------------- #
# 三、历史导入：**幂等**
# --------------------------------------------------------------------------- #
def test_import_jsonl_is_idempotent(sqlite_backend, tmp_path):
    src = tmp_path / "old.jsonl"
    src.write_text("\n".join(json.dumps({"ts": f"2026-09-01T0{i}:00:00", "session_id": "s"})
                             for i in range(3)) + "\n", encoding="utf-8")

    assert audit_store.import_jsonl("tool", src) == 3
    assert audit_store.import_jsonl("tool", src) == 0, "重复导入不得翻倍"
    assert len(audit_store.query()) == 3


def test_import_skips_broken_lines(sqlite_backend, tmp_path):
    """历史文件里混了半行/坏行不得让整次导入失败。"""
    src = tmp_path / "mixed.jsonl"
    src.write_text('{"ts":"t1","session_id":"s"}\nnot-json\n{"ts":"t2","session_id":"s"}\n',
                   encoding="utf-8")
    assert audit_store.import_jsonl("tool", src) == 2


def test_import_missing_file_is_a_noop(sqlite_backend, tmp_path):
    assert audit_store.import_jsonl("tool", tmp_path / "nope.jsonl") == 0


# --------------------------------------------------------------------------- #
# 四、审计**绝不能打断业务**
# --------------------------------------------------------------------------- #
def test_record_never_raises_on_backend_failure(monkeypatch):
    """审计故障吞掉但**不静默**（有 warning）。"""
    monkeypatch.setenv("AUDIT_BACKEND", "sqlite")
    monkeypatch.setenv("AUDIT_DB_URL", "sqlite:////nonexistent-dir-xyz/audit.db")
    get_settings.cache_clear()
    try:
        audit_store.record("tool", {"ts": "t"})   # 不该抛
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 五、四条审计流**都**经统一收口（源码级钉住）
# --------------------------------------------------------------------------- #
def test_all_four_audit_streams_go_through_the_store():
    """否则"切到 sqlite 后端"只对其中几条生效——正是本项目反复出现的"配了没生效"。"""
    import inspect

    import app.core.security.auth as auth_mod
    import app.core.security.hitl as hitl_mod
    import app.core.security.masking as masking_mod
    import app.core.tools as tools_mod

    for mod, fn in ((tools_mod, "_audit"), (auth_mod, "audit"),
                    (masking_mod, "_audit"), (hitl_mod, "_audit")):
        src = inspect.getsource(getattr(mod, fn))
        assert "audit_store" in src, f"{mod.__name__}.{fn} 未走统一审计存储"


def test_fingerprint_is_content_based():
    a = audit_store.fingerprint("tool", {"ts": "t", "x": 1})
    assert a == audit_store.fingerprint("tool", {"x": 1, "ts": "t"}), "键序不该影响指纹"
    assert a != audit_store.fingerprint("auth", {"ts": "t", "x": 1}), "kind 应参与指纹"


# --------------------------------------------------------------------------- #
# 六、查询端点
# --------------------------------------------------------------------------- #
def test_audit_endpoint_reports_jsonl_backend_honestly(jsonl_backend):
    """`jsonl` 后端下必须**如实说"查不了"**，而不是回一个空列表假装查过了。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/debug/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "jsonl" and body["count"] == 0
    assert "无法 SQL 查询" in body["note"], body


def test_audit_endpoint_returns_rows_on_sqlite(sqlite_backend):
    from fastapi.testclient import TestClient

    from app.main import app

    audit_store.record("tool", {"ts": "2026-09-14T10:00:00", "session_id": "s-endpoint"})
    with TestClient(app) as c:
        r = c.get("/api/v1/debug/audit?kind=tool&session_id=s-endpoint")
    body = r.json()
    assert r.status_code == 200 and body["count"] == 1
    assert body["events"][0]["entry"]["session_id"] == "s-endpoint"


# --------------------------------------------------------------------------- #
# 六、并发：不得丢记录、不得撕裂行（真模型套件跑出来的缺陷）
# --------------------------------------------------------------------------- #
def _parses(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except Exception:
        return False


def test_concurrent_appends_lose_nothing_and_tear_nothing(jsonl_backend):
    """并行执行器 4 个 worker 会**同时**写审计：1200 条必须一条不少，且每行都是合法 JSON。

    缺陷实证（2026-09-15，真模型套件跑起来后才暴露）：
    `_append_jsonl` 无锁，多线程各持文件句柄追加，实测 1200 次调用**只有 1069~1174 条落盘**，
    而且**零异常、零告警**——`record()` 的"绝不抛"把失败彻底吞掉，调用方看不出少写了。
    落盘还出现过半行：`data/audit/masking.jsonl` #479 截断在 `"step_id": `，
    #480 是其尾巴 `e"]}`。审计记录无声缺失＝审计链可被截断，出争议时不胜任证据。
    """
    import threading

    path = jsonl_backend / "concurrent.jsonl"
    n_threads, n_each = 8, 150
    payload = "x" * 2000  # 撑过写缓冲，逼出并发写

    def worker(tid: int) -> None:
        for i in range(n_each):
            audit_store.record(
                "tool",
                {"session_id": f"s{tid}", "step_id": f"st{tid}-{i}", "sql": payload, "i": i},
                path=path,
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * n_each, (
        f"审计记录**静默丢了** {n_threads * n_each - len(lines)} 条"
        f"（{len(lines)}/{n_threads * n_each}）"
    )
    bad = [i for i, ln in enumerate(lines) if not _parses(ln)]
    assert not bad, f"审计行被并发写撕裂（行号 {bad[:3]}）"
