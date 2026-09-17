"""D49：DLP 细粒度脱敏 — 角色/字段级策略 + 可验证水印 + 导出审批流。

Spec: docs/specs/E4/06-dlp-field-level.md

E4/02 的脱敏是**全局一档**（`mask_level` 一个值管所有人），且只约束"进 LLM 上下文"
的那一份；导出物全有全无（原始值 or 拦截）。D49 把脱敏做到**角色×字段**，
并给每份导出加**可验证**水印；脱敏版是安全默认，原始版走 D45 HITL 审批。

两条纪律（与 D45/E4/02 同源）：
1. **权限高于策略**——`Principal.denied_columns` 永远 strict，角色策略不能把权限模型放宽。
2. **默认零影响**——`DLP_POLICY` 空 + `DLP_WATERMARK_SECRET` 空 → 导出行为一字不变。
"""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.memory import short_term
from app.core.security import hitl
from app.infrastructure.llm.router import reset_llm

POLICY = {
    "analyst": {"default_level": "sample", "column_levels": {"bank_card": "strict"}},
    "intern": {"default_level": "strict", "deny_columns": ["salary"]},
}
SECRET = "test-secret-000"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def _set_policy(monkeypatch, policy):
    monkeypatch.setenv("DLP_POLICY", json.dumps(policy) if policy is not None else "")
    get_settings.cache_clear()


def _principal(**kw):
    from app.core.security.auth import Principal

    return Principal(**kw)


# --------------------------------------------------------------------------- #
# 一、角色/字段级策略解析
# --------------------------------------------------------------------------- #
def test_column_level_beats_role_default(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, POLICY)
    p = _principal(roles=["analyst"])
    assert dlp.resolve_level(p, "revenue") == "sample"     # 角色 default
    assert dlp.resolve_level(p, "bank_card") == "strict"   # 列级覆盖


def test_longest_key_wins(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, {"analyst": {"default_level": "none",
                                          "column_levels": {"phone": "none",
                                                            "customer_phone": "strict"}}})
    p = _principal(roles=["analyst"])
    assert dlp.resolve_level(p, "phone") == "none"
    assert dlp.resolve_level(p, "customer_phone") == "strict", "最长 key 优先"


def test_role_deny_columns_is_strict(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, POLICY)
    assert dlp.resolve_level(_principal(roles=["intern"]), "salary") == "strict"


def test_principal_denied_columns_is_authoritative(monkeypatch):
    """权限模型高于角色策略：即使角色策略写 none，denied_columns 仍是 strict。"""
    from app.core.security import dlp

    _set_policy(monkeypatch, {"data_owner": {"default_level": "none"}})
    p = _principal(roles=["data_owner"], denied_columns=["salary"])
    assert dlp.resolve_level(p, "salary") == "strict"
    assert dlp.resolve_level(p, "revenue") == "none"


def test_unknown_role_falls_back_to_global(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, POLICY)
    monkeypatch.setenv("MASK_LEVEL", "sample")
    get_settings.cache_clear()
    assert dlp.resolve_level(_principal(roles=["nobody"]), "phone") == "sample"


def test_empty_policy_falls_back_to_global(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, None)
    monkeypatch.setenv("MASK_LEVEL", "strict")
    get_settings.cache_clear()
    assert dlp.resolve_level(_principal(roles=["analyst"]), "phone") == "strict"


def test_malformed_policy_does_not_raise(monkeypatch):
    from app.core.security import dlp

    monkeypatch.setenv("DLP_POLICY", "{not valid json")
    get_settings.cache_clear()
    monkeypatch.setenv("MASK_LEVEL", "sample")
    get_settings.cache_clear()
    assert dlp.resolve_level(_principal(roles=["analyst"]), "phone") == "sample"


def test_bad_level_falls_back_to_global(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, {"analyst": {"default_level": "ultra_secret"}})
    monkeypatch.setenv("MASK_LEVEL", "sample")
    get_settings.cache_clear()
    assert dlp.resolve_level(_principal(roles=["analyst"]), "phone") == "sample"


# --------------------------------------------------------------------------- #
# 二、CSV 按列脱敏
# --------------------------------------------------------------------------- #
def test_mask_csv_text_mixed_levels(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, POLICY)
    p = _principal(roles=["analyst"])
    text = "id,phone,bank_card,revenue\n1,13812345678,6222021234567890,1200\n"
    out = dlp.mask_csv_text(text, dlp.make_resolver(p))
    lines = out.strip().splitlines()
    header, row = lines[0].split(","), lines[1].split(",")
    assert "bank_card" not in header, "strict 列连表头都剔除"
    assert "phone" in header
    assert "13812345678" not in out and "138****5678" in out, "sample 列按值掩码"
    assert header[-1] == "revenue" and row[-1] == "1200", "none 列原样"


def test_mask_csv_text_drops_denied_column(monkeypatch):
    from app.core.security import dlp

    _set_policy(monkeypatch, {"intern": {"default_level": "strict",
                                         "deny_columns": ["salary"]}})
    p = _principal(roles=["intern"])
    out = dlp.mask_csv_text("id,salary\n1,20000\n", dlp.make_resolver(p))
    assert "salary" not in out and "20000" not in out


