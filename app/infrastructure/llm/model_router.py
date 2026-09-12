"""Multi-provider weighted model router with failover (project-python model_router).

* Each provider = a ``ModelConfig`` with ``priority`` (lower = tried first) and
  ``weight`` (relative share within its priority band).
* Per call the candidates are ordered: highest-priority band first, weighted
  (no-replacement) random within the band, then lower bands.
* ``RouterLLM`` walks that order and returns the first success; if every
  candidate fails it either re-raises (``LLM_NO_FALLBACK``) or degrades to
  MockLLM + records a fallback event (same contract as a single OpenAILLM).

Every candidate is itself an ``OpenAILLM`` with its own three-state circuit
breaker, so a sick provider trips OPEN and is skipped fast on later calls.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Optional

from ...config import Settings
from .router import BaseLLM, MockLLM, OpenAILLM, record_fallback

logger = logging.getLogger("da.llm")


@dataclass(frozen=True)
class ModelConfig:
    model: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    priority: int = 0          # 越小越优先
    weight: float = 1.0        # 同优先级带内权重

    @classmethod
    def from_dict(cls, d: dict, defaults: Settings) -> "ModelConfig":
        return cls(
            model=d["model"],
            base_url=d.get("base_url") or defaults.llm_base_url,
            api_key=d.get("api_key") or defaults.llm_api_key,
            priority=int(d.get("priority", 0)),
            weight=float(d.get("weight", 1.0)),
        )


def parse_routes(raw: Any) -> list[dict]:
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM_ROUTES 不是合法 JSON: {exc}") from exc
    return list(raw or [])


def _weighted_permutation(items: list[ModelConfig], rng: random.Random) -> list[ModelConfig]:
    """同优先级带内按 weight 无放回加权打乱。"""
    out: list[ModelConfig] = []
    pool = list(items)
    weights = {id(c): max(0.0001, c.weight) for c in pool}
    while pool:
        total = sum(weights[id(c)] for c in pool)
        r = rng.uniform(0, total)
        acc = 0.0
        chosen = pool[-1]
        for c in pool:
            acc += weights[id(c)]
            if r <= acc:
                chosen = c
                break
        out.append(chosen)
        pool.remove(chosen)
    return out


def ordered_candidates(configs: list[ModelConfig],
                       rng: Optional[random.Random] = None) -> list[ModelConfig]:
    """高优先级在前；带内加权随机。返回每个候选恰好一次。"""
    rng = rng or random.Random()
    bands: dict[int, list[ModelConfig]] = {}
    for c in configs:
        bands.setdefault(c.priority, []).append(c)
    result: list[ModelConfig] = []
    for prio in sorted(bands):
        result.extend(_weighted_permutation(bands[prio], rng))
    return result


def build_candidates(configs: list[ModelConfig], defaults: Settings) -> list[OpenAILLM]:
    """为每个配置构建一个自带熔断、内部禁止降级的 OpenAILLM 候选。"""
    out = []
    for c in configs:
        s = defaults.model_copy(update={
            "llm_model": c.model,
            "llm_base_url": c.base_url,
            "llm_api_key": c.api_key or "",
            # 候选内部绝不自降级到 Mock；降级决策统一由 RouterLLM 做。
            "llm_no_fallback": True,
        })
        llm = OpenAILLM(s)
        llm._config = c  # type: ignore[attr-defined]
        out.append(llm)
    return out


class RouterLLM(BaseLLM):
    """故障转移路由器：每次调用按 priority/weight 重排 providers，失败换下一个。

    ``providers`` 为 (ModelConfig, BaseLLM) 二元组；测试可注入带任意名字的桩。
    """

    def __init__(self, providers: list[tuple[ModelConfig, BaseLLM]],
                 settings: Settings, rng: Optional[random.Random] = None) -> None:
        self.providers = providers
        self.settings = settings
        self.rng = rng or random.Random()

    @classmethod
    def from_configs(cls, configs: list[ModelConfig],
                     settings: Settings) -> "RouterLLM":
        llms = build_candidates(configs, settings)
        return cls(list(zip(configs, llms)), settings)

    def complete(self, system: str, user: str, stage: str = "",
                 json_mode: bool = False,
                 temperature: Optional[float] = None) -> str:
        if not self.providers:
            raise RuntimeError("RouterLLM 无候选模型")
        configs = [c for c, _ in self.providers]
        by_config = dict(self.providers)

        last: Exception | None = None
        for cfg in ordered_candidates(configs, self.rng):
            cand = by_config[cfg]
            try:
                return cand.complete(system, user, stage, json_mode, temperature)
            except Exception as exc:  # 候选自带熔断/重试，失败即换下一个
                last = exc
                logger.warning("model candidate failed (%s): %s", cfg.model, exc)

        # 全部失败 → 按单模型契约降级或上抛
        if self.settings.llm_no_fallback:
            raise last or RuntimeError("all model candidates failed")
        logger.error("all %d model candidates failed; falling back to mock", len(self.providers))
        record_fallback(stage, last or RuntimeError("all candidates failed"))
        return MockLLM().complete(system, user, stage, json_mode, temperature)
