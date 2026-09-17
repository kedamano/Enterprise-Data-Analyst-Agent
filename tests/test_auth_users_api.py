"""AUTH/02 用户账号体系与微信扫码登录的 API 回归测试。

契约（这些断言就是"这套用户体系算不算做完了"的定义）：

**账号生命周期**
* 首个注册者成为管理员（否则新部署永远进不去）
* 用户名唯一（大小写不敏感）、弱密码被拒、用户名格式校验
* 登录成功/失败的状态码分离；失败文案**不区分**「用户不存在」与「密码错误」（防枚举）

**资料与头像**
* 资料字段校验（邮箱/手机/昵称长度/简介长度）
* 头像：上传 → 可访问、版本号破缓存、**路径穿越被挡**、非图片内容被拒
* 微信头像（外链 URL）与上传头像（本站路径）两种形态都要能渲染

**会话与安全**
* 改密后**踢掉其他会话、保留当前会话**（否则用户改完密立刻掉线）
* 登录流水记录失败尝试（自查异常登录的唯一入口）
* 登出**幂等**：重复登出/无令牌登出都必须返回 ok

**权限**
* 非管理员访问 `/auth/users` → 403
* 服务端硬约束：不能改自己的角色、不能停用/删除自己、不能移除最后一名管理员

**微信扫码**
* state 生成 → 轮询 pending → 模拟确认 → 拿到令牌
* **同一微信身份稳定映射到同一账号**（这是"扫码登录"能用的前提）
* 未开启 `WECHAT_DEV_SIMULATE` 时模拟入口必须关闭（不能变成后门）
* 已配置真实凭据时，模拟通道**自动关闭**（有真凭据就不该有假通道）
* 微信创建的账号没有密码，密码登录要给出可理解的提示而非"密码错误"

隔离要点：`users.py` / `wechat.py` 都是**模块级单例**（`_store` / `_login`），
且 `auth.py` 在 import 期就把 `get_store` 绑成函数对象——所以必须 patch
**单例本体**，patch 函数名无效（与 test_kb_multi_and_files_api.py 同源）。
"""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

