from __future__ import annotations

import re
import time

from fastapi import APIRouter

from ...config import get_settings
from ...models.schemas import (
    DataSourceConn,
    DataSourceListResponse,
    DegradationSummary,
    HealthResponse,
    LLMProbeResponse,
)
from ...infrastructure.llm.router import fallback_events, llm_last_degraded

router = APIRouter()


def _mask_dsn(url: str) -> str:
    """脱敏数据库连接串：把 ``user:pass@host`` 中的密码替换为 ***。

    sqlite 文件路径无密码，原样返回；postgres/mysql 的显式密码必须打码——
    前端直接展示，绝不能泄漏凭证。
    """
    if url.startswith("sqlite"):
        return url
    return re.sub(r"(://[^:/?#]+:)[^@]+(@)", r"\1***\2", url)


@router.get("/datasources", response_model=DataSourceListResponse, tags=["health"])
def datasources():
    """已配置的数据库连接清单（含主源与命名源）。

    返回的是「访问配置」而非数据——每个源给 name / dialect / 脱敏 url / 只读标记。
    与「文件库」（个人上传文件）是两类不同资产，前端分两个面板呈现。
    """
    try:
        from ...core.tools.datasource import local_source_names, sources

        raw = sources()
        local_names = set(local_source_names())
    except Exception:
        raw = {}
        local_names = set()
    conns = [
        DataSourceConn(
            name=name,
            dialect=info.get("dialect", "unknown"),
            url=_mask_dsn(info.get("url", "")),
            readonly=get_settings().sql_readonly,
            origin="local" if name in local_names else "env",
        )
        for name, info in raw.items()
    ]
    return DataSourceListResponse(sources=conns)


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

    # 知识库后端：KNOWLEDGE_ENABLED 是配置意图，这里报**观测事实**。
    # 三态里的 sqlite_fallback 表示"声明启用了但 Milvus 连不上，实际在跑 SQLite"，
    # 语义检索能力是假的——这是必须能被监控看见的状态，不能混在 sqlite 里。
    try:
        from ...core.tools.knowledge_tool import kb_backend, kb_last_error

        kb_backend_name = kb_backend()
        kb_err = kb_last_error() if kb_backend_name == "sqlite_fallback" else None
    except Exception:
        kb_backend_name, kb_err = "sqlite", None

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
        knowledge_backend=kb_backend_name,
        milvus_error=kb_err,
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
