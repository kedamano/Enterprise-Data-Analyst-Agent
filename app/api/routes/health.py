from __future__ import annotations

import time

from fastapi import APIRouter

from ...config import get_settings
from ...models.schemas import DegradationSummary, HealthResponse, LLMProbeResponse
from ...infrastructure.llm.router import fallback_events, llm_last_degraded

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health():
    """服务健康 + **观测到的** LLM 状态。

    DEGRADE/01：`mock_llm` 是配置意图，`llm_degraded` 是观测事实。两者必须分开报——
    D18 的坑就是真实 key 全被 402 拒绝、全程 mock 兜底，而 `/health` 只回配置值，
    对外表现得一切正常。
    """
    s = get_settings()
    events = fallback_events()
    last = events[-1] if events else None
    # P0-4：把原始事件流翻译成「哪一级降了 / 为什么 / 影响什么」。
    # 此前只回 llm_fallbacks_total 一个数字，排障要自己去翻日志。
    try:
        from ...infrastructure.llm.degradation import summarize_degradation

        deg = summarize_degradation(events)
        deg_model = DegradationSummary.model_validate(deg)
        by_stage: dict[str, int] = {}
        for e in events:
            key = str(e.get("stage") or "unknown")
            by_stage[key] = by_stage.get(key, 0) + 1
    except Exception:
        deg_model, by_stage = None, {}
    try:
        from ...core.tools.datasource import available_sources

        source_names = available_sources()
    except Exception:
        source_names = []

    return HealthResponse(
        data_sources=source_names,
        status="ok",
        mock_llm=s.use_mock_llm,
        data_source=s.data_db_url,
        llm_mode="mock" if s.use_mock_llm else "real",
        llm_degraded=llm_last_degraded(),
        llm_fallbacks_total=len(events),
        llm_last_error=(last or {}).get("error"),
        llm_degraded_stages=[st.stage for st in (deg_model.stages if deg_model else [])],
        llm_degradation=deg_model,
        llm_fallbacks_by_stage=by_stage,
    )


@router.get("/health/llm", response_model=LLMProbeResponse, tags=["health"])
def health_llm():
    """主动探测 LLM 真实可达性（1 token，绝不降级）。

    刻意**不**走 `get_llm().complete()`：那条路径失败会降级为 Mock 并返回内容，
    探测就被自己的兜底骗过了。这里直接调用、异常即 false，且不写降级事件、
    不影响熔断器状态（否则探测本身会把熔断器打开）。

    有成本（1 token/次），故独立端点，不并入 `/health` 自动轮询。
    """
    s = get_settings()
    if s.use_mock_llm:
        return LLMProbeResponse(reachable=True, model="mock", latency_ms=0.0)
    try:
        from openai import OpenAI
    except ImportError as exc:
        return LLMProbeResponse(reachable=False, model=s.llm_model, error=f"openai 未安装: {exc}")
    started = time.time()
    try:
        client = OpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url)
        client.chat.completions.create(
            model=s.llm_model,
            max_tokens=1,
            timeout=min(15, s.llm_timeout_s),
            messages=[{"role": "user", "content": "ping"}],
        )
        return LLMProbeResponse(reachable=True, model=s.llm_model,
                                latency_ms=round((time.time() - started) * 1000, 1))
    except Exception as exc:
        return LLMProbeResponse(reachable=False, model=s.llm_model,
                                latency_ms=round((time.time() - started) * 1000, 1),
                                error=str(exc)[:300])
