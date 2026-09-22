"""Pytest fixtures for REAL-LLM testing of the Data Analyst Agent.

IMPORTANT (TDD integrity):
- 真模型套件的 endpoint / model / key 一律由 **`.env` 或 shell 环境变量**提供。
  本文件**不设** `LLM_BASE_URL` / `LLM_MODEL` 的默认值——设了会盖过 `.env`
  并把 key 发到错误端点（详见下方注释，这是一次被误判数轮的假红）。
- ``LLM_NO_FALLBACK`` is forced on so that a failed real-LLM call raises the
  real error instead of silently degrading to MockLLM (which would fake green).
  When credits run out or the endpoint is rate-limited, the test simply ERRORS —
  that is an upstream condition, not a code defect, and we do NOT special-case it.
"""
from __future__ import annotations

import os
from pathlib import Path

# PyCharm 默认工作目录可能是 tests/ —— 所有相对路径（.env / data/）基于项目根解析，
# 在导入任何 app 模块之前切到项目根，保证从任意目录启动 pytest 行为一致。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_PROJECT_ROOT)

# --- Must be set BEFORE any `app` module is imported (pydantic-settings reads
#     env at first get_settings() call). ---
#
# 这里**故意不再 setdefault(LLM_BASE_URL / LLM_MODEL)**。
# 原因（2026-09-15 实测）：`setdefault` 一旦生效，写进 os.environ 的值**优先级高于 .env**
# （pydantic-settings：环境变量 > .env 文件）。旧代码把 base_url 默认成 openrouter，
# 于是 .env 里的真 key 被发到了**另一个端点**，症状是
# `401 Missing Authentication header` —— 被连着几轮误读成"没有 key / 余额不足"（假红）。
# endpoint/model 一律由 .env 或 shell 提供；两者都没有时 settings 有自己的默认值
# （app/config.py），且因 key 为空 `use_mock_llm` 为真，
# `test_agent_real.py::test_llm_backend_is_real` 会**当场失败**——不会静默跑 mock（假绿）。
os.environ["MOCK_LLM"] = "false"
# Real failures must surface as real errors (no silent mock fallback).
os.environ["LLM_NO_FALLBACK"] = "true"
# 测试默认与 .env 的中间件配置隔离（进程内 dict / JSONL / SQLite），
# 防止普通测试污染 live Redis/PG/Milvus；live 测试文件自行 monkeypatch 开启。
for _var in ("REDIS_URL", "POSTGRES_DSN", "MILVUS_HOST"):
    os.environ.setdefault(_var, "")

# --- 知识库两条 SQLite 路径隔离（必须在任何 `app` 导入之前设置）---
#
# `knowledge_tool._DB_PATH` 与 `knowledge_catalog._DB_PATH` 都是**模块级常量**，
# 在导入时求值。它们默认指向真实 `data/`，于是：
#   * 测试调 `get_catalog().create_base("诊断测试库")` 会往**真实目录**塞一条空库
#     （实测累积 16 条，直接出现在用户的知识库界面上）；
#   * 任何走 `get_store()` 的用例都会写真实分块表。
#
# 这里把两者指向真实库的**临时副本**：写入被隔离，而既有已种子内容（企业知识库
# 137 分块）仍然可见——测试若依赖它不会因此假红。
import shutil as _shutil
import tempfile as _tempfile

_KB_ISOLATION_DIR = Path(_tempfile.mkdtemp(prefix="da_kb_isolation_"))
for _db_name, _env_var in (("knowledge.db", "KNOWLEDGE_DB_PATH"),
                           ("knowledge_meta.db", "KNOWLEDGE_META_DB")):
    _src = _PROJECT_ROOT / "data" / _db_name
    _dst = _KB_ISOLATION_DIR / _db_name
    if _src.exists():
        _shutil.copy2(_src, _dst)   # 拷贝而非移动：真实库只读不动
    os.environ[_env_var] = str(_dst)

import pytest

