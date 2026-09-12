"""P1-3 长期记忆回写：失败 / 降级必须把**结构化、可操作**的教训写回长期记忆。

背景（sleep.csv 案例）：LLM 全程降级到 Mock，报告却以 FINISH + 置信度 0.90 收场。
旧的 ``_persist_memory`` 只在 ``state.error`` / reflection FAIL 时写教训，
这种「静默降级」从未被记录 —— 下次同样会话只会重蹈覆辙。

本文件锁死：
1. 降级（mock 兜底但 FINISH）必须写回 lesson，且**分类根因 + 可操作动作**
   （复用 P0-4 的 classify_error），而不是只堆错误串。
2. 执行失败（state.error）同样写回 lesson。
3. recent_lessons 能按时间倒序回读 lesson（写回必须有回读通道，否则只进不出）。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.memory import long_term

REGION_ERR = (
    "Error code: 403 - {'error': {'message': 'This model is not available in your "
    "region.', 'code': 403}}"
)


def _fake_state(session_id="p13-sess", fallbacks=None, error="", reflection=None):
    return SimpleNamespace(
        session_id=session_id,
        status="FINISH",
        user_query="这份文件反应了什么数据规律",
        context=SimpleNamespace(objective="这份文件反应了什么数据规律"),
        analysis=SimpleNamespace(findings=[]),
        reflection=reflection,
        error=error,
        metadata={"llm_fallbacks": fallbacks or []},
    )


def test_degradation_written_back_as_classified_lesson(monkeypatch):
    """静默降级必须被记录，且给出可操作规避动作。"""
    from app.core.agents.data_analyst.nodes import _persist_memory
    import app.core.agents.data_analyst.nodes as nd

    captured = []
    monkeypatch.setattr(nd.long_term, "append", lambda e, tenant=None: captured.append(e))
    monkeypatch.setattr(nd.short_term, "put", lambda *a, **k: None)
    monkeypatch.setattr(nd.short_term, "get", lambda *a, **k: [])

    st = _fake_state(fallbacks=[
        {"stage": "planner", "error": REGION_ERR, "run_id": "p13-sess"},
        {"stage": "analyst", "error": REGION_ERR, "run_id": "p13-sess"},
        {"stage": "reporter", "error": REGION_ERR, "run_id": "p13-sess"},
    ])

    _persist_memory(st)

    deg = [e for e in captured if e.get("category") == "llm_degradation"]
    assert deg, "降级必须写回长期记忆，否则 P1-3 未闭环"
    # 3 个阶段 → 3 条降级教训（去重到阶段，不重复计数同一次原因）
    assert len(deg) == 3
    assert deg[0]["kind"] == "region_blocked"        # 分类根因
    assert deg[0]["action"]                          # 可操作动作存在
    assert "更换可用区域" in deg[0]["action"]         # 动作具体可执行
    assert deg[0]["stage"] == "planner"
    assert "ts" in deg[0]                              # 供 recent_lessons 排序


def test_execution_failure_written_back(monkeypatch):
    """state.error 路径仍写回 lesson（不退化旧行为）。"""
    from app.core.agents.data_analyst.nodes import _persist_memory
    import app.core.agents.data_analyst.nodes as nd

    captured = []
    monkeypatch.setattr(nd.long_term, "append", lambda e, tenant=None: captured.append(e))
    monkeypatch.setattr(nd.short_term, "put", lambda *a, **k: None)
    monkeypatch.setattr(nd.short_term, "get", lambda *a, **k: [])

    st = _fake_state(error="sql execution failed: no such table upload.sleep")
    _persist_memory(st)

    fail = [e for e in captured if e.get("category") == "execution_failure"]
    assert fail, "执行失败必须写回 lesson"
    assert "no such table" in fail[0]["reason"]


def test_healthy_run_writes_no_lesson(monkeypatch):
    """无错误、无降级时不应污染长期记忆。"""
    from app.core.agents.data_analyst.nodes import _persist_memory
    import app.core.agents.data_analyst.nodes as nd

    captured = []
    monkeypatch.setattr(nd.long_term, "append", lambda e, tenant=None: captured.append(e))
    monkeypatch.setattr(nd.short_term, "put", lambda *a, **k: None)
    monkeypatch.setattr(nd.short_term, "get", lambda *a, **k: [])

    st = _fake_state()  # 无 error / 无 fallbacks
    _persist_memory(st)
    assert [e for e in captured if e.get("type") == "lesson"] == []


def test_recent_lessons_filters_and_orders(monkeypatch):
    """recent_lessons 只回 lesson 类，且按时间倒序。"""
    fixed = [
        {"type": "analysis_summary", "objective": "x", "ts": "2026-01-01T00:00:00Z"},
        {"type": "lesson", "category": "llm_degradation", "stage": "planner",
         "ts": "2026-03-01T00:00:00Z"},
        {"type": "lesson", "category": "execution_failure", "ts": "2026-02-01T00:00:00Z"},
    ]
    monkeypatch.setattr(long_term, "_load", lambda: fixed)
    out = long_term.recent_lessons(limit=10)
    assert len(out) == 2                       # 仅 lesson 类
    assert out[0]["ts"].startswith("2026-03-01")  # 新的在前
    assert out[1]["ts"].startswith("2026-02-01")


def test_recent_lessons_respects_limit(monkeypatch):
    fixed = [
        {"type": "lesson", "ts": f"2026-0{i}-01T00:00:00Z"}
        for i in range(1, 6)
    ]
    monkeypatch.setattr(long_term, "_load", lambda: fixed)
    assert len(long_term.recent_lessons(limit=3)) == 3
