"""Audit logging: high-risk marker, sanitized params, resilient to failures."""
from __future__ import annotations

import json

import pytest

from app.core.tools import execute_tool
from app.core.tools.__init__ import _sanitize


@pytest.fixture
def audit_path(tmp_path, monkeypatch):
    target = tmp_path / "tool_audit.jsonl"
    monkeypatch.setattr("app.core.tools.AUDIT_LOG", target)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _records(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_high_risk_python_analysis_is_marked(audit_path):
    execute_tool("a1", "python_analysis",
                 {"code": "print(_json.dumps({'rows': 0}))"}, "aud_sess")
    recs = _records(audit_path)
    assert recs and recs[-1]["tool"] == "python_analysis"
    rec = recs[-1]
    assert rec["high_risk"] is True
    assert rec["params_sanitized"].get("code")  # 代码本身非机密，不脱敏为 ***
    assert rec["status"] in ("SUCCESS", "FAILED")
    assert rec["permission"] == "COMPUTE"


def test_read_tool_is_not_high_risk(audit_path):
    execute_tool("a2", "schema_search", {"keyword": "revenue"}, "aud_sess")
    recs = _records(audit_path)
    assert recs and recs[-1]["tool"] == "schema_search"
    assert recs[-1]["high_risk"] is False


def test_sanitize_redacts_secrets_and_truncates():
    clean = _sanitize({"api_key": "sk-abc", "sql": "SELECT 1", "nested": {"token": "x", "ok": 1}})
    assert clean["api_key"] == "***"
    assert clean["sql"] == "SELECT 1"
    assert clean["nested"]["token"] == "***" and clean["nested"]["ok"] == 1
    big = _sanitize({"sql": "L" * 3000})
    assert "截断" in big["sql"] and len(big["sql"]) < 2000


def test_audit_failure_never_breaks_tool(tmp_path, monkeypatch):
    # 审计路径不可写（父路径被文件占位）→ _audit 内部吞错，工具仍成功
    blocker = tmp_path / "f.json"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr("app.core.tools.AUDIT_LOG", blocker / "x.jsonl")
    res = execute_tool("a3", "schema_search", {"keyword": "revenue"}, "aud_sess")
    assert res.status == "SUCCESS", res.error
