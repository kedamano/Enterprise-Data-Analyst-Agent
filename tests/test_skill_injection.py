"""技能注入链路契约：勾选 → metadata → context 提示词。

关注点：
- **只有**勾选且**已启用**的技能进入提示词；
- **没勾选时行为与改造前一字不差**（metadata 不写入任何东西）；
- 已删除/不存在的 id 静默跳过，不制造空块。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.core.agents.data_analyst.state import ContextModel
from app.core.skills import get_skill_store


@pytest.fixture
def skills(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "skills_dir", str(tmp_path / "skills"))
    return get_skill_store()


def test_new_state_attaches_selected_skill(skills):
    from app.core.agents.data_analyst.graph import _new_state

    item = skills.create("营收口径规范", "统一口径", "营收 = SUM(paid_amount)")
    st = _new_state("s1", "统计营收", [], False, [item["id"]])
    assert st.metadata.get("skill_ids") == [item["id"]]
    assert "营收口径规范" in st.metadata.get("skills_text", "")
    assert "SUM(paid_amount)" in st.metadata.get("skills_text", "")


def test_disabled_skill_is_not_injected(skills):
    from app.core.agents.data_analyst.graph import _new_state

    item = skills.create("已停用技能", "", "不该出现")
    skills.set_enabled(item["id"], False)
    st = _new_state("s2", "q", [], False, [item["id"]])
    assert st.metadata.get("skills_text") is None


def test_no_selection_leaves_metadata_untouched(skills):
    from app.core.agents.data_analyst.graph import _new_state

    skills.create("某技能", "", "x")
    st = _new_state("s3", "q")
    assert "skills_text" not in st.metadata
    assert "skill_ids" not in st.metadata


def test_unknown_skill_id_is_skipped(skills):
    from app.core.agents.data_analyst.graph import _new_state

    st = _new_state("s4", "q", [], False, ["not-a-real-skill"])
    assert st.metadata.get("skills_text") is None


def test_max_per_request_caps_selection(skills, monkeypatch):
    from app.core.agents.data_analyst.graph import _new_state

    monkeypatch.setattr(get_settings(), "skill_max_per_request", 2)
    ids = [skills.create(f"技能{i}", "", f"内容{i}")["id"] for i in range(4)]
    st = _new_state("s5", "q", [], False, ids)
    assert len(st.metadata.get("skill_ids", [])) == 2


def test_run_context_payload_includes_active_skills(skills, monkeypatch):
    """端到端（到提示词为止）：run_context 必须把技能正文带进发给模型的消息里。"""
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state

    item = skills.create("营收口径规范", "统一口径", "营收 = SUM(paid_amount)")
    st = _new_state("s6", "统计营收", [], False, [item["id"]])

    captured: dict[str, str] = {}

    def fake_llm(model_cls, stage, user, *, ok=None, fallback=None, retries=1):
        captured["stage"] = stage
        captured["user"] = user
        return ContextModel(objective="统计营收"), None

    monkeypatch.setattr(nodes, "_llm_model", fake_llm)
    nodes.run_context(st)

    assert captured.get("stage") == "context"
    assert "营收口径规范" in captured["user"]
    assert "SUM(paid_amount)" in captured["user"]


def test_run_context_without_skills_has_no_skill_block(skills, monkeypatch):
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state

    st = _new_state("s7", "统计营收")

    captured: dict[str, str] = {}

    def fake_llm(model_cls, stage, user, *, ok=None, fallback=None, retries=1):
        captured["user"] = user
        return ContextModel(objective="统计营收"), None

    monkeypatch.setattr(nodes, "_llm_model", fake_llm)
    nodes.run_context(st)
    assert "active_skills" not in captured["user"]
    assert "<skill" not in captured["user"]


# --------------------------------------------------------------------------- #
# 技能必须到达**所有产出内容的阶段**，而不只是 context。
#
# 真实缺陷（2026-09-17 live 复现）：技能正文只塞进了 run_context 的 payload，
# planner / analyst / reporter 各自另建 payload、都没带技能 —— 于是用户勾了技能，
# 最终报告里看不出任何差别（"看着接通、实际无效"）。
# 用带唯一标记的技能正文，逐一在三个阶段抓取真正发给模型的消息。
# --------------------------------------------------------------------------- #
_MARK = "SXOPS-7133"


@pytest.fixture
def marker_skill(skills):
    return skills.create("标记技能", "输出约束", f"报告必须原样包含 {_MARK}")


def test_skills_reach_planner_prompt(skills, marker_skill, monkeypatch):
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state
    from app.core.agents.data_analyst.state import PlanModel

    st = _new_state("s8", "统计营收", [], False, [marker_skill["id"]])
    captured: dict[str, str] = {}

    def fake_llm(model_cls, stage, user, *, ok=None, fallback=None, retries=1):
        captured["stage"] = stage
        captured["user"] = user
        return PlanModel(steps=[]), None

    monkeypatch.setattr(nodes, "_llm_model", fake_llm)
    nodes.run_planner(st)
    assert captured.get("stage") == "planner"
    assert _MARK in captured["user"], "planner 提示词里没有技能正文"


def test_skills_reach_analyst_prompt(skills, marker_skill, monkeypatch):
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state
    from app.core.agents.data_analyst.state import AnalysisResult

    st = _new_state("s9", "统计营收", [], False, [marker_skill["id"]])
    captured: dict[str, str] = {}

    def fake_llm(model_cls, stage, user, *, ok=None, fallback=None, retries=1):
        captured["stage"] = stage
        captured["user"] = user
        return AnalysisResult(), None

    monkeypatch.setattr(nodes, "_llm_model", fake_llm)
    nodes.run_analyst(st)
    assert captured.get("stage") == "analyst"
    assert _MARK in captured["user"], "analyst 提示词里没有技能正文"


def test_skills_reach_reporter_prompt(skills, marker_skill, monkeypatch):
    """报告是用户唯一直接看到的东西——技能到不了这里就等于没生效。"""
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state
    from app.infrastructure.llm.router import reset_llm

    # ⚠️ 先建 state：此时 settings.skills_dir 还指向 skills fixture 的临时目录。
    # 若先 cache_clear() 再建 state，skills_dir 会回落成真实 data/skills，
    # 技能 id 查不到 → 静默测不到东西（这个顺序错误我先踩过一次）。
    st = _new_state("s10", "统计营收", [], False, [marker_skill["id"]])
    monkeypatch.setenv("MOCK_LLM", "false")
    get_settings.cache_clear()
    reset_llm()
    try:
        captured: dict[str, str] = {}

        def fake_llm(stage, user, json_mode=True):
            captured["stage"] = stage
            captured["user"] = user
            return "## 报告\n\n营收 100。"

        monkeypatch.setattr(nodes, "_llm", fake_llm)
        nodes.run_reporter(st)
        assert captured.get("stage") == "reporter"
        assert _MARK in captured["user"], "reporter 提示词里没有技能正文"
    finally:
        get_settings.cache_clear()
        reset_llm()


def test_reporter_prompt_has_no_skill_block_when_unselected(skills, monkeypatch):
    """未勾选时必须与改造前一字不差——不许凭空多出 skills 字段。"""
    from app.core.agents.data_analyst import nodes
    from app.core.agents.data_analyst.graph import _new_state
    from app.infrastructure.llm.router import reset_llm

    skills.create("某技能", "", "x")
    st = _new_state("s11", "统计营收")  # 同上：先建 state 再清 settings 缓存
    monkeypatch.setenv("MOCK_LLM", "false")
    get_settings.cache_clear()
    reset_llm()
    try:
        captured: dict[str, str] = {}
        monkeypatch.setattr(nodes, "_llm", lambda stage, user, json_mode=True: (
            captured.update(user=user) or "## 报告\n\n营收 100。"))
        nodes.run_reporter(st)
        assert "active_skills" not in captured["user"]
        assert "<skill" not in captured["user"]
    finally:
        get_settings.cache_clear()
        reset_llm()
