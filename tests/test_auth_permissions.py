"""AUTH/01 用户级鉴权与数据权限。

Spec: docs/specs/AUTH/01-user-auth-and-data-permissions.md
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.security import auth as auth_mod
from app.core.security.auth import Principal, parse_keys, validate_filter_fragment


@pytest.fixture
def _offline(monkeypatch, tmp_path):
    """本组用例必须离线：.env 现在是真实模型配置，不能让它打到真端点。"""
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))


@pytest.fixture
def auth_off(monkeypatch, _offline):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    auth_mod.reset_quota()
    yield
    get_settings.cache_clear()


def _keys_env(monkeypatch, **overrides):
    entry = {"key": "k-alice", "user_id": "alice", "tenant": "acme", **overrides}
    monkeypatch.setenv("AUTH_KEYS", json.dumps([entry]))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    get_settings.cache_clear()


@pytest.fixture
def auth_on(monkeypatch, _offline):
    _keys_env(monkeypatch)
    auth_mod.reset_quota()
    yield
    get_settings.cache_clear()
    auth_mod.reset_quota()


ALICE = {"X-API-Key": "k-alice"}
PAYLOAD = {"query": "分析各区域营收", "session_id": "auth_s1"}


# --------------------------------------------------------------------------- #
# 1. 身份解析与配置校验（纯函数）
# --------------------------------------------------------------------------- #
def test_principal_parsing_and_defaults():
    keys = parse_keys(json.dumps([{"key": "k1", "user_id": "u1", "tenant": "t1"}]))
    assert keys[0][0] == "k1" and keys[0][1].user_id == "u1"
    assert parse_keys("") == []
    with pytest.raises(ValueError):
        parse_keys("{not json")
    with pytest.raises(ValueError):
        parse_keys(json.dumps([{"user_id": "no-key"}]))  # 必须含 key


def test_row_filter_fragment_is_validated():
    """过滤片段由配置方提供，但**仍要过白名单**——否则等于把注入写进配置。"""
    assert validate_filter_fragment("region_id IN (1,2)") is None
    assert validate_filter_fragment("") is None
    assert validate_filter_fragment("1=1; DROP TABLE fact_sales") is not None
    assert validate_filter_fragment("1=1 -- 注释") is not None
    assert validate_filter_fragment("id IN (SELECT id FROM x)") is not None
    assert validate_filter_fragment("a IN (1,2") is not None

    with pytest.raises(ValueError):
        Principal(row_filters={"fact_sales": "1=1; DROP TABLE t"})


def test_bad_filter_config_fails_loudly(monkeypatch):
    """配置写坏必须在**读取时**就报，而不是运行到一半才发现。"""
    monkeypatch.setenv("AUTH_KEYS", json.dumps(
        [{"key": "k", "user_id": "u", "row_filters": {"fact_sales": "x; drop"}}]))
    get_settings.cache_clear()
    with pytest.raises(Exception):
        from app.core.security.auth import resolve_key

        resolve_key("k")


# --------------------------------------------------------------------------- #
# 2. 兼容与拒绝路径
# --------------------------------------------------------------------------- #
def test_auth_disabled_keeps_anonymous_access(auth_off):
    """**默认关**：既有行为完全不变（无 key 也能分析）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).post("/api/v1/chat/analyze", json=PAYLOAD).json()
    assert body["status"] in ("FINISH", "CLARIFY"), body


def test_missing_or_wrong_key_is_401(auth_on):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    assert c.post("/api/v1/chat/analyze", json=PAYLOAD).status_code == 401
    r = c.post("/api/v1/chat/analyze", json=PAYLOAD, headers={"X-API-Key": "nope"})
    assert r.status_code == 401
    assert "exists" not in r.text.lower(), "不得回显 key 是否存在（防枚举）"


def test_health_stays_public(auth_on):
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/api/v1/health").status_code == 200