# --------------------------------------------------------------------------- #
# 三、水印（可验证，不可伪造）
# --------------------------------------------------------------------------- #
def test_watermark_round_trip():
    from app.core.security.watermark import issue_watermark, verify_watermark

    tok = issue_watermark(user_id="alice", session_id="s-1",
                          exported_at="2026-09-14T00:00:00+00:00", secret=SECRET)
    assert tok.startswith("v1.")
    meta = verify_watermark(tok, SECRET)
    assert meta and meta["user_id"] == "alice" and meta["session_id"] == "s-1"


def test_watermark_tampered_payload_is_rejected():
    from app.core.security.watermark import issue_watermark, verify_watermark

    tok = issue_watermark(user_id="alice", session_id="s-1",
                          exported_at="t", secret=SECRET)
    payload, _, sig = tok.split(".")
    forged = f"{payload}.{sig[::-1]}"          # 翻转签名
    assert verify_watermark(forged, SECRET) is None


def test_watermark_wrong_secret_is_rejected():
    from app.core.security.watermark import issue_watermark, verify_watermark

    tok = issue_watermark(user_id="alice", session_id="s-1",
                          exported_at="t", secret=SECRET)
    assert verify_watermark(tok, "other-secret") is None


def test_watermark_bad_format_is_rejected():
    from app.core.security.watermark import verify_watermark

    assert verify_watermark("not-a-token", SECRET) is None
    assert verify_watermark("", SECRET) is None
    assert verify_watermark(None, SECRET) is None


def test_no_watermark_when_secret_empty():
    from app.core.security.watermark import issue_watermark

    assert issue_watermark(user_id="a", session_id="s", exported_at="t", secret="") is None


# --------------------------------------------------------------------------- #
# 四、导出集成：策略 + 水印 + 审批流
# --------------------------------------------------------------------------- #
@pytest.fixture
def pii_session(env, tmp_path, monkeypatch, request):
    """造一个含手机号的会话（导出 CSV 里带原始值）。

    session_id **必须每个用例唯一**：``run_analysis`` 有请求级响应缓存，
    同 (session_id, query) 第二次调用会**直接返回缓存而不落 checkpoint**——
    4 个导出用例共用一个 id 时，第二个起必然 404（与 test_export.py 逐例唯一 id 同范式）。
    """
    db = tmp_path / "crm.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE c (id INTEGER, phone TEXT)")
    con.executemany("INSERT INTO c VALUES (?,?)", [(1, "13812345678")])
    con.commit()
    con.close()
    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{db.as_posix()}")
    get_settings.cache_clear()

    from app.core.agents.data_analyst.checkpoint import load as cp_load
    from app.core.agents.data_analyst.graph import run_analysis

    sid = f"dlp-{request.node.name}"
    run_analysis(sid, "看看 c 表")
    assert cp_load(sid) is not None, f"run_analysis 未落 checkpoint（cwd={Path.cwd()}）"
    return sid


def _zip_of(client, url):
    r = client.get(url)
    assert r.status_code == 200, r.text[:200]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}, r.headers


def test_export_applies_role_policy_when_masked(env, monkeypatch, pii_session):
    from fastapi.testclient import TestClient

    from app.core.security import dlp
    from app.core.security.auth import Principal
    from app.main import app

    _set_policy(monkeypatch, POLICY)
    monkeypatch.setattr("app.core.security.auth.current_principal",
                        lambda: _principal(roles=["intern"]))  # 全 strict
    monkeypatch.setenv("DLP_WATERMARK_SECRET", SECRET)
    get_settings.cache_clear()

    with TestClient(app) as c:
        files, headers = _zip_of(c, f"/api/v1/chat/analyze/export/{pii_session}?masked=1")
    csv_text = b"".join(v for k, v in files.items() if k.endswith(".csv")).decode("utf-8")
    assert "13812345678" not in csv_text, "intern 角色导出不得含原始手机号"
    assert "masked" in headers  # 策略应用标记


def test_export_emits_verifiable_watermark(env, monkeypatch, pii_session):
    from fastapi.testclient import TestClient

    from app.core.security.watermark import verify_watermark
    from app.main import app

    _set_policy(monkeypatch, POLICY)
    monkeypatch.setattr("app.core.security.auth.current_principal",
                        lambda: _principal(user_id="alice", roles=["analyst"]))
    monkeypatch.setenv("DLP_WATERMARK_SECRET", SECRET)
    get_settings.cache_clear()

    with TestClient(app) as c:
        files, headers = _zip_of(c, f"/api/v1/chat/analyze/export/{pii_session}?masked=1")
    assert "WATERMARK.txt" in files, "导出包必须带水印文件"
    tok = headers.get("x-dlp-watermark") or headers.get("X-Dlp-Watermark")
    assert tok, "响应头必须带水印 token（便于接收方即时校验）"
    meta = verify_watermark(tok, SECRET)
    assert meta and meta["user_id"] == "alice", meta


