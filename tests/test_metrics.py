"""可观测性指标测试（#3）。

验证：Metrics 寄存器能正确累加 counter / 直方图分位 / gauge；`render_prometheus`
产出 Prometheus 兼容文本；OTel 导出在 SDK 缺失时静默 no-op（不抛异常）；
`/metrics` 端点可抓取并包含核心指标。
"""
from __future__ import annotations

import time

from app.infrastructure.observability import metrics as m


def test_counter_and_histogram():
    reg = m.Metrics()
    reg.inc("http_requests_total", 3)
    assert reg.snapshot()["counters"]["http_requests_total"] == 3
    for v in (0.1, 0.2, 0.5, 1.0, 2.0):
        reg.observe("http_request_duration_seconds", v)
    snap = reg.snapshot()
    h = snap["histograms"]["http_request_duration_seconds"]
    assert h["n"] == 5
    assert h["p95"] >= 1.0  # 5 个样本 P95 接近最大值
    assert h["max"] == 2.0


def test_gauge():
    reg = m.Metrics()
    reg.set_gauge("llm_cost_usd_total", 1.23)
    assert reg.snapshot()["gauges"]["llm_cost_usd_total"] == 1.23


def test_render_prometheus_format():
    reg = m.Metrics()
    reg.inc("tool_calls_total", 2)
    reg.observe("tool_call_duration_seconds", 0.3)
    text = reg.render_prometheus()
    assert "tool_calls_total 2" in text
    assert "# TYPE" in text
    assert "tool_call_duration_seconds" in text


def test_pctl_edge_cases():
    assert m._pctl([], 95) == 0.0
    assert m._pctl([5.0], 95) == 5.0


def test_export_otel_missing_sdk_is_noop():
    # 未安装 opentelemetry SDK 时必须返回 False 且不抛异常
    assert m.export_otel() is False


def test_metrics_endpoint_exposes_core_metrics():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    # 先打一个请求，制造一点指标
    c.get("/api/v1/health")
    resp = c.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
