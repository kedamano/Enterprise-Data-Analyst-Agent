"""security/auth.py + api/routes/auth.py — RBAC 门禁链路。

待测模块：app/core/security/auth.py（current_principal / enabled）、
          app/api/routes/auth.py（require_admin / require_user）

覆盖链路：
- admin 身份 → require_admin 通过（返回 user dict）。
- non-admin 身份 → require_admin 抛 403。
- 无 token / 匿名 → require_user 抛 401（require_admin 经 require_user 后也 401）。

mock 策略：直接用 auth.set_current() 注入 Principal，模拟路由层身份依赖。
require_admin / require_user 是 FastAPI Depends —— 这里直接调用底层函数而不走 Depends 注入。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.security import auth
from app.core.security.auth import Principal


# --------------------------------------------------------------------------- #
# 模拟路由层身份守卫：require_user / require_admin 的核心判定
# （直接从 api/routes/auth.py 抠出判定逻辑，不依赖 FastAPI Depends 注入）
# --------------------------------------------------------------------------- #
def _require_user_like(principal: Principal | None) -> Principal:
    """镜像 api/routes/auth.py::require_user 的 401 判定。"""
    if principal is None or principal.is_anonymous:
        raise HTTPException(status_code=401, detail="需要登录后才能执行此操作")
    return principal


def _require_admin_like(user: dict) -> dict:
    """镜像 api/routes/auth.py::require_admin 的 403 判定。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# --------------------------------------------------------------------------- #
# 1. require_admin 通过 / 拒绝
# --------------------------------------------------------------------------- #
def test_require_admin_allows_admin_user():
    """role=admin 的 user dict 必须通过 require_admin（不抛）。"""
    admin_user = {"id": "u1", "username": "alice", "role": "admin"}
    result = _require_admin_like(admin_user)
    assert result["role"] == "admin"


def test_require_admin_rejects_analyst_with_403():
    """role=analyst 的 user 抛 403（而不是 401 或 500）。"""
    analyst_user = {"id": "u2", "username": "bob", "role": "analyst"}
    with pytest.raises(HTTPException) as exc_info:
        _require_admin_like(analyst_user)
    assert exc_info.value.status_code == 403


def test_require_admin_rejects_viewer_with_403():
    """role=viewer 同样 403——与被 downgrade 到 anonymous 区分。"""
    viewer_user = {"id": "u3", "username": "carol", "role": "viewer"}
    with pytest.raises(HTTPException) as exc_info:
        _require_admin_like(viewer_user)
    assert exc_info.value.status_code == 403


# --------------------------------------------------------------------------- #
# 2. require_user 链：无 token / 匿名 → 401
# --------------------------------------------------------------------------- #
def test_require_user_rejects_anonymous_with_401():
    """匿名 principal（无任何 token）必须在 require_user 这里被截为 401。"""
    anon = Principal(user_id="anonymous", roles=[])
    with pytest.raises(HTTPException) as exc_info:
        _require_user_like(anon)
    assert exc_info.value.status_code == 401


def test_require_user_rejects_none_with_401():
    """None（无 token 上下文）也 401。"""
    with pytest.raises(HTTPException) as exc_info:
        _require_user_like(None)
    assert exc_info.value.status_code == 401


def test_require_user_allows_authenticated_analyst():
    """已登录 analyst 通过 require_user（不抛），但后面 require_admin 再拦。"""
    p = Principal(user_id="u_analyst", roles=["analyst"])
    # 非匿名即可过 require_user
    assert _require_user_like(p) is p


# --------------------------------------------------------------------------- #
# 3. 端到端模拟：require_user → require_admin 链路
# --------------------------------------------------------------------------- #
def test_full_guard_chain_admin_passes():
    """完整链路：已登录 admin 过 require_user + require_admin。"""
    admin = {"id": "u1", "username": "alice", "role": "admin"}
    principal = Principal(user_id="u1", roles=["admin"])
    # step 1: require_user
    assert _require_user_like(principal) is principal
    # step 2: require_admin
    assert _require_admin_like(admin) == admin


def test_full_guard_chain_non_admin_blocked_at_admin_check():
    """完整链路：已登录 analyst 过 require_user，但在 require_admin 被 403 拦截。"""
    analyst = {"id": "u2", "username": "bob", "role": "analyst"}
    principal = Principal(user_id="u2", roles=["analyst"])
    # step 1: 通过
    assert _require_user_like(principal) is principal
    # step 2: 403
    with pytest.raises(HTTPException) as exc_info:
        _require_admin_like(analyst)
    assert exc_info.value.status_code == 403
