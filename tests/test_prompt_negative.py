"""Prompt copy guards: the planner prompt must carry tool misuse negative rules."""
from __future__ import annotations

from app.core.prompts import load_prompt


def test_planner_prompt_has_tool_misuse_guardrails():
    text = load_prompt("planner")
    assert "Tool Misuse Guardrails" in text
    assert "knowledge_search" in text and "knowledge≠数据" or "never fetch actual numbers" in text.lower() \
        or "do NOT use to fetch actual numbers" in text
    assert "Do NOT reach for a tool merely because it is available" in text
