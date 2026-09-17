"""D45：**两步授权 HITL** —— 高危动作需二次确认。

缺口（Gap 五）
-------------
> 缺 **两步授权 / 高危确认 HITL** 策略引擎。

现状是"要么全放、要么硬拦"：读库有只读守卫、写码有沙箱 + AST、导出有路径白名单——
但**没有一个"这一步值得让人看一眼再放行"的机制**。而恰恰有几类动作是合规上需要人过目的：

| 动作 | 为什么需要人看一眼 |
|---|---|
| `export_raw` | 导出物**保留未脱敏原始值**（E4/02 只约束进 LLM 上下文的那份），对外分享前需确认 |
| `deliver_python` | 交付的脚本由模型生成，将在沙箱内执行（任意代码执行面） |
| `masking_disabled` | 关掉脱敏会让敏感值进 LLM 上下文 |

**默认关**（`HITL_ENABLED=false`）：既有 950+ 用例与本地开发行为**零影响**。

两条安全纪律（与 E4/02 脱敏同源）
--------------------------------
1. **失败即关闭（fail-closed）**：策略引擎自己出错时**必须判定"需要确认"**，
   而不是放行。安全控制与其他降级不同，不能"退化为可用"。
2. **未知动作默认需确认**：新加的动作在明确登记之前，默认走确认流程——
   默认放行会让"忘了登记"变成"静默开了口子"。

授权凭证用 `secrets.token_urlsafe`：**不可从 session 推导**，
否则调用方能自己伪造一个"已确认"。
"""
from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.core.security import hitl
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def hitl_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HITL_ENABLED", "true")
    monkeypatch.setenv("HITL_AUDIT_LOG", str(tmp_path / "hitl.jsonl"))
    get_settings.cache_clear()
    hitl.reset("s-hitl")   # 清 pending **与**一次性放行（否则授权会漏到下一个用例）
    yield tmp_path / "hitl.jsonl"
    get_settings.cache_clear()