def test_valid_key_is_allowed_and_audited(auth_on, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_AUDIT_LOG_PATH", str(tmp_path / "auth.jsonl"))
    monkeypatch.setattr(auth_mod, "AUTH_AUDIT_LOG", tmp_path / "auth.jsonl")

    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).post("/api/v1/chat/analyze", json=PAYLOAD, headers=ALICE)
    assert r.status_code == 200, r.text[:200]

    lines = [json.loads(x) for x in (tmp_path / "auth.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert any(x["decision"] == "ALLOW" and x["user_id"] == "alice" for x in lines), lines


def test_deny_is_also_audited(auth_on, tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_AUDIT_LOG", tmp_path / "auth.jsonl")

    from fastapi.testclient import TestClient

    from app.main import app

    TestClient(app).post("/api/v1/chat/analyze", json=PAYLOAD)   # 无 key → 401
    lines = [json.loads(x) for x in (tmp_path / "auth.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert any(x["decision"] == "DENY" for x in lines), "拒绝也必须留痕（只记放行无法复盘）"


def test_enabled_without_any_identity_source_is_503(monkeypatch):
    """开了鉴权、却**一个身份来源都没有**：要吵，不能静默放开。

    注意判据是「无身份来源」而不是「无 AUTH_KEYS」——账号体系（AUTH/02）同样是
    合法来源，只配账号、不配静态 key 是正常部署，那种情况下 503 会把服务整个打死。
    所以这里把账号体系也关掉，构造真正的零来源状态。
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", "")
    monkeypatch.setenv("USER_AUTH_ENABLED", "false")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).post("/api/v1/chat/analyze", json=PAYLOAD).status_code == 503
    get_settings.cache_clear()


def test_enabled_without_keys_but_with_accounts_is_401(monkeypatch):
    """只开鉴权 + 账号体系、不配静态 key：匿名应被要求**登录**（401），而不是 503。

    503 表达的是"服务端配置坏了"，此时配置其实完全合法，用户去 /auth/login 就能进来；
    回 503 会让"只用账号登录、不用 API key"的部署彻底不可用。
    但安全意图不变：匿名依然拿不到数据，且登录入口必须可达。
    """
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", "")
    monkeypatch.setenv("USER_AUTH_ENABLED", "true")
    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.post("/api/v1/chat/analyze", json=PAYLOAD)
    assert r.status_code == 401, r.text[:200]
    assert "登录" in r.json()["detail"], "提示要告诉用户出路是登录，而不是只报错"
    # 零身份来源时 503 的那种"服务不可用"绝不能出现
    assert r.status_code != 503
    # 登录入口必须仍然可达，否则用户被彻底锁在门外
    assert c.get("/api/v1/auth/config").status_code == 200
    get_settings.cache_clear()


def test_quota_exceeded_is_429(monkeypatch, _offline):
    _keys_env(monkeypatch, quota_per_min=2)
    auth_mod.reset_quota()

    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    codes = [c.post("/api/v1/chat/analyze", json=PAYLOAD, headers=ALICE).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429, codes
    auth_mod.reset_quota()
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 3. 数据权限：表级 / 列级 / 行级
# --------------------------------------------------------------------------- #
def test_guard_sql_table_whitelist():
    from app.core.security.data_guard import guard_sql

    p = Principal(user_id="u", allowed_tables=["fact_sales"])
    assert guard_sql("SELECT revenue FROM fact_sales", p) is None
    err = guard_sql("SELECT * FROM dim_region", p)
    assert err and "dim_region" in err
    err2 = guard_sql("SELECT f.revenue FROM fact_sales f JOIN dim_region r ON 1=1", p)
    assert err2 and "dim_region" in err2, "JOIN 的表也要过白名单"


def test_guard_sql_denied_column_rejected():
    from app.core.security.data_guard import guard_sql

    p = Principal(user_id="u", denied_columns=["customers"])
    assert guard_sql("SELECT revenue FROM fact_sales", p) is None
    err = guard_sql('SELECT "customers" FROM fact_sales', p)
    assert err and "customers" in err, "引用禁列必须拒绝，而不是静默返回空"


def test_row_filters_are_injected():
    from app.core.security.data_guard import apply_row_filters

    p = Principal(user_id="u", row_filters={"fact_sales": "region_id IN (1,2)"})
    sql, applied = apply_row_filters("SELECT region_id, revenue FROM fact_sales", p)
    assert applied == ["fact_sales"]
    assert "WHERE (region_id IN (1,2))" in sql

    # 已有 WHERE → AND
    sql2, _ = apply_row_filters("SELECT * FROM fact_sales WHERE revenue > 0", p)
    assert sql2.count("WHERE") == 1 and "AND (region_id IN (1,2))" in sql2

    # 谓词必须插在 GROUP BY 之前（不能拼到末尾）
    sql3, _ = apply_row_filters("SELECT region_id, SUM(revenue) FROM fact_sales GROUP BY region_id", p)
    assert sql3.index("WHERE") < sql3.index("GROUP BY"), sql3

    # 无关表不加
    sql4, applied4 = apply_row_filters("SELECT * FROM dim_region", p)
    assert applied4 == [] and sql4 == "SELECT * FROM dim_region"


def test_denied_columns_dropped_from_output():
    from app.core.security.data_guard import drop_denied_columns

    out = {"rows": [{"region_id": 1, "revenue": 10, "customers": 99}],
           "columns": {"region_id": {}, "revenue": {}, "customers": {}}}
    cleaned, dropped = drop_denied_columns(out, Principal(user_id="u", denied_columns=["customers"]))
    assert dropped == ["customers"]
    assert "customers" not in cleaned["rows"][0]
    assert "customers" not in cleaned["columns"]


# --------------------------------------------------------------------------- #
# 4. 端到端：权限真的作用到工具上
# --------------------------------------------------------------------------- #
def test_table_permission_blocks_tool_end_to_end(monkeypatch, tmp_path, _offline):
    """未授权表 → sql_query 被拒（NON_RETRYABLE），而不是静默少做。"""
    _keys_env(monkeypatch, allowed_tables=["dim_region"])
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()

    from app.core.agents.data_analyst.state import AgentState
    from app.core.security.auth import set_current, reset_current
    from app.core.tools import execute_tool

    token = set_current(Principal(user_id="alice", roles=["analyst"], allowed_tables=["dim_region"]))
    try:
        res = execute_tool("s1", "sql_query", {"sql": "SELECT * FROM fact_sales"}, "auth_e2e")
    finally:
        reset_current(token)

    assert res.status == "FAILED", res.output
    assert "无权访问表" in (res.error or ""), res.error
    assert res.error_class == "NON_RETRYABLE", "权限错误不该被重试"
    get_settings.cache_clear()


def test_row_filter_applies_end_to_end(monkeypatch, tmp_path, _offline):
    _keys_env(monkeypatch, row_filters={"fact_sales": "region_id = 1"})
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()

    from app.core.security.auth import set_current, reset_current
    from app.core.tools import execute_tool

    token = set_current(Principal(user_id="alice", roles=["analyst"], row_filters={"fact_sales": "region_id = 1"}))
    try:
        res = execute_tool("s1", "sql_query",
                           {"sql": "SELECT region_id, revenue FROM fact_sales"}, "auth_row")
    finally:
        reset_current(token)

    assert res.status == "SUCCESS", res.error
    rows = (res.output or {}).get("rows") or []
    assert rows and {r["region_id"] for r in rows} == {1}, "行级过滤必须真的收窄结果"
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 5. 会话归属
# --------------------------------------------------------------------------- #
def test_export_of_others_session_is_403(auth_on, tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()

    from app.core.agents.data_analyst.checkpoint import save as cp_save
    from app.core.agents.data_analyst.state import AgentState
    from app.core.memory import short_term
    from app.core.security.auth import record_session_owner

    st = AgentState(session_id="alice_sess", user_query="q", status="FINISH")
    st.report = "报告"
    cp_save(st)
    short_term.put("owner:alice_sess", "principal", {"user_id": "alice", "tenant": "acme"})

    # 两个 key 都要在：bob 读 alice 的会话应被拒，alice 自己应能读
    monkeypatch.setenv("AUTH_KEYS", json.dumps([
        {"key": "k-alice", "user_id": "alice", "tenant": "acme"},
        {"key": "k-bob", "user_id": "bob", "tenant": "acme"},
    ]))
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    # bob 读 alice 的会话 → 403（同租户也不默认开放：最小权限）
    assert c.get("/api/v1/chat/analyze/export/alice_sess",
                 headers={"X-API-Key": "k-bob"}).status_code == 403
    # 本人可读
    assert c.get("/api/v1/chat/analyze/export/alice_sess", headers=ALICE).status_code == 200
    get_settings.cache_clear()


def test_session_owner_recorded_on_analyze(auth_on, tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    from app.core.memory import short_term

    short_term._store.clear()

    from fastapi.testclient import TestClient

    from app.main import app

    TestClient(app).post("/api/v1/chat/analyze", json=PAYLOAD, headers=ALICE)
    owner = short_term.get("owner:auth_s1", "principal")
    assert owner and owner["user_id"] == "alice", owner
    get_settings.cache_clear()