from app.config import get_settings
from app.infrastructure.llm.router import (
    OpenAILLM,
    get_llm,
    reset_llm,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Fresh LLM/settings/cache/env/singletons — every test starts clean.

    覆盖的泄漏通道：
    - env var (AUTH_ENABLED / DATA_DB_URL / RESPONSE_CACHE_ENABLED / SHORT_TERM_TTL_S / ...)
    - settings lru_cache
    - LLM router 单例 (_llm)
    - response_cache / semantic_cache（跨测试持久化命中）
    - 模块级单例：QueryRewriter / reranker._ce_model / users_core._store
    - knowledge_tool EMV override
    """
    from app.core.tools.knowledge_tool import set_emv_override
    import os as _os

    # D61：把默认 QueryRewriter 单例归零。
    try:
        from app.core.rag.rewrite import reset_default_rewriter
        reset_default_rewriter()
    except Exception:
        pass

    reset_llm()
    # 顺序不能反：set_emv_override 内部 _resolve_emv() 会调 get_settings()。
    set_emv_override(None)
    get_settings.cache_clear()

    # 跨测试缓存隔离：response_cache._MEM 与 semantic_cache (SQLite)。
    try:
        from app.core.agents.data_analyst.response_cache import clear as _clear_resp
        _clear_resp()
    except Exception:
        pass
    try:
        from app.core.agents.data_analyst.semantic_cache import clear_semantic, _reset_connection
        clear_semantic(None)
        _reset_connection()
    except Exception:
        pass

    # 模块级单例归零（reranker CE model / token_budget / user store / knowledge_tool store）。
    try:
        from app.core.rag import rerank as _rr
        setattr(_rr, "_ce_model", None)
        setattr(_rr, "_ce_error", None)
    except Exception:
        pass
    try:
        from app.core.security import users_core as _uc
        setattr(_uc, "_store", None)
    except Exception:
        pass
    try:
        from app.core.memory import token_budget as _tb
        if hasattr(_tb, "_budgets"):
            getattr(_tb, "_budgets").clear()
    except Exception:
        pass

    # 记录 teardown 需要 restore 的 env vars（在 setup 时当前值未知，统一在 teardown 清理）。
    _leaked_env_keys = (
        "AUTH_ENABLED", "DATA_DB_URL", "RESPONSE_CACHE_ENABLED",
        "SHORT_TERM_TTL_S", "LONG_TERM_PATH", "CHECKPOINT_DIR",
    )

    yield

    # ---- Teardown：逆向恢复 ----
    set_emv_override(None)
    reset_llm()
    get_settings.cache_clear()

    for _k in _leaked_env_keys:
        _os.environ.pop(_k, None)

    try:
        from app.core.agents.data_analyst.response_cache import clear as _clear_resp
        _clear_resp()
    except Exception:
        pass
    try:
        from app.core.agents.data_analyst.semantic_cache import clear_semantic, _reset_connection
        clear_semantic(None)
        _reset_connection()
    except Exception:
        pass
    try:
        from app.core.rag import rerank as _rr
        setattr(_rr, "_ce_model", None)
        setattr(_rr, "_ce_error", None)
    except Exception:
        pass
    try:
        from app.core.security import users_core as _uc
        setattr(_uc, "_store", None)
    except Exception:
        pass
    try:
        from app.core.memory import token_budget as _tb
        if hasattr(_tb, "_budgets"):
            getattr(_tb, "_budgets").clear()
    except Exception:
        pass
    except Exception:
        pass

@pytest.fixture
def session_id() -> str:
    import uuid

    return "real_" + uuid.uuid4().hex[:10]


@pytest.fixture(scope="session", autouse=True)
def _ensure_sample_db():
    """Make sure the SQLite sample exists so tool tests are deterministic."""
    import pathlib

    db = pathlib.Path("data/sample_enterprise.db")
    if not db.exists():
        from scripts.generate_sample import main as gen_main

        gen_main()
    yield
