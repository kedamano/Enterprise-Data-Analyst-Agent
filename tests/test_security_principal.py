"""security/auth.py — Principal 模型的 RBAC 展开与配额链路。

待测模块：app/core/security/auth.py（Principal.effective_permissions / check_quota / validate_filter_fragment）

覆盖链路：
- Principal 经 roles 展开为 ToolPermission 集合；admin = 全集、viewer = 只读。
- 未知 role 被安全地忽略（不爆炸、不放大）。
- check_quota 滑动窗口生效；匿名 / quota<=0 永不限。
- validate_filter_fragment 拒绝 SQL 注入片段。

mock 策略：Principal 是纯 Pydantic 模型，权限展开走 roles_to_permissions（纯函数），
无需 mock。quota 用 freezegun-style 的假时间注入（可选）。
"""
from __future__ import annotations

import pytest

from app.core.security import auth
from app.core.security.auth import Principal, check_quota, validate_filter_fragment
from app.core.tools.specs import ToolPermission, roles_to_permissions


# --------------------------------------------------------------------------- #
# 1. Principal → permissions 展开
# --------------------------------------------------------------------------- #
def test_admin_principal_expands_all_permissions():
    """admin 角色必须展开为 ToolPermission 全集（否则管理员连 schema 都不能查）。"""
    p = Principal(user_id="admin1", roles=["admin"])
    eff = auth.effective_permissions(p)
    assert eff == {tp.value for tp in ToolPermission}


def test_viewer_principal_read_only():
    """viewer 只能读元数据+知识库，不能碰业务数据、计算、产物。"""
    p = Principal(user_id="viewer1", roles=["viewer"])
    eff = auth.effective_permissions(p)
    assert "READ_METADATA" in eff
    assert "READ_KNOWLEDGE" in eff
    assert "READ_DATA" not in eff
    assert "COMPUTE" not in eff
    assert "GENERATE_ARTIFACT" not in eff


def test_unknown_role_ignored_no_blowup():
    """配置写错角色名必须被安全地忽略——不放大、不抛、不静默开全权限。"""
    # 纯函数：roles_to_permissions
    assert roles_to_permissions(["ghost_role"]) == set()
    # Principal 构造含未知角色也不爆炸
    p = Principal(user_id="u", roles=["ghost_role"])
    assert auth.effective_permissions(p) == set()


def test_anonymous_principal_has_no_special_permissions():
    """匿名 principal（默认 / 无上下文）不应自动获得业务数据权限。"""
    p = auth.anonymous_principal()
    assert p.is_anonymous
    assert auth.effective_permissions(p) == set()


def test_multi_role_union():
    """多角色取并集：viewer + analyst == analyst。"""
    union = roles_to_permissions(["viewer", "analyst"])
    assert union == roles_to_permissions(["analyst"])


# --------------------------------------------------------------------------- #
# 2. Quota 滑动窗口
# --------------------------------------------------------------------------- #
def test_check_quota_unlimited_when_zero_or_anonymous():
    """quota_per_min <= 0 表示不限；匿名同样不限。"""
    p_anon = Principal(user_id="anonymous", quota_per_min=0)
    for _ in range(100):
        assert check_quota(p_anon) is True


def test_check_quota_sliding_window_blocks_after_limit(monkeypatch):
    """quota_per_min=3：前 3 次通过、第 4 次阻断。"""
    fake_t = [0.0]

    def _fake_time():
        return fake_t[0]

    monkeypatch.setattr("app.core.security.auth.time.time", _fake_time)
    auth.reset_quota()
    p = Principal(user_id="q_user", quota_per_min=3)

    assert check_quota(p) is True   # 1
    assert check_quota(p) is True   # 2
    assert check_quota(p) is True   # 3
    assert check_quota(p) is False  # 超配额


def test_check_quota_window_resets_after_60s(monkeypatch):
    """滑动窗口：60.1s 后再调用，旧记录被驱逐，应再次通过。"""
    fake_t = [0.0]

    def _fake_time():
        return fake_t[0]

    monkeypatch.setattr("app.core.security.auth.time.time", _fake_time)
    auth.reset_quota()
    p = Principal(user_id="q_user2", quota_per_min=2)

    assert check_quota(p) is True
    assert check_quota(p) is True
    assert check_quota(p) is False  # 超

    # 前进 61 秒：滑动窗口清空
    fake_t[0] = 61.0
    assert check_quota(p) is True


# --------------------------------------------------------------------------- #
# 3. row_filters SQL 注入防护
# --------------------------------------------------------------------------- #
def test_validate_filter_fragment_rejects_injection():
    """过滤片段必须把多语句/注释/子查询一律拒掉——不能把 SQL 注入写进配置。"""
    assert validate_filter_fragment("1; DROP TABLE users") is not None
    assert validate_filter_fragment("region_id=1 -- hack") is not None
    assert validate_filter_fragment("union select * from secrets") is not None


def test_validate_filter_fragment_accepts_simple_predicate():
    """合法谓词（IN / 比较）必须放行。"""
    assert validate_filter_fragment("region_id IN (1, 2)") is None
    assert validate_filter_fragment("tenant = 'acme'") is None
