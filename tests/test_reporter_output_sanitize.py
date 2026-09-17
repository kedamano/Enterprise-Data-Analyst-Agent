"""Reporter 输出净化：**"像报告的文本"才配当报告**。

真实跑出来的缺陷（2026-09-13，`nvidia/nemotron-3-super-120b-a12b` 真跑
`r_join_amplification_guard`）：`run_reporter` 把模型输出 `{"tool": "schema", "args": {}}`
——一段**工具调用 JSON**——原样当成报告发给用户，报告长度 36 字符、findings 0。

原实现的唯一守卫是 MockLLM 的 `__markdown__` 信号（nodes.py），
即"**只防自己人**"：真实模型产出任何形状的 JSON 都会被照单全收。
这与 C.3 修的畸形 planner 输出是同一类问题——**模型输出不可信，必须有可用性判据**。

设计：
1. 输出能解析成 JSON 对象 → 不是报告（报告是 Markdown）；
   - 若是 `__markdown__` 模拟信号 → 退回模板（既有行为）；
   - 若含 `report`/`markdown`/`content`/`text` 字符串字段 → **取出内层 Markdown**（模型爱包一层）；
   - 否则 → 退回模板；
2. 退化兜底**必须可观测**（铁律 3：任何静默降级判失败）→ 记 `metadata["reporter_fallback"]`。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.nodes import run_reporter
from app.core.agents.data_analyst.state import AgentState, AnalysisResult
from app.infrastructure.llm.router import reset_llm

TOOL_CALL_BLOB = '{"tool": "schema", "args": {}}'
PLAIN_MD = "# 各品类营收分析\n\nSoftware 品类营收最高，达 1.2 亿元。"


@pytest.fixture
def llm_reporter_env(monkeypatch):
    """让 run_reporter 走 LLM 分支（而非 mock/模板直通）。"""
    monkeypatch.setenv("MOCK_LLM", "false")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _state() -> AgentState:
    s = AgentState(session_id="rep_sanitize", user_query="把订单表和商品表关联后统计各品类营收")
    s.analysis = AnalysisResult()
    return s


def _stub(monkeypatch, payload: str) -> None:
    import app.core.agents.data_analyst.nodes as nodes

    monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: payload)


# --------------------------------------------------------------------------- #
# 1. 退化输出不得当报告发出
# --------------------------------------------------------------------------- #
def test_tool_call_blob_not_accepted_as_report(llm_reporter_env, monkeypatch):
    """真跑实测的形态：模型返回工具调用 JSON，不得原样当报告。"""
    _stub(monkeypatch, TOOL_CALL_BLOB)
    state = run_reporter(_state())
    assert '"tool"' not in state.report, "工具调用 JSON 被当成报告发出去了"
    assert state.report.strip(), "兜底后报告不能为空"


def test_arbitrary_json_object_not_accepted_as_report(llm_reporter_env, monkeypatch):
    _stub(monkeypatch, '{"foo": 1}')
    state = run_reporter(_state())
    assert state.report.strip()
    assert not state.report.strip().startswith("{")


def test_reporter_fallback_is_visible_not_silent(llm_reporter_env, monkeypatch):
    """铁律 3：兜底是降级，必须留下可观测痕迹。"""
    _stub(monkeypatch, TOOL_CALL_BLOB)
    state = run_reporter(_state())
    assert state.metadata.get("reporter_fallback"), "退化兜底没有记录，属静默降级"


# --------------------------------------------------------------------------- #
# 2. 正常路径不受影响
# --------------------------------------------------------------------------- #
def test_plain_markdown_passes_through(llm_reporter_env, monkeypatch):
    _stub(monkeypatch, PLAIN_MD)
    state = run_reporter(_state())
    assert PLAIN_MD in state.report
    assert "reporter_fallback" not in (state.metadata or {})


def test_wrapped_markdown_is_unwrapped(llm_reporter_env, monkeypatch):
    """模型常把 Markdown 包在 JSON 里——应取出内层，而不是丢掉整份内容。"""
    _stub(monkeypatch, json.dumps({"report": PLAIN_MD}, ensure_ascii=False))
    state = run_reporter(_state())
    assert "# 各品类营收分析" in state.report
    assert '"report"' not in state.report


def test_mock_signal_still_falls_back(llm_reporter_env, monkeypatch):
    """既有行为不许回退：MockLLM 的 `__markdown__` 信号仍走模板。"""
    _stub(monkeypatch, '{"__markdown__": true}')
    state = run_reporter(_state())
    assert "__markdown__" not in state.report
    assert state.report.strip()