def _audit_lines(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------- #
# 一、默认关：既有行为零影响
# --------------------------------------------------------------------------- #
def test_disabled_by_default():
    assert get_settings().hitl_enabled is False


def test_nothing_gated_when_disabled(monkeypatch):
    monkeypatch.setenv("HITL_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert hitl.requires_confirmation("export_raw") is None
        assert hitl.requires_confirmation("deliver_python") is None
    finally:
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# 二、策略判定
# --------------------------------------------------------------------------- #
def test_known_high_risk_action_is_gated(hitl_env):
    risk = hitl.requires_confirmation("export_raw")
    assert risk is not None and risk.action == "export_raw"
    assert "脱敏" in risk.reason, "理由要能回答「为什么要确认」"


def test_low_risk_action_is_not_gated(hitl_env):
    assert hitl.requires_confirmation("run_sql_readonly") is None


def test_unknown_action_defaults_to_gated(hitl_env):
    """**未知动作默认需确认**——否则"忘了登记"就等于静默开口子。"""
    risk = hitl.requires_confirmation("some_new_dangerous_thing")
    assert risk is not None


def test_policy_error_fails_closed(monkeypatch, hitl_env):
    """策略引擎自己炸了 → **判定需要确认**（fail-closed），绝不放行。"""
    monkeypatch.setattr(hitl, "_RULES", None)     # 制造内部错误
    risk = hitl.requires_confirmation("export_raw")
    assert risk is not None, "安全控制不能 fail-open"


# --------------------------------------------------------------------------- #
# 三、确认流转
# --------------------------------------------------------------------------- #
def test_begin_stores_pending_with_unguessable_token(hitl_env):
    hitl.begin("s-hitl", "export_raw", {"format": "zip"})
    pending = short_term.get("s-hitl", "pending_confirmation")
    assert pending and pending["action"] == "export_raw"
    assert pending["token"] and len(pending["token"]) >= 16
    assert "s-hitl" not in pending["token"], "凭证不得从 session 推导（否则可伪造）"


def test_approve_with_correct_token(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    pending = short_term.get("s-hitl", "pending_confirmation")
    ok, msg = hitl.decide("s-hitl", pending["token"], approved=True)
    assert ok and "确认" in msg
    assert short_term.get("s-hitl", "pending_confirmation") is None, "确认后必须清 pending"


def test_deny_is_honoured(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    pending = short_term.get("s-hitl", "pending_confirmation")
    ok, msg = hitl.decide("s-hitl", pending["token"], approved=False)
    assert not ok and "拒绝" in msg


def test_wrong_token_is_rejected(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    ok, _ = hitl.decide("s-hitl", "forged-token", approved=True)
    assert not ok, "伪造凭证不得放行"
    assert short_term.get("s-hitl", "pending_confirmation") is not None, "失败不得清掉 pending"


def test_decide_without_pending_is_rejected(hitl_env):
    ok, msg = hitl.decide("s-hitl", "whatever", approved=True)
    assert not ok and "没有" in msg


# --------------------------------------------------------------------------- #
# 四、审计：允许与拒绝**都记**
# --------------------------------------------------------------------------- #
def test_both_allow_and_deny_are_audited(hitl_env):
    """只记拒绝无法复盘（同 AUTH/01 的取舍）。"""
    hitl.begin("s-hitl", "export_raw")
    t1 = short_term.get("s-hitl", "pending_confirmation")["token"]
    hitl.decide("s-hitl", t1, approved=True)

    hitl.begin("s-hitl", "deliver_python")
    t2 = short_term.get("s-hitl", "pending_confirmation")["token"]
    hitl.decide("s-hitl", t2, approved=False)

    lines = _audit_lines(hitl_env)
    assert [x["decision"] for x in lines] == ["ALLOW", "DENY"]
    assert all(x["action"] for x in lines)


def test_wrong_token_is_audited_as_deny(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    hitl.decide("s-hitl", "forged", approved=True)
    lines = _audit_lines(hitl_env)
    assert lines and lines[-1]["decision"] == "DENY"


# --------------------------------------------------------------------------- #
# 五、一次性放行（否则调用方重试会被反复拦住）
# --------------------------------------------------------------------------- #
def test_approve_creates_a_one_shot_grant(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    token = short_term.get("s-hitl", "pending_confirmation")["token"]
    hitl.decide("s-hitl", token, approved=True)

    assert hitl.take_grant("s-hitl", "export_raw") is True
    assert hitl.take_grant("s-hitl", "export_raw") is False, "放行必须是**一次性**的"


def test_grant_is_scoped_to_the_action(hitl_env):
    """确认了导出，不等于批准了交付脚本。"""
    hitl.begin("s-hitl", "export_raw")
    token = short_term.get("s-hitl", "pending_confirmation")["token"]
    hitl.decide("s-hitl", token, approved=True)
    assert hitl.take_grant("s-hitl", "deliver_python") is False


def test_deny_creates_no_grant(hitl_env):
    hitl.begin("s-hitl", "export_raw")
    token = short_term.get("s-hitl", "pending_confirmation")["token"]
    hitl.decide("s-hitl", token, approved=False)
    assert hitl.take_grant("s-hitl", "export_raw") is False


# --------------------------------------------------------------------------- #
# 六、端到端 API 流转：428 → /confirm → 重试成功
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "ck"))
    monkeypatch.setenv("HITL_ENABLED", "true")
    monkeypatch.setenv("HITL_AUDIT_LOG", str(tmp_path / "hitl.jsonl"))
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    hitl.reset("hitl-api")
    yield tmp_path / "hitl.jsonl"
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    hitl.reset("hitl-api")


def _finished_session(session_id: str = "hitl-api") -> str:
    from app.core.agents.data_analyst.graph import run_analysis

    run_analysis(session_id, "分析各区域营收")
    return session_id


def test_api_export_is_gated_then_succeeds_after_confirm(api_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        sid = _finished_session()
        first = c.get(f"/api/v1/chat/analyze/export/{sid}?format=zip")
        assert first.status_code == 428, first.text[:200]
        body = first.json()["detail"]
        assert body["action"] == "export_raw" and body["token"]

        ok = c.post("/api/v1/chat/analyze/confirm",
                    json={"session_id": sid, "token": body["token"], "approved": True})
        assert ok.status_code == 200, ok.text[:200]

        again = c.get(f"/api/v1/chat/analyze/export/{sid}?format=zip")
        assert again.status_code == 200, "确认后重试必须放行（一次性放行）"


def test_api_confirm_with_wrong_token_is_409(api_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        sid = _finished_session()
        c.get(f"/api/v1/chat/analyze/export/{sid}?format=zip")
        bad = c.post("/api/v1/chat/analyze/confirm",
                     json={"session_id": sid, "token": "forged", "approved": True})
        assert bad.status_code == 409, bad.text[:200]


def test_api_deny_keeps_gate_closed(api_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        sid = _finished_session()
        first = c.get(f"/api/v1/chat/analyze/export/{sid}?format=zip")
        token = first.json()["detail"]["token"]
        c.post("/api/v1/chat/analyze/confirm",
               json={"session_id": sid, "token": token, "approved": False})
        still = c.get(f"/api/v1/chat/analyze/export/{sid}?format=zip")
        assert still.status_code == 428, "拒绝之后不得放行"