def test_export_raw_requires_hitl_when_policy_active(env, monkeypatch, pii_session):
    from fastapi.testclient import TestClient

    from app.main import app

    _set_policy(monkeypatch, POLICY)
    monkeypatch.setattr("app.core.security.auth.current_principal",
                        lambda: _principal(roles=["analyst"]))
    monkeypatch.setenv("HITL_ENABLED", "true")
    monkeypatch.setenv("HITL_AUDIT_LOG", "data/audit/hitl.jsonl")
    get_settings.cache_clear()
    hitl.reset(pii_session)
    sid = pii_session
    try:
        with TestClient(app) as c:
            r = c.get(f"/api/v1/chat/analyze/export/{sid}?masked=0")
            assert r.status_code == 428, r.text[:200]
            body = r.json()["detail"]
            assert body["action"] == "export_raw" and body["token"]
            assert "confirm" in body["howto"]
    finally:
        hitl.reset(sid)


def test_export_zero_impact_when_no_policy(env, monkeypatch, pii_session):
    """DLP_POLICY 空 + 无 secret → 缺省 masked=0、原始导出、无水印（与 D48 一字不变）。"""
    from app.main import app

    _set_policy(monkeypatch, None)
    monkeypatch.delenv("DLP_WATERMARK_SECRET", raising=False)
    get_settings.cache_clear()

    with TestClient(app) as c:
        files, headers = _zip_of(c, f"/api/v1/chat/analyze/export/{pii_session}")
    csv_text = b"".join(v for k, v in files.items() if k.endswith(".csv")).decode("utf-8")
    assert "13812345678" in csv_text, "无策略时导出仍是原始值"
    assert "WATERMARK.txt" not in files


# --------------------------------------------------------------------------- #
# 五、水印 verify HTTP 端点（E4/06 §2.3 — secret 只从服务端读取，不接受客户端传入）
# --------------------------------------------------------------------------- #
@pytest.fixture
def wm_env(monkeypatch):
    """独水印 verify 端点用的环境：启用水印 secret，清理缓存。"""
    monkeypatch.setenv("DLP_WATERMARK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("DLP_WATERMARK_SECRET", raising=False)
    get_settings.cache_clear()


def _issue(**kw):
    from app.core.security.watermark import issue_watermark

    return issue_watermark(secret=SECRET, **kw)


def test_watermark_verify_post_200_round_trip(wm_env):
    """POST 正样本 → 200 + ok=True + 解析出的 payload。"""
    from app.main import app

    tok = _issue(user_id="alice", session_id="s-1",
                 exported_at="2026-09-14T00:00:00+00:00")
    with TestClient(app) as c:
        r = c.post("/api/v1/security/watermark/verify", json={"token": tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["watermark"]["user_id"] == "alice"
    assert body["watermark"]["session_id"] == "s-1"


def test_watermark_verify_get_200_round_trip(wm_env):
    """GET 正样本 → 200（WATERMARK.txt 里的离线校验链接场景）。"""
    from app.main import app

    tok = _issue(user_id="bob", session_id="s-2", exported_at="t")
    with TestClient(app) as c:
        r = c.get("/api/v1/security/watermark/verify", params={"token": tok})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["watermark"]["user_id"] == "bob"


def test_watermark_verify_tampered_returns_422(wm_env):
    """篡改 token → 422 + ok=False + reason，且不暴露 secret。"""
    from app.main import app

    tok = _issue(user_id="alice", session_id="s-1", exported_at="t")
    forged = f"{tok[:-3]}XYZ"  # 破坏十六进制签名
    with TestClient(app) as c:
        r = c.post("/api/v1/security/watermark/verify", json={"token": forged})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["ok"] is False
    assert "reason" in body
    assert SECRET not in str(body), "响应里不得泄漏 secret"


def test_watermark_verify_wrong_secret_returns_422(wm_env):
    """token 是别的 secret 签的 → 用当前 secret 校验应 422。"""
    from app.core.security.watermark import issue_watermark
    from app.main import app

    other = issue_watermark(user_id="alice", session_id="s-1",
                            exported_at="t", secret="other-secret")
    with TestClient(app) as c:
        r = c.post("/api/v1/security/watermark/verify", json={"token": other})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["ok"] is False


def test_watermark_verify_bad_format_returns_422(wm_env):
    """格式错误的 token（非 v1.x.y / 空）→ 422，而非 500。"""
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/api/v1/security/watermark/verify", json={"token": "garbage"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["ok"] is False


def test_watermark_verify_missing_secret_returns_409(monkeypatch):
    """服务未配 DLP_WATERMARK_SECRET → 409（校验能力不存在，不是 token 无效）。"""
    monkeypatch.delenv("DLP_WATERMARK_SECRET", raising=False)
    get_settings.cache_clear()
    from app.main import app

    tok = _issue(user_id="alice", session_id="s-1", exported_at="t")
    with TestClient(app) as c:
        r = c.post("/api/v1/security/watermark/verify", json={"token": tok})
    assert r.status_code == 409, r.text
    assert "不可用" in r.json()["detail"]
