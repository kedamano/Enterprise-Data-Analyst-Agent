"""TDD fixes for issues surfaced by the user's demo run (2026-09-09).

1. MockLLM context handler must extract the query from the §21
   ``<user_request>`` envelope — the objective must not become the literal
   tag ``<user_request>``.
2. A missing SQLite data source must fail loudly instead of silently
   creating an empty database (which cascades into "缺少 table 参数"
   / ``SELECT 1`` fallbacks and fabricated-looking findings).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.core.prompts import build_user_message
from app.core.tools import execute_tool
from app.infrastructure.llm.router import MockLLM, reset_llm


@pytest.fixture
def mock_llm_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


# --------------------------------------------------------------------------- #
# 1. MockLLM must see through the §21 envelope
# --------------------------------------------------------------------------- #
def test_mock_context_objective_is_not_the_envelope_tag():
    m = MockLLM()
    user = build_user_message("分析最近三个月营收下滑的原因", {"available_data_sources": []})
    ctx = m._stage_context(user)
    assert ctx["objective"].startswith("分析最近"), f"objective 应为用户原话，实际: {ctx['objective']!r}"
    assert "<user_request>" not in ctx["objective"]
    assert "task_context" not in ctx["objective"]


def test_mock_context_envelope_falls_back_to_raw_text():
    # 无信封的裸文本（兼容旧格式）仍应可用
    m = MockLLM()
    ctx = m._stage_context("按地区分析订单量趋势")
    assert ctx["objective"].startswith("按地区")


# --------------------------------------------------------------------------- #
# 2. Missing data source must fail loudly (no silent empty sqlite)
# --------------------------------------------------------------------------- #
@pytest.fixture
def missing_db(monkeypatch, tmp_path):
    missing = tmp_path / "no_such.db"
    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{missing.as_posix()}")
    get_settings.cache_clear()
    yield missing
    get_settings.cache_clear()


def test_sql_query_fails_loudly_when_db_missing(missing_db):
    res = execute_tool("q", "sql_query",
                       {"sql": "SELECT COUNT(*) AS n FROM fact_sales"}, "missing_db")
    assert res.status == "FAILED"
    assert res.error and "数据源不存在" in res.error, res.error
    # 关键：不得静默创建空库文件
    assert not missing_db.exists(), "禁止静默创建空数据库文件"


def test_schema_search_fails_loudly_when_db_missing(missing_db):
    res = execute_tool("s", "schema_search", {"keyword": "revenue"}, "missing_db")
    assert res.status == "FAILED"
    assert res.error and "数据源不存在" in res.error
    assert not missing_db.exists()


def test_profile_fails_loudly_when_db_missing(missing_db):
    res = execute_tool("p", "dataset_profile", {"table": "fact_sales"}, "missing_db")
    assert res.status == "FAILED"
    assert res.error and "数据源不存在" in res.error
    assert not missing_db.exists()
