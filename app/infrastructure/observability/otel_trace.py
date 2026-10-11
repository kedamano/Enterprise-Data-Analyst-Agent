"""OpenTelemetry trace export (opt-in).

当配置 OTEL_EXPORTER_OTLP_ENDPOINT 时，每个 Span 完成即推送到 OTel Collector → Jaeger/Tempo。
未配置或 SDK 未安装时静默 no-op（tracing JSONL 仍是事实来源）。

使用：无需修改调用方。Tracer.end() 自动调 ``emit_span()``；
失败一条不打（fail-open），导出错误只打 debug 日志。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger("da.otel")

# --------------------------------------------------------------------------- #
# 懒加载的 SDK 组件（仅在 OTEL_EXPORTER_OTLP_ENDPOINT 已配时初始化一次）
# --------------------------------------------------------------------------- #
_tracer_provider: Any = None
_span_exporter: Any = None
_otel_service_name: str = "edaa"


def _init_otel() -> bool:
    """一次性初始化 OTel TracerProvider + BatchSpanProcessor。返回是否成功。"""
    global _tracer_provider, _span_exporter
    if _tracer_provider is not None:
        return True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry import trace as otel_trace

        _span_exporter = OTLPSpanExporter(endpoint=endpoint)
        resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", _otel_service_name)})
        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(BatchSpanProcessor(_span_exporter))
        otel_trace.set_tracer_provider(_tracer_provider)
        logger.info("OTel trace export 初始化成功 → %s", endpoint)
        return True
    except ImportError:
        logger.debug("opentelemetry SDK / OTLP exporter 未安装 → 跳过 OTel trace export")
        _tracer_provider = False  # 标记为不可用，不再重试
        return False
    except Exception as exc:
        logger.warning("OTel trace export 初始化失败（静默 no-op）：%s", exc)
        _tracer_provider = False
        return False


def is_enabled() -> bool:
    """OTel 导出是否可用（配置了 endpoint 且 SDK 已成功加载）。"""
    if _tracer_provider is None:
        _init_otel()
    return _tracer_provider is not None and _tracer_provider is not False


def emit_span(stage: str, trace_id: str, span_id: str, status: str,
              duration_ms: float, error: Optional[str] = None,
              started_at: Optional[float] = None,
              prompt_tokens: Optional[int] = None,
              completion_tokens: Optional[int] = None) -> None:
    """把一条结构化 span 推送到 OTel Collector（异步 BatchSpanProcessor 缓冲）。"""
    if not is_enabled():
        return
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status as OtelStatus, StatusCode

        tracer = otel_trace.get_tracer("edaa")
        name = f"edaa.{stage}"
        ctx = tracer.start_span(
            name,
            kind=SpanKind.INTERNAL,
            start_time=int((started_at or time.time()) * 1e9),
        )
        # 用 setattr 设置属性（兼容不同 SDK 版本）
        try:
            ctx.set_attribute("edaa.stage", stage)
            ctx.set_attribute("edaa.trace_id", trace_id)
            ctx.set_attribute("edaa.span_id", span_id)
            ctx.set_attribute("edaa.status", status)
            if prompt_tokens is not None:
                ctx.set_attribute("llm.prompt_tokens", prompt_tokens)
            if completion_tokens is not None:
                ctx.set_attribute("llm.completion_tokens", completion_tokens)
            if error:
                ctx.set_attribute("error.message", error)
            if status == "ERROR":
                ctx.set_status(OtelStatus(StatusCode.ERROR, error or ""))
            ctx.end(end_time=int(time.time() * 1e9))
        except Exception:
            ctx.end()
    except Exception as exc:
        logger.debug("OTel span emit 失败（静默 no-op）：%s", exc)


def shutdown() -> None:
    """关闭 OTel exporter（进程退出前调用，flush 剩余 span）。"""
    global _tracer_provider, _span_exporter
    if _tracer_provider and _tracer_provider is not True and _tracer_provider is not False:
        try:
            _tracer_provider.force_flush()
            logger.debug("OTel exporter force_flush done")
        except Exception as exc:
            logger.debug("OTel force_flush 失败（静默）：%s", exc)
    _tracer_provider = None
    _span_exporter = None