# 4×4 的真 PNG（PIL 生成），用于头像上传
def _png_bytes(size: int = 64, color: tuple[int, int, int] = (90, 60, 200)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """临时用户库 + 临时头像目录 + 可用的微信模拟通道；返回 ``(client, store)``。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    monkeypatch.setenv("USER_REGISTRATION_OPEN", "true")
    monkeypatch.setenv("WECHAT_DEV_SIMULATE", "true")
    # 真实凭据必须清空，否则模拟通道会按设计自动关闭
    monkeypatch.setenv("WECHAT_APPID", "")
    monkeypatch.setenv("WECHAT_APPSECRET", "")

    from app.config import get_settings
    from app.core.security import users as users_core
    from app.core.security import wechat as wechat_core
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()

    store = users_core.UserStore(
        db_path=tmp_path / "users.db", avatar_dir=tmp_path / "avatars"
    )
    monkeypatch.setattr(users_core, "_store", store)
    monkeypatch.setattr(wechat_core, "_login", wechat_core.WeChatLogin())

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client, store

    users_core.reset_store()
    wechat_core.reset_login()
    get_settings.cache_clear()


def _register(client, username="alice", password="Passw0rd1", **extra):
    body = {"username": username, "password": password, **extra}
    return client.post("/api/v1/auth/register", json=body)


# --------------------------------------------------------------------- 能力探测

def test_config_reports_capabilities(env):
    client, _ = env
    cfg = client.get("/api/v1/auth/config").json()
    assert cfg["user_auth_enabled"] is True
    assert cfg["registration_open"] is True
    assert cfg["password_min_length"] >= 8
    assert cfg["has_users"] is False
    # 未配置凭据 + 开了模拟开关 → 模拟可用
    assert cfg["wechat"]["configured"] is False
    assert cfg["wechat"]["simulate_available"] is True
    assert set(cfg["wechat"]["missing"]) == {
        "WECHAT_APPID", "WECHAT_APPSECRET", "WECHAT_REDIRECT_URI",
    }


def test_roles_matrix_exposes_all_roles(env):
    client, _ = env
    roles = client.get("/api/v1/auth/roles").json()["roles"]
    assert [r["role"] for r in roles] == ["viewer", "analyst", "analyst_lead", "admin"]
    admin = next(r for r in roles if r["role"] == "admin")
    analyst = next(r for r in roles if r["role"] == "analyst")
    # 权限必须递增（管理员是超集），否则"划分"没有意义
    assert set(analyst["permissions"]) < set(admin["permissions"])
    assert admin["permission_labels"], "权限需要中文标签给前端直接渲染"


# --------------------------------------------------------------------- 注册

def test_first_user_becomes_admin(env):
    client, _ = env
    r = _register(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["user"]["role"] == "admin"
    assert body["user"]["status"] == "active"
    assert body["token"], "注册后应直接签发令牌（否则用户还要再登一次）"
    # 密码哈希绝不能出现在响应里
    assert "password_hash" not in r.text


def test_second_user_is_not_admin(env):
    client, _ = env
    _register(client, "alice", "Passw0rd1")
    r = _register(client, "bob", "Passw0rd2")
    assert r.json()["user"]["role"] != "admin", "只有首个账号才是管理员"


def test_register_rejects_duplicate_username_case_insensitive(env):
    client, _ = env
    _register(client, "alice", "Passw0rd1")
    r = _register(client, "ALICE", "Passw0rd2")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "username_taken"


@pytest.mark.parametrize(
    "username,password,code",
    [
        ("ab", "Passw0rd1", "invalid_username"),        # 太短
        ("has space", "Passw0rd1", "invalid_username"),  # 非法字符
        ("alice", "short", "weak_password"),             # 太短
        ("alice", "alllowercase", "weak_password"),      # 无数字
        ("alice", "12345678", "weak_password"),          # 无字母
    ],
)
def test_register_validates_input(env, username, password, code):
    client, _ = env
    r = _register(client, username, password)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == code


# --------------------------------------------------------------------- 登录

def test_login_and_wrong_password(env):
    client, _ = env
    _register(client)
    ok = client.post("/api/v1/auth/login",
                     json={"username": "alice", "password": "Passw0rd1"})
    assert ok.status_code == 200
    assert ok.json()["user"]["username"] == "alice"

    bad = client.post("/api/v1/auth/login",
                      json={"username": "alice", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["detail"]["code"] == "bad_credentials"


def test_login_does_not_leak_user_existence(env):
    """不存在的用户与错密码必须是**同一条**文案，否则可被用来枚举账号。"""
    client, _ = env
    _register(client)
    a = client.post("/api/v1/auth/login",
                    json={"username": "ghost", "password": "whatever1"})
    b = client.post("/api/v1/auth/login",
                    json={"username": "alice", "password": "whatever1"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"]["message"] == b.json()["detail"]["message"]


def test_me_anonymous_and_authenticated(env):
    client, _ = env
    anon = client.get("/api/v1/auth/me")
    assert anon.status_code == 200, "/me 不能因未登录就 401，前端要拿它判断登录态"
    assert anon.json()["authenticated"] is False
    assert anon.json()["user"] is None

    token = _register(client).json()["token"]
    me = client.get("/api/v1/auth/me", headers=_hdr(token)).json()
    assert me["authenticated"] is True
    assert me["user"]["username"] == "alice"


def test_disabled_account_cannot_login_and_token_is_revoked(env):
    client, store = env
    admin = _register(client).json()
    bob = _register(client, "bob", "Passw0rd2").json()

    store.set_status(bob["user"]["id"], "disabled", actor_id=admin["user"]["id"])

    assert client.post("/api/v1/auth/login",
                       json={"username": "bob", "password": "Passw0rd2"}).status_code == 403
    # 停用前签发的令牌必须立刻失效，不能等自然过期
    me = client.get("/api/v1/auth/me", headers=_hdr(bob["token"])).json()
    assert me["authenticated"] is False


# --------------------------------------------------------------------- 资料 / 头像

def test_update_profile_and_validation(env):
    client, _ = env
    token = _register(client).json()["token"]
    r = client.patch("/api/v1/auth/me", headers=_hdr(token),
                     json={"display_name": "Alice", "phone": "13800000000",
                           "email": "a@x.com", "bio": "数据负责人"})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"
    assert r.json()["phone"] == "13800000000"

    for bad in ({"email": "not-an-email"}, {"phone": "123"},
                {"display_name": "x" * 41}, {"bio": "y" * 201}):
        rr = client.patch("/api/v1/auth/me", headers=_hdr(token), json=bad)
        assert rr.status_code == 400, f"{bad} 应被拒绝"


def test_update_profile_requires_login(env):
    client, _ = env
    assert client.patch("/api/v1/auth/me", json={"display_name": "x"}).status_code == 401


def test_avatar_upload_serve_and_traversal_guard(env):
    client, _ = env
    token = _register(client).json()["token"]

    r = client.post("/api/v1/auth/me/avatar", headers=_hdr(token),
                    files={"file": ("me.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["avatar"]
    assert url.startswith("/api/v1/auth/avatar/")
    assert url.endswith("?v=1"), "头像地址必须带版本号，否则换头像后浏览器读缓存"

    # 头像路由是公开的（<img> 无法携带 Authorization 头）
    got = client.get(url.split("?")[0])
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/")

    # 删除
    after = client.delete("/api/v1/auth/me/avatar", headers=_hdr(token)).json()
    assert after["avatar"] == ""
    assert client.get(url.split("?")[0]).status_code == 404


def test_avatar_rejects_non_image_and_bad_extension(env):
    client, _ = env
    token = _register(client).json()["token"]

    r = client.post("/api/v1/auth/me/avatar", headers=_hdr(token),
                    files={"file": ("x.png", b"definitely not an image", "image/png")})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_avatar_image"

    r = client.post("/api/v1/auth/me/avatar", headers=_hdr(token),
                    files={"file": ("x.exe", _png_bytes(), "application/octet-stream")})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "bad_avatar_type"


def test_avatar_route_cannot_escape_directory(env):
    """头像按**文件名**取，且必须落在 avatar_dir 内——这里锁死路径穿越面。"""
    client, _ = env
    for name in ("..%2F..%2Fapp%2Fmain.py", "..", "....//settings.py"):
        r = client.get(f"/api/v1/auth/avatar/{name}")
        assert r.status_code == 404, f"{name} 不应可读"


# --------------------------------------------------------------------- 改密 / 会话

def test_change_password_revokes_other_sessions_but_keeps_current(env):
    client, _ = env
    token = _register(client).json()["token"]
    other = client.post("/api/v1/auth/login",
                        json={"username": "alice", "password": "Passw0rd1"}).json()["token"]

    r = client.post("/api/v1/auth/me/password", headers=_hdr(token),
                    json={"old_password": "Passw0rd1", "new_password": "NewPassw0rd1"})
    assert r.status_code == 200
    assert r.json()["revoked_sessions"] >= 1

    # 当前设备保持在线（否则用户改完密就被踢出去，体验灾难）
    assert client.get("/api/v1/auth/me", headers=_hdr(token)).json()["authenticated"] is True
    # 其他设备下线
    assert client.get("/api/v1/auth/me", headers=_hdr(other)).json()["authenticated"] is False
    # 旧密码失效、新密码可用
    assert client.post("/api/v1/auth/login",
                       json={"username": "alice", "password": "Passw0rd1"}).status_code == 401
    assert client.post("/api/v1/auth/login",
                       json={"username": "alice", "password": "NewPassw0rd1"}).status_code == 200


def test_change_password_validations(env):
    client, _ = env
    token = _register(client).json()["token"]
    h = _hdr(token)

    r = client.post("/api/v1/auth/me/password", headers=h,
                    json={"old_password": "wrong", "new_password": "NewPassw0rd1"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_old_password"

    r = client.post("/api/v1/auth/me/password", headers=h,
                    json={"old_password": "Passw0rd1", "new_password": "Passw0rd1"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "same_password"

    r = client.post("/api/v1/auth/me/password", headers=h,
                    json={"old_password": "Passw0rd1", "new_password": "weak"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "weak_password"


def test_sessions_and_login_events(env):
    client, _ = env
    token = _register(client).json()["token"]
    client.post("/api/v1/auth/login", json={"username": "alice", "password": "Passw0rd1"})
    client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"})

    s = client.get("/api/v1/auth/me/sessions", headers=_hdr(token)).json()["sessions"]
    assert len(s) >= 2
    assert {"created_at", "expires_at", "user_agent", "ip"} <= set(s[0])

    ev = client.get("/api/v1/auth/me/logins", headers=_hdr(token)).json()["events"]
    assert any(not e["ok"] for e in ev), "失败的登录尝试必须留痕"
    assert any(e["ok"] for e in ev)


def test_logout_is_idempotent(env):
    client, _ = env
    token = _register(client).json()["token"]
    assert client.post("/api/v1/auth/logout", headers=_hdr(token)).json()["revoked"] is True
    assert client.get("/api/v1/auth/me", headers=_hdr(token)).json()["authenticated"] is False
    # 重复登出 / 无令牌登出都不能报错
    assert client.post("/api/v1/auth/logout", headers=_hdr(token)).status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200


def test_lockout_event_is_persisted_and_attributed_to_the_victim(env):
    """账户被打到锁定，受害者必须能在自己的登录流水里看见——这是该功能存在的意义。

    这里锁的是两个真实踩过的坑：

    1. **回滚**：`locked` 事件以前写在 `with sqlite3.connect()` 块**内部**、紧跟着
       `raise`。`sqlite3.Connection.__exit__` 遇到异常会 rollback，于是这次 insert
       被静默丢弃——表里永远查不到锁定记录（其余分支的 raise 都在块外，所以没中招）。
    2. **归属**：以前记为 `user_id=""`，而 `/auth/me/logins` 是 `WHERE user_id=?`
       过滤的 —— 即使写进去，受害者也看不到。
    """
    client, _ = env
    token = _register(client).json()["token"]

    # 打到锁定（配置上限 8，多打几次确保越界）
    for _ in range(10):
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"})

    r = client.post("/api/v1/auth/login",
                    json={"username": "alice", "password": "Passw0rd1"})
    assert r.status_code == 429, "锁定期间即使口令正确也不该放行"
    assert r.json()["detail"]["code"] == "locked"

    ev = client.get("/api/v1/auth/me/logins",
                    headers=_hdr(token), params={"limit": 50}).json()["events"]
    reasons = [e["reason"] for e in ev]
    assert "locked" in reasons, "锁定事件必须落库（此前被 sqlite 事务回滚吃掉）"
    assert reasons.count("bad_password") >= 8, "锁定前的失败尝试也要留痕"
    # 归属正确：返回的都是自己的事件（接口按 user_id 过滤，故投影里不含该字段）
    assert all(e["username"].lower() == "alice" for e in ev)
    assert all("user_id" not in e for e in ev), "按用户过滤后不必回传 user_id"


def test_lockout_event_for_unknown_username_is_not_attributed(env):
    """用户名不存在时无法归属——不能凭空挂到别人账号上（否则可用来污染他人流水）。"""
    client, _ = env
    token = _register(client).json()["token"]

    # 先制造一批"别人的"失败（用户名不存在 + 打到它自己也被锁）
    for _ in range(10):
        client.post("/api/v1/auth/login", json={"username": "ghost", "password": "bad"})
    # 再来一次自己的失败，作为"filter 确实在按 user_id 工作"的对照
    client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"})

    ev = client.get("/api/v1/auth/me/logins",
                    headers=_hdr(token), params={"limit": 50}).json()["events"]

    assert all(e["username"].lower() != "ghost" for e in ev), \
        "别人的失败尝试不能出现在我的流水里 — 按 user_id 过滤必须生效"
    assert [e["username"].lower() for e in ev].count("alice") == 1, \
        "对照：自己的失败尝试必须能看到（否则就是过滤过严，功能形同虚设）"


def test_lockout_does_not_extend_itself_indefinitely(env):
    """锁定期内的尝试只留痕，**不累计**失败计数——否则攻击者能把账号永久锁死。

    "锁定"这一分支每触发一次就会写一条 ``locked`` 事件。若它也算失败，
    攻击者持续尝试的每一条都会把窗口往后推，受害者便**永远**登不进去
    （即使口令完全正确）——这就是经典的「锁定型 DoS」。
    不变量：窗口内被计入的失败数，恒等于真实凭据失败数（本用例为 8）。
    """
    client, store = env
    _register(client)

    for _ in range(10):                       # 8 次真实失败 + 触发锁定
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"})
    for _ in range(20):                       # 锁定期内继续打，制造一批 locked 事件
        client.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"})

    from app.core.security import users as users_core

    # 断言的就是这个内部口径，故直接调它（锁是模块级的，见 users.py 的 `with _lock, ...`）
    with users_core._lock, store._connect() as c:  # noqa: SLF001
        recent = store._recent_failures(c, "alice", 300)  # noqa: SLF001
        locked_rows = c.execute(
            "SELECT COUNT(*) FROM login_events WHERE username='alice' "
            "AND reason='locked'",
        ).fetchone()[0]

    assert locked_rows >= 1, "锁定事件本身仍要留痕（审计用）"
    assert recent == 8, (
        f"锁定窗口内的尝试被计入了失败数（{recent} > 8）——"
        "攻击者可持续尝试把账号无限期锁死"
    )


# --------------------------------------------------------------------- 权限

def test_only_admin_can_list_users(env):
    client, _ = env
    _register(client, "alice", "Passw0rd1")                 # admin
    bob = _register(client, "bob", "Passw0rd2").json()      # analyst

    assert client.get("/api/v1/auth/users").status_code == 401
    assert client.get("/api/v1/auth/users",
                      headers=_hdr(bob["token"])).status_code == 403

    ok = client.get("/api/v1/auth/users", headers=_hdr(
        client.post("/api/v1/auth/login",
                    json={"username": "alice", "password": "Passw0rd1"}).json()["token"]))
    assert ok.status_code == 200
    assert {u["username"] for u in ok.json()["users"]} == {"alice", "bob"}


def test_admin_can_change_role_and_disable(env):
    client, _ = env
    admin = _register(client, "alice", "Passw0rd1").json()
    bob = _register(client, "bob", "Passw0rd2").json()
    h = _hdr(admin["token"])

    r = client.patch(f"/api/v1/auth/users/{bob['user']['id']}", headers=h,
                     json={"role": "analyst_lead"})
    assert r.status_code == 200 and r.json()["role"] == "analyst_lead"

    r = client.patch(f"/api/v1/auth/users/{bob['user']['id']}", headers=h,
                     json={"status": "disabled"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"

    r = client.patch(f"/api/v1/auth/users/{bob['user']['id']}", headers=h, json={})
    assert r.status_code == 400, "空 patch 应报错而不是静默成功"


def test_admin_cannot_lock_itself_out(env):
    """把自己降权/停用/删除都必须在服务端被挡住——否则系统会被锁死。"""
    client, _ = env
    admin = _register(client, "alice", "Passw0rd1").json()
    aid, h = admin["user"]["id"], _hdr(admin["token"])

    r = client.patch(f"/api/v1/auth/users/{aid}", headers=h, json={"role": "viewer"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "self_role_change"

    r = client.patch(f"/api/v1/auth/users/{aid}", headers=h, json={"status": "disabled"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "self_disable"

    r = client.delete(f"/api/v1/auth/users/{aid}", headers=h)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "self_delete"


def test_last_admin_cannot_be_demoted_by_another_admin(env):
    client, store = env
    a1 = _register(client, "alice", "Passw0rd1").json()
    a2 = _register(client, "bob", "Passw0rd2").json()
    store.set_role(a2["user"]["id"], "admin", actor_id=a1["user"]["id"])

    # 现在是两名管理员：a1 可以被降级
    r = client.patch(f"/api/v1/auth/users/{a1['user']['id']}",
                     headers=_hdr(a2["token"]), json={"role": "analyst"})
    assert r.status_code == 200
    # 只剩 a2 一名管理员：再降级必须被拒
    r = client.patch(f"/api/v1/auth/users/{a2['user']['id']}",
                     headers=_hdr(a2["token"]), json={"role": "analyst"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] in {"self_role_change", "last_admin"}


def test_unknown_role_and_status_rejected(env):
    client, _ = env
    admin = _register(client, "alice", "Passw0rd1").json()
    bob = _register(client, "bob", "Passw0rd2").json()
    h, bid = _hdr(admin["token"]), bob["user"]["id"]
    assert client.patch(f"/api/v1/auth/users/{bid}", headers=h,
                        json={"role": "superuser"}).status_code == 400
    assert client.patch(f"/api/v1/auth/users/{bid}", headers=h,
                        json={"status": "banana"}).status_code == 400


# --------------------------------------------------------------------- 微信扫码

def test_wechat_qr_flow_and_account_stability(env):
    """扫码 → 轮询 → 拿令牌 → 同一身份复用账号。

    ⚠️ 先注册一个口令账号再走微信：否则微信用户会成为**首个账号**，按
    ``USER_FIRST_REGISTRANT_IS_ADMIN`` 继承管理员，断言角色就失去意义了。
    这里要锁的是"扫码进来自动建号，但**不会被提权**"。
    """
    client, _ = env
    admin = _register(client, username="boss", password="Passw0rd9").json()
    assert admin["user"]["role"] == "admin", "先建的管理员应拿到最高角色，后续断言才有基准"

    q = client.get("/api/v1/auth/wechat/qrcode").json()
    assert q["configured"] is False and q["simulated"] is True
    state = q["state"]
    assert state

    assert client.get("/api/v1/auth/wechat/poll",
                      params={"state": state}).json()["status"] == "pending"

    r = client.post("/api/v1/auth/wechat/simulate", json={"state": state, "nickname": "张三"})
    assert r.status_code == 200
    first = r.json()
    assert first["status"] == "confirmed" and first["token"]

    # 轮询也要能看到 confirmed（真实前端就是靠轮询拿 token）
    polled = client.get("/api/v1/auth/wechat/poll", params={"state": state}).json()
    assert polled["status"] == "confirmed" and polled["token"] == first["token"]

    me = client.get("/api/v1/auth/me", headers=_hdr(first["token"])).json()["user"]
    assert me["source"] == "wechat"
    assert me["role"] == "analyst", "微信新用户不能因扫码拿到管理员"
    assert me["id"] != admin["user"]["id"], "微信账号必须是独立账号，不能并到口令账号上"

    # 同一微信身份再次扫码 → 必须是同一个账号（否则每次扫码都在建新号）
    s2 = client.get("/api/v1/auth/wechat/qrcode").json()["state"]
    second = client.post("/api/v1/auth/wechat/simulate",
                         json={"state": s2, "nickname": "张三"}).json()
    assert second["user_id"] == first["user_id"]
    # 复用不该把它提权，也不该建新号
    me2 = client.get("/api/v1/auth/me", headers=_hdr(second["token"])).json()["user"]
    assert me2["role"] == "analyst" and me2["id"] == me["id"]


def test_wechat_account_has_no_password_login(env):
    """微信账号没有密码：用密码登录它时必须给出可理解的提示，而不是"密码错误"。"""
    client, _ = env
    st = client.get("/api/v1/auth/wechat/qrcode").json()["state"]
    tok = client.post("/api/v1/auth/wechat/simulate",
                      json={"state": st, "nickname": "张三"}).json()["token"]
    uname = client.get("/api/v1/auth/me", headers=_hdr(tok)).json()["user"]["username"]

    r = client.post("/api/v1/auth/login", json={"username": uname, "password": "whatever1"})
    assert r.status_code == 400
    code = r.json()["detail"]["code"]
    # 关键：不能笼统报"用户名或密码错误"，否则用户永远不知道该改用微信扫码
    assert code == "wechat_only", code
    assert "微信" in r.json()["detail"]["message"]

    # 缺 state 的 simulate 请求应被拒（参数校验生效）
    assert client.post("/api/v1/auth/wechat/simulate", json={}).status_code == 422


def test_wechat_poll_rejects_unknown_state(env):
    client, _ = env
    r = client.get("/api/v1/auth/wechat/poll", params={"state": "bogus-state-xyz"})
    assert r.status_code == 200
    assert r.json()["status"] in {"expired", "error"}


def test_wechat_simulate_is_closed_when_not_enabled(monkeypatch, tmp_path):
    """`WECHAT_DEV_SIMULATE` 关闭时，模拟入口必须不可用——它不能变成后门。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    monkeypatch.setenv("WECHAT_DEV_SIMULATE", "false")
    monkeypatch.setenv("WECHAT_APPID", "")
    monkeypatch.setenv("WECHAT_APPSECRET", "")

    from app.config import get_settings
    from app.core.security import users as users_core
    from app.core.security import wechat as wechat_core
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()
    monkeypatch.setattr(users_core, "_store",
                        users_core.UserStore(tmp_path / "u.db", tmp_path / "av"))
    monkeypatch.setattr(wechat_core, "_login", wechat_core.WeChatLogin())

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        cfg = client.get("/api/v1/auth/config").json()
        assert cfg["wechat"]["simulate_available"] is False
        st = client.get("/api/v1/auth/wechat/qrcode").json()["state"]
        r = client.post("/api/v1/auth/wechat/simulate", json={"state": st, "nickname": "x"})
        # 503 = 通道不可用（后端用"服务不可用"语义拒绝，而非"参数错误"）
        assert r.status_code in (400, 403, 404, 503), r.text
        # 失败后会话不应变成 confirmed
        assert client.get("/api/v1/auth/wechat/poll",
                          params={"state": st}).json()["status"] == "pending"

    users_core.reset_store()
    wechat_core.reset_login()
    get_settings.cache_clear()


def test_wechat_simulate_is_closed_when_real_credentials_present(monkeypatch, tmp_path):
    """有真凭据时不能还有假通道——否则等于留了一个免授权的登录后门。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    monkeypatch.setenv("WECHAT_DEV_SIMULATE", "true")   # 即使显式打开
    monkeypatch.setenv("WECHAT_APPID", "wx123")
    monkeypatch.setenv("WECHAT_APPSECRET", "sec")
    monkeypatch.setenv("WECHAT_REDIRECT_URI", "https://example.com/cb")

    from app.config import get_settings
    from app.core.security import users as users_core
    from app.core.security import wechat as wechat_core
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()
    monkeypatch.setattr(users_core, "_store",
                        users_core.UserStore(tmp_path / "u.db", tmp_path / "av"))
    monkeypatch.setattr(wechat_core, "_login", wechat_core.WeChatLogin())

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        q = client.get("/api/v1/auth/wechat/qrcode").json()
        assert q["configured"] is True
        assert q["simulated"] is False
        # 真实二维码地址应带 appid 与 state
        assert "appid=wx123" in q["qr_url"] and q["state"] in q["qr_url"]
        r = client.post("/api/v1/auth/wechat/simulate", json={"state": q["state"]})
        assert r.status_code in (400, 403)

    users_core.reset_store()
    wechat_core.reset_login()
    get_settings.cache_clear()


# --------------------------------------------------------------------- 中间件

def test_bearer_token_is_accepted_by_protected_api(monkeypatch, tmp_path):
    """`Authorization: Bearer` 与 `X-API-Key` 必须都能过中间件，且解析成同一身份。

    这是"用户体系接进来但业务接口一行都不用改"的关键契约。
    """
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", '[{"key":"k1","user_id":"machine","roles":["analyst"]}]')

    from app.config import get_settings
    from app.core.security import users as users_core
    from app.core.security import wechat as wechat_core
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()
    monkeypatch.setattr(users_core, "_store",
                        users_core.UserStore(tmp_path / "u.db", tmp_path / "av"))
    monkeypatch.setattr(wechat_core, "_login", wechat_core.WeChatLogin())

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        # 公开路径不需要凭据
        assert client.get("/api/v1/health").status_code == 200

        # 受保护路径：匿名 401，两种凭据都放行
        assert client.get("/api/v1/knowledge-bases").status_code == 401
        assert client.get("/api/v1/knowledge-bases",
                          headers={"X-API-Key": "k1"}).status_code == 200

        token = client.post("/api/v1/auth/register",
                            json={"username": "alice",
                                  "password": "Passw0rd1"}).json()["token"]
        assert client.get("/api/v1/knowledge-bases",
                          headers=_hdr(token)).status_code == 200

        # 伪造/过期令牌 → 401（不能因为"带了头"就放行）
        assert client.get("/api/v1/knowledge-bases",
                          headers=_hdr("forged.token.value")).status_code == 401

    users_core.reset_store()
    wechat_core.reset_login()
    get_settings.cache_clear()
