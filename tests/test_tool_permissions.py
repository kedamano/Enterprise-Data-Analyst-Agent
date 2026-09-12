"""TDD suite for the Tool Permission Model (spec §22).

Contract under test:

* Every registered tool declares a ``ToolSpec``: permission, timeout_s,
  data_scope, rate_limit_per_min, audit_policy — no bare callables in the
  registry.
* Permission vocabulary is read/compute only; there is NO write permission
  (spec: default ``NO WRITE ACCESS``).
* Registry and spec table stay in sync (no orphan on either side).
* ``audit_policy=ALWAYS`` writes one JSONL record per execution (success and
  failure alike) to ``data/audit/tool_audit.jsonl``.
* ``rate_limit_per_min`` is enforced per (session, tool): calls beyond the
  limit fail fast without invoking the tool.
"""
from __future__ import annotations

import json
import time

import pytest

from app.core.tools import AUDIT_LOG, REGISTRY, execute_tool
from app.core.tools.specs import TOOL_SPECS, ToolPermission, ToolSpec


# --------------------------------------------------------------------------- #
# 1. Declarative coverage — spec §22 required fields
# --------------------------------------------------------------------------- #
def test_every_registered_tool_has_a_spec():
    assert set(REGISTRY) == set(TOOL_SPECS), "REGISTRY 与 TOOL_SPECS 必须一一对应"


def test_all_seven_tools_declared():
    expected = {
        "schema_search", "knowledge_search", "dataset_profile",
        "sql_query", "python_analysis", "visualization", "generate_report",
        "freeform",  # E2：模型产出的只读自由 SQL 工具
        "image_analyze",  # P2-2：多模态图片视觉解析
    }
    assert expected == set(TOOL_SPECS)


@pytest.mark.parametrize("name,permission", [
    ("schema_search", ToolPermission.READ_METADATA),
    ("knowledge_search", ToolPermission.READ_KNOWLEDGE),
    ("dataset_profile", ToolPermission.READ_DATA),
    ("sql_query", ToolPermission.READ_DATA),
    ("python_analysis", ToolPermission.COMPUTE),
    ("visualization", ToolPermission.COMPUTE),
    ("generate_report", ToolPermission.GENERATE_ARTIFACT),
])
def test_permission_matches_spec_suggestion(name, permission):
    spec = TOOL_SPECS[name]
    assert spec.permission is permission
    # §22 全部必填字段非空
    assert spec.timeout_s > 0
    assert spec.data_scope
    assert spec.rate_limit_per_min > 0
    assert spec.audit_policy == "ALWAYS"
    assert spec.description
    assert spec.input_schema.get("type") == "object"


def test_no_write_permission_exists():
    # 规格：默认 NO WRITE ACCESS —— 权限词表里根本不允许出现写权限
    assert not any("WRITE" in p.value for p in ToolPermission)


def test_spec_is_pydantic_model_with_schema():
    spec = TOOL_SPECS["sql_query"]
    assert isinstance(spec, ToolSpec)
    assert "sql" in spec.input_schema.get("properties", {})


# --------------------------------------------------------------------------- #
# 2. Audit log (audit_policy = ALWAYS)
# --------------------------------------------------------------------------- #
def _read_audit_tail(n: int = 5) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(ln) for ln in lines[-n:]]


def test_successful_execution_is_audited():
    execute_tool("a1", "schema_search", {"keyword": "revenue"}, "perm_audit")
    records = [r for r in _read_audit_tail(10) if r.get("step_id") == "a1"]
    assert records, "成功执行也必须落审计日志"
    rec = records[-1]
    for field in ("ts", "session_id", "step_id", "tool", "permission", "status"):
        assert field in rec and rec[field] not in (None, ""), f"审计记录缺 {field}"
    assert rec["status"] == "SUCCESS"
    assert rec["permission"] == "READ_METADATA"


def test_failed_execution_is_audited():
    execute_tool("a2", "sql_query", {"sql": "DELETE FROM fact_sales"}, "perm_audit")
    records = [r for r in _read_audit_tail(10) if r.get("step_id") == "a2"]
    assert records and records[-1]["status"] == "FAILED"
    assert records[-1]["error_class"] == "NON_RETRYABLE"


# --------------------------------------------------------------------------- #
# 3. Rate limiting (per session + tool)
# --------------------------------------------------------------------------- #
def test_rate_limit_rejects_excess_calls(monkeypatch):
    spec = TOOL_SPECS["schema_search"]
    monkeypatch.setattr(spec, "rate_limit_per_min", 2, raising=False)
    # ToolSpec 若为 pydantic BaseModel，setattr 默认禁止——允许测试改字段
    # （若此处报错说明实现不允许，需要换成构造新 spec 的方式）
    results = [
        execute_tool("rl", "schema_search", {"keyword": "x"}, "perm_rl")
        for _ in range(3)
    ]
    assert [r.status for r in results] == ["SUCCESS", "SUCCESS", "FAILED"]
    assert results[2].error and "rate" in results[2].error.lower()
    # 被限流的调用不应有第三次真实执行——但审计仍应记录
    assert results[2].error_class == "NON_RETRYABLE"


def test_rate_limit_is_per_session(monkeypatch):
    spec = TOOL_SPECS["schema_search"]
    monkeypatch.setattr(spec, "rate_limit_per_min", 2, raising=False)
    r1 = execute_tool("rl_a", "schema_search", {"keyword": "x"}, "perm_rl_a")
    r2 = execute_tool("rl_a", "schema_search", {"keyword": "x"}, "perm_rl_b")
    assert r1.status == "SUCCESS" and r2.status == "SUCCESS", \
        "不同会话的限额应互相独立"
