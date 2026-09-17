"""AUTH-02：空 `session_id` 不再共用一个桶。

动因（D54 真实基线复盘的顺带发现）
-----------------------------------
`sidecar_path()` 把空/`None` 归一成字面量 **`default`**（`attachments.py:214`），
而上传接口的默认值**就是** `"default"`（`attachments.py:48`），
`chat.py:61` 用 `req.session_id or ""` → 归一后**还是同一个桶**。

实测：`attached_tables(None)` 与 `("")` 返回同一批表。
→ **两个都不带 `session_id` 的调用方会互相看见对方上传的表**（可达路径）。

前端（`web/src/lib/api.ts:275`）一直显式回传 session_id，故只有**直接调 API** 的调用方
会走到这里 —— 影响面有界，但这是**跨调用方的数据可见性**问题。

本文件钉住：边界补齐（生成而不是拒绝）+ 桶隔离 + `default` 不再是可达路径。
"""
from __future__ import annotations

import re
import uuid

import pytest

from app.core.attachments import attached_tables, resolve_session_id, sidecar_path

_GENERATED = re.compile(r"^s_[0-9a-f]{12}$")

_CSV_A = b"region,revenue\nEast,100\nWest,200\n"
_CSV_B = b"channel,orders\nOnline,7\nRetail,9\n"


@pytest.fixture
def mock_llm(monkeypatch):
    # 变量名必须是 `MOCK_LLM`（`app/config.py::mock_llm`）——
    # 写 `MOCK_LM` 不会生效，`use_mock_llm` 因 key 存在而为假，
    # 测试会**真的去打线上模型**（实测慢 40 倍，且不是离线用例该有的行为）。
    monkeypatch.setenv("MOCK_LLM", "1")
    from app.config import get_settings
    from app.infrastructure.llm.router import reset_llm

    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _upload(client, name: str, blob: bytes, session: str | None = None):
    data = {} if session is None else {"session_id": session}
    return client.post("/api/v1/attachments/upload",
                       files={"file": (name, blob, "text/csv")}, data=data)


# --------------------------------------------------------------------------- #
# 一、`resolve_session_id`：纯函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_blank_gets_a_generated_id(blank):
    assert _GENERATED.match(resolve_session_id(blank)), resolve_session_id(blank)


def test_two_blank_calls_get_different_ids():
    """**不变量**：不能是固定值——固定值等于换了个名字的共享桶。"""
    assert resolve_session_id("") != resolve_session_id("")
    assert resolve_session_id(None) != resolve_session_id(None)


def test_provided_id_passes_through_untouched():
    """用户传什么就是什么——桶的**所有权**是 AUTH/01 的事，这里不做清洗。"""
    for sid in ("alice", "real_abc123", "sess-with-dash", "会话中文"):
        assert resolve_session_id(sid) == sid


def test_generated_id_is_safe_for_sidecar_path():
    """生成的 id 必须落在 `sidecar_path` 的字符白名单里（否则会被清洗成另一个桶）。"""
    sid = resolve_session_id("")
    assert re.sub(r"[^A-Za-z0-9_-]", "_", sid) == sid


# --------------------------------------------------------------------------- #
# 二、路由：不带 session 的两次上传**互不可见**
# --------------------------------------------------------------------------- #
def test_two_sessionless_uploads_do_not_share_a_bucket(mock_llm):
    client = _client()
    r1 = _upload(client, "a.csv", _CSV_A)
    r2 = _upload(client, "b.csv", _CSV_B)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    sid1, sid2 = r1.json()["session_id"], r2.json()["session_id"]
    assert sid1 != sid2, "两次不带 session 的上传被塞进了同一个桶"
    assert _GENERATED.match(sid1) and _GENERATED.match(sid2)

    names1 = {t.get("table") for t in attached_tables(sid1)}
    names2 = {t.get("table") for t in attached_tables(sid2)}
    assert names1 and names2
    assert not (names1 & names2), f"两个桶里的表重叠了: {names1 & names2}"


def test_explicit_session_still_accumulates(mock_llm):
    """显式传 session 的既有行为不变：第二次上传能看见第一次。"""
    client = _client()
    sid = "sess-" + uuid.uuid4().hex[:8]
    assert _upload(client, "a.csv", _CSV_A, session=sid).status_code == 200
    assert _upload(client, "b.csv", _CSV_B, session=sid).status_code == 200
    tables = {t.get("table") for t in attached_tables(sid)}
    assert len(tables) >= 2, tables


def test_listing_without_session_does_not_leak(mock_llm):
    client = _client()
    assert _upload(client, "a.csv", _CSV_A).status_code == 200
    listed = client.get("/api/v1/attachments/list")
    assert listed.status_code == 200, listed.text
    assert listed.json()["attachments"] == [], "不带 session 的 list 不该返回别人的东西"


def test_default_bucket_is_no_longer_reachable(mock_llm):
    """`sidecar_path` 的 `default` 兜底保留，但**不再是可达路径**。"""
    client = _client()
    before = attached_tables("default")
    assert _upload(client, "a.csv", _CSV_A).status_code == 200
    assert attached_tables("default") == before, "上传落进了共享的 default 桶"
    assert sidecar_path(None) == sidecar_path("default"), "兜底分支本身保留"


def test_blank_list_and_clear_get_a_generated_id(mock_llm):
    """list/clear 与 upload **同一条规则**：空 → 生成一个空桶的 id 并回声。"""
    client = _client()
    listed = client.get("/api/v1/attachments/list")
    assert listed.status_code == 200, listed.text
    assert _GENERATED.match(listed.json()["session_id"]), listed.json()

    cleared = client.delete("/api/v1/attachments/clear")
    assert cleared.status_code == 200, cleared.text
    assert _GENERATED.match(cleared.json()["session_id"]), cleared.json()


# --------------------------------------------------------------------------- #
# 三、路由：不带 session 的 /chat/analyze 也不落到共享桶
# --------------------------------------------------------------------------- #
def test_sessionless_analyze_gets_a_generated_id(mock_llm):
    """契约：`AnalyzeResponse.session_id` 是 `s_xxx`，不是 `""`/`default`。"""
    client = _client()
    r = client.post("/api/v1/chat/analyze", json={"query": "看一下本月销售额"})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert _GENERATED.match(sid), sid


def test_analyze_with_explicit_session_passes_through(mock_llm):
    """既有行为不变：显式传的 session 原样回传（不清洗、不改写）。"""
    client = _client()
    sid = "sess-" + uuid.uuid4().hex[:8]
    r = client.post("/api/v1/chat/analyze",
                    json={"query": "看一下本月销售额", "session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == sid


def test_analyze_sees_its_own_uploads(mock_llm):
    """生成的 id 是可用的：upload 回传的 id 拿去做 analyze，附件仍然可见。"""
    client = _client()
    up = _upload(client, "a.csv", _CSV_A)
    assert up.status_code == 200, up.text
    sid = up.json()["session_id"]
    assert {t.get("table") for t in attached_tables(sid)}
    r = client.post("/api/v1/chat/analyze",
                    json={"query": "按区域看营收", "session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["session_id"] == sid
