"""Pytest fixtures for REAL-LLM testing of the Data Analyst Agent.

IMPORTANT (TDD integrity):
- The real API key / model / base_url are injected via environment variables at
  the `pytest` command line (NEVER hardcoded here or in any committed file).
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
#     env at first get_settings() call). The key itself comes from the shell. ---
os.environ.setdefault("LLM_BASE_URL", "https://openrouter.ai/api/v1")
os.environ.setdefault("LLM_MODEL", "deepseek/deepseek-chat")
os.environ["MOCK_LLM"] = "false"
# Real failures must surface as real errors (no silent mock fallback).
os.environ["LLM_NO_FALLBACK"] = "true"
# 测试默认与 .env 的中间件配置隔离（进程内 dict / JSONL / SQLite），
# 防止普通测试污染 live Redis/PG/Milvus；live 测试文件自行 monkeypatch 开启。
for _var in ("REDIS_URL", "POSTGRES_DSN", "MILVUS_HOST"):
    os.environ.setdefault(_var, "")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.infrastructure.llm.router import (  # noqa: E402
    OpenAILLM,
    get_llm,
    reset_llm,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Fresh LLM/settings cache before every test."""
    reset_llm()
    get_settings.cache_clear()
    yield
    reset_llm()


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
