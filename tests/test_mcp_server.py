"""D50：MCP 接入层 —— 把**只读**工具按 MCP 协议暴露（可发现 / 可授权 / 可观测）。

Spec: docs/specs/MCP/01-adapter.md

暴露边界（**宁可少暴露，不可多暴露**）：只暴露 READ_METADATA/READ_KNOWLEDGE/READ_DATA。
`python_analysis`（任意代码执行面）/`visualization`/`generate_report` **绝不暴露**，
`image_analyze` 因耦合附件挂载而显式排除。

三支柱：
1. **可发现** —— `list_tools()` 返回 name/description/inputSchema，schema 直接取自
   `TOOL_SPECS`（§22 已声明式登记，接入层不另写一份）。
2. **可授权** —— 每个 `call_tool` 都走 `execute_tool()`（RBAC 门禁/限流/守卫/审计全在）。
3. **可观测** —— 权限被拒**也**写 tool 审计（否则"谁在探边界"无从复盘）。

接入层不写任何安全逻辑（鉴权/限流/守卫委托 `execute_tool`），只负责 **MCP 协议形态
到进程内工具调用的桥接**——这是"协议适配层"与"安全层"的边界。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.infrastructure.llm.router import reset_llm

READONLY_TOOLS = {"schema_search", "knowledge_search", "dataset_profile", "sql_query", "freeform"}
NEVER_TOOLS = {"python_analysis", "visualization", "generate_report", "image_analyze"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("MCP_ENABLED", "true")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield tmp_path
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def _db(monkeypatch, tmp_path) -> None:
    db = tmp_path / "mcp.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sales (region TEXT, revenue REAL)")
    con.executemany("INSERT INTO sales VALUES (?,?)",
                    [("华东", 1.2), ("华北", 0.8)])
    con.commit()
    con.close()
    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{db.as_posix()}")
    get_settings.cache_clear()


def _as_role(monkeypatch, roles: list[str], user_id: str = "u"):
    from app.core.security.auth import Principal

    p = Principal(user_id=user_id, roles=roles)
    monkeypatch.setattr("app.core.security.auth.current_principal", lambda: p)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_KEYS", json.dumps([{"key": "k", "user_id": user_id, "roles": roles}]))
    get_settings.cache_clear()
    return p


def _audit_lines(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --------------------------------------------------------------------------- #
# 一、可发现
# --------------------------------------------------------------------------- #
def test_list_tools_exposes_exactly_the_readonly_set(env):
    import asyncio
    from app.core.integrations.mcp_server import build_mcp_server

    tools = asyncio.run(build_mcp_server().list_tools())
    names = {t.name for t in tools}
    assert names == READONLY_TOOLS, names


def test_list_tools_never_exposes_code_or_artifact_tools(env):
    """任意代码执行面绝不暴露给外部 client。"""
    import asyncio
    from app.core.integrations.mcp_server import build_mcp_server

    tools = asyncio.run(build_mcp_server().list_tools())
    names = {t.name for t in tools}
    assert names.isdisjoint(NEVER_TOOLS), f"不该暴露: {names & NEVER_TOOLS}"


def test_listed_tools_carry_schema_and_readonly_hint(env):
    import asyncio
    from app.core.integrations.mcp_server import build_mcp_server

    for tool in asyncio.run(build_mcp_server().list_tools()):
        assert tool.name and tool.description, f"{tool.name} 缺描述"
        assert tool.input_schema, f"{tool.name} 缺 input_schema"
        assert tool.input_schema.get("type") == "object", tool.input_schema
        assert tool.annotations is not None and tool.annotations.read_only_hint is True, \
            f"{tool.name} 未标 read_only_hint"


def test_mcp_schema_pinned_to_tool_specs(env):
    """**签名漂移即测试失败**——MCP 2.x 从函数签名推导 input_schema，无法从外部注入。
    所以显式包装函数的参数必须与 `TOOL_SPECS.input_schema.properties` 的 key
    与 required 对齐。"""
    import inspect
    from app.core.integrations import mcp_server as mod
    from app.core.tools.specs import TOOL_SPECS

    by_name = {name: getattr(mod, f"mcp_{name}") for name in READONLY_TOOLS}
    for name, fn in by_name.items():
        sig = inspect.signature(fn)
        props = set(TOOL_SPECS[name].input_schema.get("properties") or {})
        required = set(TOOL_SPECS[name].input_schema.get("required") or [])
        # 必须含所有 required 参数
        missing = required - set(sig.parameters.keys())
        assert not missing, f"{name} 缺 required 参数: {missing}"
        # 所有 fn 参数都必须是 TOOL_SPECS 里出现的（防止参数名漂移）
        extra = set(sig.parameters.keys()) - props - {"self"}
        assert not extra, f"{name} 多了多余参数: {extra}"


# --------------------------------------------------------------------------- #
# 二、可授权（复用 AUTH/01，接入层不重写鉴权）
# --------------------------------------------------------------------------- #
def test_call_tool_denies_without_permission_and_does_not_execute(env, monkeypatch):
    """viewer 无 READ_DATA → `isError`，且**工具函数根本不被调用**。"""
    from app.core.integrations.mcp_server import build_mcp_server, result_to_mcp, invoke_tool
    from app.core.tools import REGISTRY
    from app.core.tools import sql_tool

    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["viewer"])

    called: list = []

    def spy(params):
        called.append(params)
        return {"ok": True, "rows": []}

    REGISTRY["sql_query"] = spy
    try:
        result = result_to_mcp(invoke_tool("sql_query", {"sql": "SELECT 1", "database": "default"}))
    finally:
        REGISTRY["sql_query"] = sql_tool.run
    assert result.is_error is True
    assert "权限不足" in _text_of(result)
    assert called == [], "权限不足时工具函数绝不执行"


def test_call_tool_succeeds_with_sufficient_permission(env, monkeypatch):
    from app.core.integrations.mcp_server import build_mcp_server, result_to_mcp, invoke_tool

    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["analyst"])

    result = result_to_mcp(invoke_tool("schema_search", {"query": "sales"}))
    assert result.is_error is False
    assert "sales" in _text_of(result).lower()


# --------------------------------------------------------------------------- #
# 三、可观测：成功与拒绝**都**落审计
# --------------------------------------------------------------------------- #
def test_call_tool_audits_success(env, monkeypatch):
    from app.core.integrations.mcp_server import build_mcp_server, result_to_mcp, invoke_tool

    audit = env / "tool_audit.jsonl"
    monkeypatch.setattr("app.core.tools.AUDIT_LOG", audit)
    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["analyst"])

    invoke_tool("schema_search", {"query": "sales"})
    lines = _audit_lines(audit)
    assert any(x["tool"] == "schema_search" and x["status"] == "SUCCESS" for x in lines), lines


def test_call_tool_audits_permission_denial(env, monkeypatch):
    """拒绝必须也落审计——否则"谁在探边界"无从复盘。"""
    from app.core.integrations.mcp_server import build_mcp_server, result_to_mcp, invoke_tool

    audit = env / "tool_audit.jsonl"
    monkeypatch.setattr("app.core.tools.AUDIT_LOG", audit)
    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["viewer"])

    invoke_tool("sql_query", {"sql": "SELECT 1", "database": "default"})
    lines = _audit_lines(audit)
    assert any(x["tool"] == "sql_query" and x["status"] == "FAILED" for x in lines), lines


# --------------------------------------------------------------------------- #
# 四、健壮性：失败是**结果**而非**异常**
# --------------------------------------------------------------------------- #
def test_invoke_unknown_tool_returns_error(env):
    from app.core.integrations.mcp_server import invoke_tool

    result = invoke_tool("no_such_tool", {})
    assert result["status"] == "FAILED"


def test_invoke_tool_exception_becomes_error_not_raise(env, monkeypatch):
    """工具内部炸了 → 转 `isError`，绝不裸抛打挂 server。"""
    from app.core.integrations.mcp_server import invoke_tool
    from app.core.tools import REGISTRY

    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["analyst"])

    def boom(params):
        raise RuntimeError("db exploded")

    REGISTRY["sql_query"] = boom
    try:
        result = invoke_tool("sql_query", {"sql": "SELECT 1", "database": "default"})
    finally:
        from app.core.tools import sql_tool
        REGISTRY["sql_query"] = sql_tool.run
    assert result["status"] == "FAILED"


# --------------------------------------------------------------------------- #
# 五、REST 面
# --------------------------------------------------------------------------- #
def test_rest_list_tools(env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/v1/mcp/tools")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    assert names == READONLY_TOOLS, names


def test_rest_call_tool(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _db(monkeypatch, env)
    _as_role(monkeypatch, roles=["analyst"])

    with TestClient(app) as c:
        r = c.post("/api/v1/mcp/call",
                   json={"tool": "schema_search", "arguments": {"query": "sales"}},
                   headers={"X-API-Key": "k"})
    assert r.status_code == 200, r.text[:200]
    assert r.json()["status"] == "SUCCESS"


def test_rest_503_when_mcp_disabled(monkeypatch):
    """默认关 → 两端点 **503**（配置未启用要吵，不静默空跑）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("MCP_ENABLED", "false")
    get_settings.cache_clear()

    with TestClient(app) as c:
        assert c.get("/api/v1/mcp/tools").status_code == 503
        assert c.post("/api/v1/mcp/call",
                      json={"tool": "schema_search", "arguments": {}}).status_code == 503


def test_mcp_disabled_by_default():
    assert get_settings().mcp_enabled is False


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _text_of(result) -> str:
    """从 MCP CallToolResult 里取出文本内容。"""
    parts = []
    for item in (getattr(result, "content", None) or []):
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
        else:
            parts.append(str(item))
    return "\n".join(parts)
