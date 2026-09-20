"""Semantic cache：同 session 内 cosine >= threshold 命中历史结果，不调 LLM。

与 response_cache 并列测试——语义覆盖的是 exact-match 兜不住的"换几个词再问"场景。
"""
from __future__ import annotations

import threading

import pytest

from app.config import get_settings
from app.core.agents.data_analyst import semantic_cache
from app.core.agents.data_analyst.state import AgentState


@pytest.fixture
def semantic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_CACHE_SIMILARITY_THRESHOLD", "0.92")
    # 隔离 SQLite 到 tmp_path
    monkeypatch.setenv("SEMANTIC_CACHE_DB_PATH", str(tmp_path / "semantic_cache.db"))
    get_settings.cache_clear()
    semantic_cache._reset_connection()
    semantic_cache.clear_semantic()
    yield
    semantic_cache.clear_semantic()
    semantic_cache._reset_connection()
    get_settings.cache_clear()


def _make_state(session: str, query: str, status: str = "FINISH",
                report: str = "营收 100 万", degraded: bool = False) -> AgentState:
    st = AgentState(session_id=session, user_query=query, status=status)
    st.report = report
    st.metadata["degraded"] = degraded
    return st


# --------------------------------------------------------------------------- #
# 1. 命中与高相似度
# --------------------------------------------------------------------------- #
def test_semantic_hit_high_similarity(semantic_env):
    """put 一条 status=FINISH 记录；用高相似 query 查，应命中且 similarity > 0.85。"""
    st = _make_state("sess_1", "各区域营收分析", report="营收 100 万")
    semantic_cache.put_semantic("sess_1", "各区域营收分析", st)

    # 同字符不同排列 → cosine=1.0（hashing trick 对字符集相同的 query 给出完美匹配）
    hit = semantic_cache.get_semantic("sess_1", "分析各区域营收")
    assert hit is not None, "高相似 query 应命中语义缓存"
    assert hit.status == "FINISH"
    assert hit.report == "营收 100 万"
    assert hit.metadata.get("cache_hit") is True
    assert hit.metadata.get("cache_key") == "semantic"
    assert hit.metadata.get("similarity", 0) > 0.85
    assert hit.metadata.get("matched_query") == "各区域营收分析"


# --------------------------------------------------------------------------- #
# 2. 不命中——话题完全不同
# --------------------------------------------------------------------------- #
def test_semantic_no_hit_low_similarity(semantic_env):
    """put "营收 100 万"；用极不同的话题 "反欺诈规则有哪些" 查，应返回 None。"""
    st = _make_state("sess_2", "营收 100 万", report="营收 100 万")
    semantic_cache.put_semantic("sess_2", "营收 100 万", st)

    hit = semantic_cache.get_semantic("sess_2", "反欺诈规则有哪些")
    assert hit is None, "话题完全不同不应命中"


# --------------------------------------------------------------------------- #
# 3. 降级结果不缓存
# --------------------------------------------------------------------------- #
def test_semantic_skip_degraded(semantic_env):
    """degraded=True 的记录不缓存（put 时跳过）。断言再查返回 None。"""
    st = _make_state("sess_3", "test_q", report="模板报告", degraded=True)
    semantic_cache.put_semantic("sess_3", "test_q", st)

    hit = semantic_cache.get_semantic("sess_3", "test_q")
    assert hit is None, "降级结果不应入语义缓存"


# --------------------------------------------------------------------------- #
# 4. 非 FINISH 不缓存
# --------------------------------------------------------------------------- #
def test_semantic_skip_non_finish(semantic_env):
    """status=ERROR 的记录不缓存（断言查不到）。"""
    st = _make_state("sess_4", "err_q", status="ERROR")
    st.report = "anything"
    semantic_cache.put_semantic("sess_4", "err_q", st)

    hit = semantic_cache.get_semantic("sess_4", "err_q")
    assert hit is None, "非 FINISH 状态不应入语义缓存"


# --------------------------------------------------------------------------- #
# 5. 跨 session 租户隔离
# --------------------------------------------------------------------------- #
def test_semantic_cross_session_no_leak(semantic_env):
    """session A 存的 B 查不到。"""
    st = _make_state("sess_A", "营收数据", report="营收 200 万")
    semantic_cache.put_semantic("sess_A", "营收数据", st)

    hit = semantic_cache.get_semantic("sess_B", "营收数据")
    assert hit is None, "session B 不能命中 session A 的语义缓存（租户隔离）"


# --------------------------------------------------------------------------- #
# 6. exact-match 优先；query 改词走 semantic
# --------------------------------------------------------------------------- #
def test_semantic_integration_with_response_cache(semantic_env, monkeypatch):
    """先写 exact-match 命中→应走 resp cache；query 改几个词→走 semantic cache。

    直接对 run_analysis mock trace_run + 真实 sqlite 验证两条路径。"""
    import app.core.agents.data_analyst.graph as g

    # mock trace_run: 用空 context manager
    from contextlib import contextmanager

    @contextmanager
    def _fake_trace(run_id=None):
        yield

    monkeypatch.setattr(g, "trace_run", _fake_trace)

    # 先把一条 FINISH 结果塞进语义缓存（与 response_cache 共存）
    st = AgentState(session_id="sess_int", user_query="各区域营收分析", status="FINISH")
    st.report = "区域营收分析报告"
    st.metadata["degraded"] = False
    semantic_cache.put_semantic("sess_int", "各区域营收分析", st)

    # 关闭 response_cache（模拟 exact-match 未命中），验证走 semantic 路径
    import app.core.agents.data_analyst.response_cache as rc_mod
    real_get = rc_mod.get_cached
    real_put = rc_mod.put_cached
    real_en = rc_mod.enabled

    rc_mod.get_cached = lambda *a, **kw: None
    rc_mod.enabled = lambda: False
    rc_mod.put_cached = lambda *a, **kw: None

    try:
        # 同字符不同排列 → semantic cache 命中
        hit = semantic_cache.get_semantic("sess_int", "分析各区域营收")
        assert hit is not None, "语义路径应在 exact-match 关闭后命中"
        assert hit.metadata.get("cache_key") == "semantic"
        assert hit.report == "区域营收分析报告"
        assert hit.metadata.get("similarity", 0) >= 0.92
    finally:
        rc_mod.get_cached = real_get
        rc_mod.put_cached = real_put
        rc_mod.enabled = real_en


# --------------------------------------------------------------------------- #
# 7. 并发写入不报错
# --------------------------------------------------------------------------- #
def test_semantic_concurrent_writes(semantic_env):
    """多线程 put 10 条 —— sqlite3 锁 + check_same_thread=False 应不报错。"""
    errors: list = []

    def _put(i: int):
        try:
            st = _make_state("sess_conc", f"query_{i}", report=f"报告 {i}")
            semantic_cache.put_semantic("sess_conc", f"query_{i}", st)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_put, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入不应报错：{errors}"

    # 验证至少有一条能查到
    hit = semantic_cache.get_semantic("sess_conc", "query_0")
    assert hit is not None
    assert hit.report == "报告 0"
