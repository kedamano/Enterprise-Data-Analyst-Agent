"""Tool output volume capping — huge stdout/rows must be truncated centrally."""
from __future__ import annotations

import app.core.tools as tools


def test_huge_tool_output_is_capped(monkeypatch):
    def _bloat(params):
        return {
            "ok": True,
            "stdout": "x" * 30000,
            "rows": [{"i": i} for i in range(5000)],
        }

    monkeypatch.setitem(tools.REGISTRY, "bloat_tool", _bloat)
    try:
        res = tools.execute_tool("cap1", "bloat_tool", {})
        assert res.status == "SUCCESS", res.error
        out = res.output
        assert len(out["stdout"]) <= 20000 + len("…[截断]"), "stdout 应被截断"
        assert "截断" in out["stdout"]
        # rows：2000 + 1 个截断标记行
        assert len(out["rows"]) == 2001, f"rows 应截到 2000+标记，实际 {len(out['rows'])}"
        assert out["rows"][-1].get("_truncated") == 3000
    finally:
        tools.REGISTRY.pop("bloat_tool", None)


def test_normal_output_untouched(monkeypatch):
    def _small(params):
        return {"ok": True, "rows": [{"a": 1}], "stdout": "ok"}

    monkeypatch.setitem(tools.REGISTRY, "small_tool", _small)
    try:
        res = tools.execute_tool("cap2", "small_tool", {})
        assert res.output["rows"] == [{"a": 1}]
        assert res.output["stdout"] == "ok"
    finally:
        tools.REGISTRY.pop("small_tool", None)
