"""E4/02 输出脱敏：敏感列的值不进 LLM 上下文（默认开）。

Spec: docs/specs/E4/02-masking.md
手机号/邮箱/身份证/姓名一旦进 LLM 上下文就等于出境且不可撤回；
而分析师本机产物（CSV）必须可用 —— 所以**先落盘、再脱敏**，两者不能混为一谈。
"""
from __future__ import annotations

import csv
import json
import sqlite3

import pytest

from app.config import get_settings
from app.core.memory import short_term
from app.core.security.masking import (
    apply_masking,
    is_sensitive_column,
    mask_rows,
    mask_value,
)
from app.infrastructure.llm.router import reset_llm


@pytest.fixture
def mask_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("MASK_PII_ENABLED", "true")
    monkeypatch.setenv("MASK_LEVEL", "sample")
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()
    yield
    get_settings.cache_clear()
    reset_llm()
    short_term._store.clear()


def _pii_db(tmp_path) -> str:
    db = tmp_path / "crm.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE customers (customer_id INTEGER, customer_name TEXT, phone TEXT, "
                "email TEXT, amount REAL, remark TEXT)")
    con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", [
        (1, "张三", "13812345678", "zhangsan@example.com", 1200.5, "联系电话 13900001111"),
        (2, "李四", "13987654321", "lisi@corp.cn", 800.0, "正常"),
    ])
    con.commit()
    con.close()
    return f"sqlite:///{db.as_posix()}"


# --------------------------------------------------------------------------- #
# 1. 掩码算法
# --------------------------------------------------------------------------- #
def test_mask_value_algorithms():
    assert mask_value("13812345678") == "138****5678"   # 留前 3 后 4
    assert mask_value("zhangsan@example.com") == "z***@example.com"
    assert mask_value("张三") == "***"
    assert mask_value("310101199001011234") == "310***********1234"
    assert mask_value(None) == ""


def test_sensitive_column_shared_with_semantics():
    """E4/02 与 SEMANTIC/01 必须共用同一份模式表（否则规则漂移）。"""
    from app.core.semantics import is_pii_column

    for col in ("phone", "email", "customer_name", "身份证", "address"):
        assert is_sensitive_column(col) == is_pii_column(col) is True, col
    for col in ("region_name", "revenue", "sale_date"):
        assert is_sensitive_column(col) == is_pii_column(col) is False, col


# --------------------------------------------------------------------------- #
# 2. 分级语义
# --------------------------------------------------------------------------- #
def test_sample_level_masks_values_but_keeps_stats():
    rows = [{"customer_id": 1, "customer_name": "张三", "phone": "13812345678", "amount": 1200.5}]
    masked, cols = mask_rows(rows, level="sample")
    assert masked[0]["phone"] == "138****5678"
    assert masked[0]["customer_name"] == "***"
    assert masked[0]["amount"] == 1200.5, "数值列不掩码"
    assert masked[0]["customer_id"] == 1
    assert set(cols) == {"phone", "customer_name"}


def test_strict_level_drops_sensitive_columns():
    rows = [{"customer_id": 1, "customer_name": "张三", "phone": "13812345678"}]
    masked, _ = mask_rows(rows, level="strict")
    assert "phone" not in masked[0] and "customer_name" not in masked[0]
    assert masked[0]["customer_id"] == 1, "非敏感列必须保留"


def test_value_shaped_pii_masked_even_on_innocent_column():
    """列名没命中（如 remark）但值里有手机号 → 也要掩（兜底）。"""
    rows = [{"remark": "联系电话 13900001111"}]
    masked, cols = mask_rows(rows, level="sample")
    assert "13900001111" not in masked[0]["remark"]
    assert "remark" in cols


def test_sentinel_row_untouched():
    rows = [{"a": 1}, {"_truncated": 100}]
    masked, _ = mask_rows(rows, level="sample")
    assert masked[1] == {"_truncated": 100}, "上下文预算哨兵不是数据，不该被改"


def test_enums_and_profile_columns_masked():
    out = {"ok": True, "enums": {"customer_name": ["张三", "李四"], "region_name": ["华东"]},
           "columns": {"phone": {"distinct": 2, "null_ratio": 0.0}}}
    masked, cols = apply_masking(out, tool="dataset_profile", step_id="s1", level="sample")
    assert masked["enums"]["customer_name"] == ["***", "***"]
    assert masked["enums"]["region_name"] == ["华东"], "非敏感枚举不受影响"
    assert "phone" in cols


def test_strict_level_removes_distinct_from_profile():
    out = {"ok": True, "columns": {"phone": {"distinct": 2, "null_ratio": 0.0},
                                   "revenue": {"distinct": 9, "null_ratio": 0.0}}}
    masked, _ = apply_masking(out, tool="dataset_profile", step_id="s1", level="strict")
    assert "phone" not in masked["columns"], "strict 下连 distinct 都不给（防反推）"
    assert masked["columns"]["revenue"]["distinct"] == 9


# --------------------------------------------------------------------------- #
# 3. 单一收口：工具结果
# --------------------------------------------------------------------------- #
def test_tool_output_masked_but_csv_keeps_raw(mask_env, monkeypatch, tmp_path):
    """核心断言：进上下文的是掩码，落盘的 CSV 仍是原始值。"""
    monkeypatch.setenv("DATA_DB_URL", _pii_db(tmp_path))
    get_settings.cache_clear()
    from app.core.tools import execute_tool

    res = execute_tool("s1", "sql_query",
                       {"sql": "SELECT customer_id, customer_name, phone, amount FROM customers"},
                       "mask_session")
    assert res.status == "SUCCESS", res.error
    rows = res.output["rows"]
    assert rows and rows[0]["phone"] == "138****5678", rows[:1]
    assert rows[0]["customer_name"] == "***"
    assert "13812345678" not in json.dumps(rows, ensure_ascii=False)

    # 落盘产物不脱敏（分析师本机要用）
    with open(res.output["csv_path"], newline="", encoding="utf-8") as fh:
        disk = list(csv.DictReader(fh))
    assert disk[0]["phone"] == "13812345678", "CSV 必须保留原始值"
    assert res.output.get("masked_columns"), "应记录本次掩了哪些列"


def test_masking_disabled_is_audited(mask_env, monkeypatch, tmp_path):
    """显式关闭脱敏这件事本身必须留痕（默认开的边界不能被悄悄绕过）。"""
    monkeypatch.setenv("DATA_DB_URL", _pii_db(tmp_path))
    monkeypatch.setenv("MASK_LEVEL", "none")
    get_settings.cache_clear()
    from app.core.tools import execute_tool

    res = execute_tool("s2", "sql_query", {"sql": "SELECT phone FROM customers"}, "mask_off")
    assert res.output["rows"][0]["phone"] == "13812345678", "none 级别不脱敏"

    from app.core.security.masking import MASKING_AUDIT_LOG

    records = [json.loads(ln) for ln in MASKING_AUDIT_LOG.read_text("utf-8").splitlines()
               if ln.strip()]
    assert any(r["level"] == "none" and r["step_id"] == "s2" for r in records[-5:]), records[-5:]


def test_masking_failure_fails_closed(monkeypatch):
    """隐私控制不允许 fail-open：脱敏出错宁可丢掉行样本。"""
    import app.core.security.masking as masking

    monkeypatch.setattr(masking, "mask_structured",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = {"rows": [{"phone": "13812345678"}]}
    masked, cols = masking.apply_masking(out, tool="sql_query", step_id="s9", level="sample")
    assert masked["rows"] == [], "出错必须丢掉行样本，而不是放行原始数据"
    assert "masking_error" in masked
    assert cols == []
