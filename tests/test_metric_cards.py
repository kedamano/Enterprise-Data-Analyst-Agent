"""D51：指标卡 —— `metrics` 的**唯一归一化口径** + SSE 下发 + 前端接线契约。

Spec: docs/specs/C5/01-report-charts-ui.md §2

断在哪：`AnalysisResult.metrics` 真实形态有两种（结构化 `{name,value,comparison}`
与 coerce 后的纯文本 `{text}`），但 SSE 的 FINISH 帧**此前根本不下发 metrics**，
前端再怎么写也无从消费。

设计选择：**归一在后端**。前端拿到的一定是 `{name, value, comparison}`，
于是"两种形态怎么办"只有一份实现（`metric_cards.normalize_metrics`），
而不是前端再猜一遍——猜错的那一半永远没人测到。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def env(monkeypatch, tmp_path):
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


# --------------------------------------------------------------------------- #
# 一、归一化（唯一口径）
# --------------------------------------------------------------------------- #
def test_normalize_keeps_structured_metrics():
    from app.core.agents.data_analyst.metric_cards import normalize_metrics

    out = normalize_metrics([
        {"name": "华东营收", "value": "1.23 亿", "comparison": "环比 +8.1%"},
    ])
    assert out == [{"name": "华东营收", "value": "1.23 亿", "comparison": "环比 +8.1%"}]


def test_normalize_puts_plain_text_into_name():
    """coerce 后的 `{text: "..."}`：整句进指标名，内容不丢（与 report_tool 同处理）。"""
    from app.core.agents.data_analyst.metric_cards import normalize_metrics

    out = normalize_metrics([{"text": "华东营收 1.23 亿"}])
    assert out == [{"name": "华东营收 1.23 亿", "value": "", "comparison": ""}]


def test_normalize_key_priority_is_name_text_metric():
    """三个候选键的优先级必须与 report_tool 一致——两处分叉等于口径分叉。"""
    from app.core.agents.data_analyst.metric_cards import normalize_metrics

    out = normalize_metrics([{"name": "N", "text": "T", "metric": "M", "value": 1}])
    assert out[0]["name"] == "N"

    out = normalize_metrics([{"text": "T", "metric": "M", "value": 1}])
    assert out[0]["name"] == "T"


def test_normalize_drops_empty_and_non_dict_entries():
    from app.core.agents.data_analyst.metric_cards import normalize_metrics

    assert normalize_metrics([{}, {"name": "  "}, None, "字符串", 42]) == [], \
        "空条目与非字典必须丢弃，否则前端渲染空气泡"


def test_normalize_tolerates_none_and_garbage():
    from app.core.agents.data_analyst.metric_cards import normalize_metrics

    assert normalize_metrics(None) == []
    assert normalize_metrics("oops") == []
    assert normalize_metrics([{"name": "A", "value": None}]) == [
        {"name": "A", "value": "", "comparison": ""}
    ], "None 值渲染成空串，不是 'None' 字面量"


def test_report_tool_and_metric_cards_share_one_normalizer():
    """源码级：report_tool 不得再写一份自己的优先级表（否则迟早分叉）。"""
    import inspect

    from app.core.tools import report_tool

    src = inspect.getsource(report_tool.run)
    assert "normalize_metrics" in src, "report_tool 必须复用同一归一化，而不是自己再判一遍键"


# --------------------------------------------------------------------------- #
# 二、SSE / REST 下发
# --------------------------------------------------------------------------- #
def _fake_state(metrics):
    from app.core.agents.data_analyst.state import AgentState

    state = AgentState(session_id="d51_metrics", user_query="分析各区域营收")
    state.analysis.metrics = list(metrics)
    state.report = "# 报告\n\n正文。"
    state.status = "FINISH"
    return state


def _sse_frames(client, state) -> list[dict]:
    r = client.post("/api/v1/chat/analyze/stream", json={"query": "分析各区域营收",
                                                         "session_id": "d51_metrics"})
    assert r.status_code == 200, r.text[:200]
    frames = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload and payload != "[DONE]":
                frames.append(json.loads(payload))
    return frames


def test_finish_frame_carries_normalized_metrics(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    state = _fake_state([{"text": "华东营收 1.23 亿"},
                         {"name": "华北营收", "value": "0.8 亿", "comparison": "环比 -3%"}])
    monkeypatch.setattr("app.api.routes.chat.stream_analysis",
                        lambda *a, **k: iter([state]))

    frames = _sse_frames(TestClient(app), state)
    finish = [f for f in frames if f["status"] == "FINISH"]
    assert finish, frames
    assert finish[-1]["metrics"] == [
        {"name": "华东营收 1.23 亿", "value": "", "comparison": ""},
        {"name": "华北营收", "value": "0.8 亿", "comparison": "环比 -3%"},
    ]


def test_finish_frame_metrics_is_empty_list_not_missing(env, monkeypatch):
    """没指标 → `[]`（前端据此整块不渲染）。键缺失与空列表是两回事。"""
    from fastapi.testclient import TestClient

    from app.main import app

    state = _fake_state([])
    monkeypatch.setattr("app.api.routes.chat.stream_analysis",
                        lambda *a, **k: iter([state]))

    finish = [f for f in _sse_frames(TestClient(app), state) if f["status"] == "FINISH"]
    assert finish[-1]["metrics"] == []


def test_non_finish_frames_do_not_carry_metrics(env, monkeypatch):
    """只有 FINISH 帧带 metrics —— 中途帧带一个半成品列表，前端会当"最终值"渲染。"""
    from fastapi.testclient import TestClient

    from app.main import app

    running = _fake_state([{"name": "A", "value": 1}])
    running.status = "EXECUTE"
    monkeypatch.setattr("app.api.routes.chat.stream_analysis",
                        lambda *a, **k: iter([running]))

    frames = _sse_frames(TestClient(app), running)
    assert frames[0]["metrics"] is None


def test_analyze_response_carries_metrics(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    state = _fake_state([{"name": "华东营收", "value": "1.23 亿"}])
    monkeypatch.setattr("app.api.routes.chat.run_analysis", lambda *a, **k: state)

    body = TestClient(app).post("/api/v1/chat/analyze",
                                json={"query": "分析", "session_id": "d51_metrics"}).json()
    assert body["metrics"] == [{"name": "华东营收", "value": "1.23 亿", "comparison": ""}]


# --------------------------------------------------------------------------- #
# 三、前端接线契约（源码级——离线跑不了浏览器，能守的是接线这一层）
# --------------------------------------------------------------------------- #
_WEB = __import__("pathlib").Path(__file__).resolve().parent.parent / "web"


def _read(rel: str) -> str:
    return (_WEB / rel).read_text(encoding="utf-8")


def test_agent_event_declares_metrics():
    api = _read("src/lib/api.ts")
    assert "interface MetricCard" in api, "缺少指标卡的类型声明"
    block = api.split("export interface AgentEvent", 1)[1].split("}", 1)[0]
    assert "metrics?" in block, "AgentEvent 必须声明 metrics（否则后端发了前端也读不到）"


def test_metric_cards_component_exists_and_is_rendered():
    assert (_WEB / "src/components/MetricCards.tsx").exists(), "缺少指标卡组件"
    cards = _read("src/components/MetricCards.tsx")
    assert 'data-testid="metric-cards"' in cards, "缺 testid（E2E 靠它定位）"
    assert "metrics" in cards

    chat = _read("src/components/ChatMessage.tsx")
    assert "MetricCards" in chat, "组件必须真的被渲染，否则是死代码"


def test_metric_cards_renders_nothing_when_empty():
    """空列表 → 返回 null：不能渲染一个空壳卡片占位。"""
    cards = _read("src/components/MetricCards.tsx")
    assert "length === 0" in cards or "length < 1" in cards
    assert "return null" in cards
