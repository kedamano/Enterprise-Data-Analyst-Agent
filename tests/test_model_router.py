"""TDD for the multi-provider weighted model router.

Contract:
* Higher ``priority`` provider is tried before lower ones (failover).
* Within a priority band, providers are picked weighted-random (weight).
* If every candidate fails: ``LLM_NO_FALLBACK=1`` → raise; otherwise degrade to
  Mock and record the fallback (same contract as single OpenAILLM).
"""
from __future__ import annotations

import random

import pytest

from app.config import Settings
from app.infrastructure.llm.model_router import (
    ModelConfig,
    RouterLLM,
    ordered_candidates,
)
from app.infrastructure.llm.router import BaseLLM, fallback_occurred, reset_fallback_events


class StubLLM(BaseLLM):
    """可注入失败/计数的假模型候选。"""

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0

    def _do_complete(self, system, user, stage="", json_mode=False, temperature=None) -> str:
        self.calls += 1
        if self.fail:
            raise ConnectionError(f"{self.name} down")
        return f"ok-{self.name}"


def _router(providers: list[tuple[ModelConfig, BaseLLM]], *, no_fallback: bool = False,
            seed: int = 7) -> RouterLLM:
    settings = Settings(llm_api_key="sk-dummy", llm_no_fallback=no_fallback, mock_llm=False)
    return RouterLLM(providers, settings, rng=random.Random(seed))


def _prov(a: StubLLM, prio: int = 0, weight: float = 1.0) -> tuple[ModelConfig, BaseLLM]:
    return (ModelConfig(model=a.name, priority=prio, weight=weight), a)


def test_failover_to_next_priority_on_failure():
    a, b = StubLLM("primary", fail=True), StubLLM("backup")
    r = _router([_prov(a, 0), _prov(b, 1)])
    out = r.complete("sys", "user", stage="context", json_mode=True)
    assert out == "ok-backup"
    assert a.calls == 1 and b.calls == 1


def test_higher_priority_wins_when_healthy():
    a, b = StubLLM("primary"), StubLLM("backup")
    r = _router([_prov(a, 0), _prov(b, 1)])
    for _ in range(50):
        assert r.complete("s", "u") == "ok-primary"
    assert b.calls == 0, "主候选健康时不应降级到备选"


def test_weight_distribution_within_band():
    heavy, light = StubLLM("heavy"), StubLLM("light")
    r = _router([_prov(heavy, 0, weight=9.0), _prov(light, 0, weight=1.0)], seed=42)
    for _ in range(200):
        r.complete("s", "u")
    assert heavy.calls > light.calls, f"heavy={heavy.calls} light={light.calls}，重者应更常被选中"


def test_ordered_candidates_respects_priority_and_no_dups():
    cfg = [ModelConfig(model="a", priority=1), ModelConfig(model="b", priority=0),
           ModelConfig(model="c", priority=0)]
    rng = random.Random(0)
    for _ in range(50):
        order = ordered_candidates(cfg, rng)
        assert order[0].model in ("b", "c"), "高优先级(b/c, prio0)应排最前"
        assert len(order) == 3 and len({c.model for c in order}) == 3


def test_all_fail_degrades_to_mock_and_records_fallback():
    reset_fallback_events()
    a, b = StubLLM("a", fail=True), StubLLM("b", fail=True)
    r = _router([_prov(a, 0), _prov(b, 1)], no_fallback=False)
    out = r.complete("sys", "user", stage="planner", json_mode=True)
    assert isinstance(out, str) and out, "降级应返回 mock 响应"
    assert fallback_occurred() is True, "全候选失败应被 fallback spy 记录"


def test_all_fail_raises_when_no_fallback():
    reset_fallback_events()
    a, b = StubLLM("a", fail=True), StubLLM("b", fail=True)
    r = _router([_prov(a, 0), _prov(b, 1)], no_fallback=True)
    with pytest.raises(ConnectionError):
        r.complete("sys", "user", stage="planner")
    assert fallback_occurred() is False
