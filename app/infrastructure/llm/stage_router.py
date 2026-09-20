"""Stage-Aware tiered LLM routing.

Routes each analysis stage to an appropriate model *tier* (cost / capability
band) instead of using the same model for everything.  Expected savings:
analyst / reporter use expensive strong models for quality; context / planner
/ executor / reflection use cheap fast models, cutting token cost 60-80%.

Tier resolution:
* Each entry in ``LLM_ROUTES`` may carry a ``tier`` field ("light" / "heavy").
* Entries without ``tier`` are classified as "legacy" (backwards-compatible).
* If a requested tier has no matching provider, we fall back to legacy,
  then to a single ``OpenAILLM`` — existing configs are unaffected.
"""
from __future__ import annotations

import logging
from typing import Optional

from ...config import Settings
from .model_router import ModelConfig, RouterLLM, parse_routes
from .router import BaseLLM, OpenAILLM

logger = logging.getLogger("da.llm")

# stage -> tier (decides which model pool to use)
STAGE_MODEL_TIER: dict[str, str] = {
    "context":     "light",
    "planner":     "light",
    "executor":    "light",
    "analyst":     "heavy",
    "reflection":  "light",
    "reporter":    "heavy",
    # fallback for unknown stages
    "__default__": "heavy",
}


def _tier_providers(settings: Settings, tier: str) -> list[ModelConfig]:
    """Filter ``llm_routes`` for entries whose ``tier`` field matches *tier*.

    * Entries **without** a ``tier`` field are classified as "legacy" (for
      backward compatibility with existing configs).
    * If *tier* has no matching entries, falls back to the legacy pool.
    * If there is no legacy pool, returns an empty list — ``StageLLM`` will
      then use its built-in ``_legacy`` ``OpenAILLM`` (which itself uses
      ``llm_model``), preserving the documented "单模型" ultimate fallback.
    * Users who never set ``tier`` are completely unaffected.
    """
    routes = parse_routes(settings.llm_routes)
    if not routes:
        return []

    # Separate entries that declare a tier from legacy ones (no tier field).
    # Keep (ModelConfig, raw_dict) pairs so we can filter by the original tier field.
    tiered: list[tuple[ModelConfig, str]] = []
    legacy: list[ModelConfig] = []
    for d in routes:
        if not isinstance(d, dict):
            continue
        mc = ModelConfig.from_dict(d, settings)
        entry_tier = d.get("tier")
        if entry_tier is None:
            legacy.append(mc)
        else:
            tiered.append((mc, entry_tier))

    # Filter the tiered pool by the requested band.
    matched = [mc for mc, t in tiered if t == tier]

    if matched:
        return matched

    # Fallback: legacy entries (no tier field at all).
    if legacy:
        return legacy

    # No tier match and no legacy pool -> empty; StageLLM falls back to _legacy.
    return []


class StageLLM(BaseLLM):
    """Tiered LLM router that picks a model pool based on the analysis stage.

    Initialises an internal ``RouterLLM`` per tier ("light" / "heavy") from
    the matching ``llm_routes`` entries.  ``vision()`` always uses the heavy
    tier (vision models are inherently expensive / capable).

    If a requested tier has no provider configuration, falls back to a plain
    ``OpenAILLM`` so that unusual stage names still work.
    """

    def __init__(self, settings: Settings) -> None:
        self._tier_routers: dict[str, BaseLLM] = {}
        for tier in ("light", "heavy"):
            providers = _tier_providers(settings, tier)
            if providers:
                self._tier_routers[tier] = RouterLLM.from_configs(providers, settings)
                logger.info("StageLLM: '%s' tier initialised with %d provider(s)",
                            tier, len(providers))
        # Legacy fallback (single OpenAILLM when no tier matches at all).
        self._legacy = OpenAILLM(settings)

    def _do_complete(
        self,
        system: str,
        user: str,
        stage: str = "",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        tier = STAGE_MODEL_TIER.get(stage, STAGE_MODEL_TIER["__default__"])
        router = self._tier_routers.get(tier)
        if router is None:
            # No matching config for this tier -> legacy fallback.
            logger.debug("StageLLM: tier '%s' has no providers, using legacy", tier)
            return self._legacy._do_complete(system, user, stage, json_mode, temperature)
        return router._do_complete(system, user, stage, json_mode, temperature)

    def vision(
        self,
        system: str,
        user: str,
        image_paths: list[str],
        stage: str = "vision",
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """Vision always routes to the heavy tier (expensive capable models)."""
        heavy = self._tier_routers.get("heavy")
        if heavy:
            return heavy.vision(system, user, image_paths, stage, json_mode, temperature)
        return self._legacy.vision(system, user, image_paths, stage, json_mode, temperature)
