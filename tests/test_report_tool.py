"""Regression tests for report rendering edge cases (string metrics etc.)."""
from __future__ import annotations

from app.core.tools.report_tool import run


def test_report_renders_string_metrics():
    """真实 LLM 可能把 metrics 返回为纯字符串列表——coerce 成 {"text": ...} 后报告仍应可读。"""
    out = run({
        "analysis": {
            "metrics": ["月度营收环比下降 12%"],  # 会被 coerce 成 {"text": ...}
            "findings": [],
        },
        "reflection": None,
        "objective": "分析营收",
    })
    assert out["ok"]
    assert "月度营收环比下降 12%" in out["report"], "字符串指标不应在报告中丢失"


def test_report_renders_structured_metrics():
    out = run({
        "analysis": {
            "metrics": [{"name": "营收", "value": "120M", "comparison": "-12%"}],
            "findings": [],
        },
        "reflection": None,
        "objective": "分析营收",
    })
    assert out["ok"]
    assert "120M" in out["report"] and "-12%" in out["report"]
