"""工具结果截断与 CSV 物化：>2000 行不得整体失败，也不得悄悄少写。

来源：S1 端到端探针（一条 15600 行的 CROSS JOIN）暴露的既有缺陷——
`_cap_output` 截断时在 rows 末尾追加哨兵行 ``{"_truncated": N}``，
而 ``_to_result`` 用 ``csv.DictWriter(fieldnames=列名)`` 直接写全部行 →
``ValueError: dict contains fields not in fieldnames: '_truncated'`` →
**任何超过 2000 行的查询都整体 FAILED**（报的还是这句与业务无关的话）。
"""
from __future__ import annotations

import csv

import pytest

from app.config import get_settings
from app.core.tools import execute_tool
from app.infrastructure.llm.router import reset_llm

BIG_QUERY = "SELECT f.sale_id, g.sale_id AS other FROM fact_sales f CROSS JOIN fact_sales g"


@pytest.fixture
def tool_env(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "true")
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    reset_llm()
    yield
    get_settings.cache_clear()
    reset_llm()


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_large_result_succeeds_and_materialises_all_rows(tool_env):
    res = execute_tool("big1", "freeform", {"sql": BIG_QUERY}, "cap_csv")

    assert res.status == "SUCCESS", res.error
    out = res.output or {}
    assert out["row_count"] > 2000, "本用例必须真的超过上下文上限才有意义"

    csv_path = out.get("csv_path")
    assert csv_path, "大结果同样要物化 CSV（下游 python/迭代依赖它）"
    rows = _read_csv(csv_path)
    assert len(rows) == out["row_count"], (
        f"CSV 行数 {len(rows)} 必须等于真实行数 {out['row_count']}——"
        "上下文截断不能把落盘数据也截掉")
    assert "_truncated" not in (rows[0] if rows else {})


def test_context_rows_are_capped_but_marked(tool_env):
    res = execute_tool("big2", "freeform", {"sql": BIG_QUERY}, "cap_csv")
    rows = (res.output or {}).get("rows") or []

    # 上限 2000 + 一条哨兵（标记还有多少没进上下文）
    assert len(rows) == 2001, f"进上下文的行数应被截断，实际 {len(rows)}"
    assert rows[-1].get("_truncated"), "截断必须显式标记，不能静默少给"


def test_small_result_has_no_sentinel(tool_env):
    res = execute_tool("small1", "freeform",
                       {"sql": "SELECT sale_id FROM fact_sales LIMIT 5"}, "cap_csv")
    assert res.status == "SUCCESS", res.error
    rows = (res.output or {}).get("rows") or []
    assert len(rows) == 5
    assert all("_truncated" not in r for r in rows)
    assert len(_read_csv((res.output or {})["csv_path"])) == 5
