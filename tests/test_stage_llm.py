"""TDD for Stage-Aware tiered LLM routing (StageLLM).

Contract:
* stage -> tier mapping: context/planner/executor/reflection -> "light";
  analyst/reporter -> "heavy".
* When llm_routes entries carry a ``tier`` field, get_llm() returns StageLLM.
* When no ``tier`` field is present, legacy RouterLLM is used (backwards compat).
* vision() always routes to the heavy tier.
* If a tier has no matching providers, StageLLM falls back to legacy OpenAILLM.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.infrastructure.llm.model_router import ModelConfig, RouterLLM, parse_routes
from app.infrastructure.llm.router import (
    BaseLLM,
    OpenAILLM,
    get_llm,
    reset_llm,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class StubLLM(BaseLLM):
    """Minimal LLM stub that records calls."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        self.calls: list[dict] = []
        self.vision_calls: list[dict] = []

    def _do_complete(self, system, user, stage="", json_mode=False, temperature=None) -> str:
        self.calls.append({"stage": stage, "json_mode": json_mode, "temperature": temperature})
        return f"ok-{self.name}"

    def vision(self, system, user, image_paths, stage="vision", json_mode=False,
               temperature=None) -> str:
        self.vision_calls.append({"stage": stage, "image_paths": image_paths})
        return f"vision-{self.name}"


def _settings_with_routes(routes: list[dict], **overrides) -> Settings:
    """Build a Settings with llm_routes set to *routes*."""
    base = dict(llm_api_key="sk-dummy", mock_llm=False)
    base.update(overrides)
    return Settings(llm_routes=routes, **base)


# ---------------------------------------------------------------------------
# 1. tier routing: light
# ---------------------------------------------------------------------------

