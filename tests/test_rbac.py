"""RBAC 门禁测试（AUTH/01 §6）。

验证：角色 → ToolPermission 的展开正确；`execute_tool` 在鉴权开启时按角色拦截
无权工具；鉴权关闭时保持既有全开（不影响 600+ 既有用例）。

铁律：不依赖真实 LLM / 真实数据库 —— 权限不足在工具执行前就被拦下，失败路径确定。
"""
from __future__ import annotations

import os

import pytest

from app.core.security import auth
from app.core.tools import execute_tool
from app.core.tools.specs import ROLE_PERMISSIONS, ToolPermission, roles_to_permissions


# --------------------------------------------------------------------------- #
# 1) 角色 → 权限映射（纯函数，最高价值、最易回归）
# --------------------------------------------------------------------------- #
def test_roles_to_permissions_viewer():
    perms = roles_to_permissions(["viewer"])
    assert ToolPermission.READ_METADATA in perms
    assert ToolPermission.READ_KNOWLEDGE in perms
    # viewer 不能碰业务数据 / 计算 / 出报告
    assert ToolPermission.READ_DATA not in perms
    assert ToolPermission.COMPUTE not in perms
    assert ToolPermission.GENERATE_ARTIFACT not in perms


def test_roles_to_permissions_analyst_vs_lead():
    analyst = roles_to_permissions(["analyst"])
    assert ToolPermission.READ_DATA in analyst
    assert ToolPermission.COMPUTE in analyst
    assert ToolPermission.GENERATE_ARTIFACT not in analyst  # 分析师不能出报告

    lead = roles_to_permissions(["analyst_lead"])
    assert ToolPermission.GENERATE_ARTIFACT in lead


def test_roles_to_permissions_admin_is_full():
    admin = roles_to_permissions(["admin"])
    assert admin == set(ToolPermission)


def test_unknown_role_ignored_safe_default():
    # 配置写错角色名不能意外放大权限（最小权限安全阀）
    assert roles_to_permissions(["nosuchrole"]) == set()
    # 多角色取并集
    assert roles_to_permissions(["viewer", "analyst"]) == roles_to_permissions(["analyst"])


def test_effective_permissions_string_form():
    p = auth.Principal(user_id="u1", roles=["analyst"])
    eff = auth.effective_permissions(p)
    assert "READ_DATA" in eff
    assert "GENERATE_ARTIFACT" not in eff


# --------------------------------------------------------------------------- #
# 2) 工具级门禁（execute_tool 执行前拦截）
# --------------------------------------------------------------------------- #
@pytest.fixture
def auth_on():
    os.environ["AUTH_ENABLED"] = "true"
    auth.get_settings.cache_clear()
    yield
    os.environ.pop("AUTH_ENABLED", None)
    auth.get_settings.cache_clear()


def test_gate_off_allows_everything_when_auth_disabled():
    """鉴权关闭时，即便角色为空也不拦截（既有行为，保证 600+ 用例绿）。"""
    os.environ.pop("AUTH_ENABLED", None)
    auth.get_settings.cache_clear()
    token = auth.set_current(auth.Principal(user_id="anon", roles=[]))
    try:
        # visualization 需要 COMPUTE；鉴权关 → 不会被权限拦（会走真实工具逻辑而失败，但非权限拒绝）
        res = execute_tool("bench", "visualization", {"dataset_ref": "x", "chart_type": "bar"}, "s1")
        assert "permission denied" not in (res.error or "").lower()
    finally:
        auth.reset_current(token)
        auth.get_settings.cache_clear()


def test_gate_denies_viewer_calling_compute_tool(auth_on):
    """viewer 缺 COMPUTE，调用 visualization 应在执行前被拒。"""
    token = auth.set_current(auth.Principal(user_id="viewer1", roles=["viewer"]))
    try:
        res = execute_tool("bench", "visualization", {"dataset_ref": "x", "chart_type": "bar"}, "s2")
        assert res.status == "FAILED"
        assert "permission denied" in (res.error or "").lower()
        assert res.error_class == "NON_RETRYABLE"  # 确定性失败，不重试
    finally:
        auth.reset_current(token)


def test_gate_allows_analyst_metadata_tool(auth_on):
    """analyst 拥有 READ_METADATA，调用 schema_search 应通过 RBAC 门禁（不被权限拒绝）。"""
    token = auth.set_current(auth.Principal(user_id="analyst1", roles=["analyst"]))
    try:
        res = execute_tool("bench", "schema_search", {"query": "region"}, "s3")
        # 不应该是权限拒绝（后面的结果由工具逻辑决定，与 RBAC 无关）
        assert "permission denied" not in (res.error or "").lower()
    finally:
        auth.reset_current(token)


def test_gate_denies_low_role_report_generation(auth_on):
    """analyst 不能生成报告（GENERATE_ARTIFACT 仅 analyst_lead/admin）。"""
    token = auth.set_current(auth.Principal(user_id="analyst2", roles=["analyst"]))
    try:
        res = execute_tool(
            "bench", "generate_report",
            {"analysis_result": {"ok": True, "findings": []}, "format": "markdown"}, "s4",
        )
        assert res.status == "FAILED"
        assert "permission denied" in (res.error or "").lower()
    finally:
        auth.reset_current(token)


def test_gate_allows_lead_report_generation(auth_on):
    """analyst_lead 可生成报告（COMPUTE+GENERATE_ARTIFACT 齐备）。"""
    token = auth.set_current(auth.Principal(user_id="lead1", roles=["analyst_lead"]))
    try:
        res = execute_tool(
            "bench", "generate_report",
            {"analysis_result": {"ok": True, "findings": []}, "format": "markdown"}, "s5",
        )
        assert "permission denied" not in (res.error or "").lower()
    finally:
        auth.reset_current(token)


def test_role_inventory_covers_all_tools():
    """每个已注册工具所需权限都能被至少一个内置角色授予（否则该工具永远不可达）。"""
    granted_by_some_role = set().union(*ROLE_PERMISSIONS.values())
    from app.core.tools.specs import TOOL_SPECS

    for name, spec in TOOL_SPECS.items():
        assert spec.permission in granted_by_some_role, f"工具 {name} 的权限 {spec.permission} 没有任何角色能授予"
