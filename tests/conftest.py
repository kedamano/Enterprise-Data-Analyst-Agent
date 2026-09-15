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