def test_stage_llm_routes_to_light_tier():
    """stage=planner maps to 'light' tier -> RouterLLM built from light routes."""
    routes = [
        {"model": "cheap-a", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "cheap-b", "base_url": "http://b", "api_key": "kb",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    stage_llm = get_llm(settings)

    # Track which providerRouterLLM.complete invokes. We mock RouterLLM.from_configs
    # so we can inspect the configs passed for the light tier.
    with patch("app.infrastructure.llm.stage_router.RouterLLM") as MockRouterLLM:
        fake_light = StubLLM("light-router")
        MockRouterLLM.from_configs.return_value = fake_light

        # Re-init StageLLM with the mock in place.
        from app.infrastructure.llm.stage_router import StageLLM
        stage = StageLLM(settings)

        # planner -> light tier
        result = stage.complete("sys", "user", stage="planner", json_mode=True)
        assert result == "ok-light-router"
        assert len(fake_light.calls) == 1
        assert fake_light.calls[0]["stage"] == "planner"


# ---------------------------------------------------------------------------
# 2. tier routing: heavy
# ---------------------------------------------------------------------------

def test_stage_llm_routes_to_heavy_tier():
    """stage=analyst maps to 'heavy' tier."""
    routes = [
        {"model": "cheap-a", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    with patch("app.infrastructure.llm.stage_router.RouterLLM") as MockRouterLLM:
        # Return distinct stubs per "tier" so we can confirm which one is called.
        fake_light = StubLLM("light-router")
        fake_heavy = StubLLM("heavy-router")
        call_log: list[str] = []

        def _from_configs(configs, st):
            # The tier is determined by which model names are in *configs*.
            models = {c.model for c in configs}
            if "heavy-x" in models:
                call_log.append("heavy")
                return fake_heavy
            call_log.append("light")
            return fake_light

        MockRouterLLM.from_configs.side_effect = _from_configs

        from app.infrastructure.llm.stage_router import StageLLM
        stage = StageLLM(settings)

        result = stage.complete("sys", "user", stage="analyst", json_mode=True)
        assert result == "ok-heavy-router"
        assert "heavy" in call_log
        assert len(fake_heavy.calls) == 1
        assert fake_heavy.calls[0]["stage"] == "analyst"


# ---------------------------------------------------------------------------
# 3. fallback: light tier missing
# ---------------------------------------------------------------------------

def test_stage_llm_fallback_to_legacy_when_no_tier_match():
    """Routes only have a 'heavy' tier; stage=planner (light) should fallback."""
    routes = [
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    with patch("app.infrastructure.llm.stage_router.RouterLLM") as MockRouterLLM, \
         patch("app.infrastructure.llm.stage_router.OpenAILLM") as MockOpenAILLM:

        MockRouterLLM.from_configs.return_value = StubLLM("heavy")
        fake_legacy = StubLLM("legacy")
        MockOpenAILLM.return_value = fake_legacy

        from app.infrastructure.llm.stage_router import StageLLM
        stage = StageLLM(settings)

        # planner -> light tier, but only heavy is configured -> fallback legacy
        result = stage.complete("sys", "user", stage="planner")
        assert result == "ok-legacy"
        assert len(fake_legacy.calls) == 1


# ---------------------------------------------------------------------------
# 4. vision always goes heavy
# ---------------------------------------------------------------------------

def test_stage_llm_vision_goes_heavy():
    """vision() always uses the heavy-tier router."""
    routes = [
        {"model": "cheap-a", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    with patch("app.infrastructure.llm.stage_router.RouterLLM") as MockRouterLLM:
        fake_light = StubLLM("light")
        fake_heavy = StubLLM("heavy")
        call_log: list[str] = []

        def _from_configs(configs, st):
            models = {c.model for c in configs}
            if "heavy-x" in models:
                call_log.append("heavy")
                return fake_heavy
            call_log.append("light")
            return fake_light

        MockRouterLLM.from_configs.side_effect = _from_configs

        from app.infrastructure.llm.stage_router import StageLLM
        stage = StageLLM(settings)

        result = stage.vision("sys", "user", image_paths=["/tmp/img.png"],
                              stage="vision", json_mode=True)
        assert result == "vision-heavy"
        assert len(fake_heavy.vision_calls) == 1
        assert fake_heavy.vision_calls[0]["image_paths"] == ["/tmp/img.png"]
        # The light router must NOT have received a vision call.
        assert len(fake_light.vision_calls) == 0


# ---------------------------------------------------------------------------
# 5. get_llm: no tier -> RouterLLM
# ---------------------------------------------------------------------------

def test_get_llm_no_tier_config_uses_old_router():
    """llm_routes without any tier field -> get_llm() returns RouterLLM."""
    routes = [
        {"model": "deepseek-chat", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1},
        {"model": "gpt-4o", "base_url": "http://b", "api_key": "kb",
         "priority": 1, "weight": 1},
    ]
    settings = _settings_with_routes(routes)

    reset_llm()
    llm = get_llm(settings)
    assert isinstance(llm, RouterLLM)
    # Not StageLLM.
    from app.infrastructure.llm.stage_router import StageLLM
    assert not isinstance(llm, StageLLM)
    reset_llm()


# ---------------------------------------------------------------------------
# 6. get_llm: with tier -> StageLLM
# ---------------------------------------------------------------------------

def test_get_llm_with_tier_config_uses_stage_llm():
    """llm_routes with a tier field -> get_llm() returns StageLLM."""
    routes = [
        {"model": "cheap-a", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    reset_llm()
    llm = get_llm(settings)
    from app.infrastructure.llm.stage_router import StageLLM
    assert isinstance(llm, StageLLM)
    reset_llm()


# ---------------------------------------------------------------------------
# 7. _tier_providers parsing
# ---------------------------------------------------------------------------

def test_tier_providers_parses_tier_field():
    """_tier_providers filters llm_routes by the tier field correctly."""
    routes = [
        {"model": "cheap-a", "base_url": "http://a", "api_key": "ka",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "cheap-b", "base_url": "http://b", "api_key": "kb",
         "priority": 0, "weight": 1, "tier": "light"},
        {"model": "heavy-x", "base_url": "http://x", "api_key": "kx",
         "priority": 0, "weight": 1, "tier": "heavy"},
    ]
    settings = _settings_with_routes(routes)

    from app.infrastructure.llm.stage_router import _tier_providers

    light = _tier_providers(settings, "light")
    assert len(light) == 2
    assert {c.model for c in light} == {"cheap-a", "cheap-b"}

    heavy = _tier_providers(settings, "heavy")
    assert len(heavy) == 1
    assert heavy[0].model == "heavy-x"
