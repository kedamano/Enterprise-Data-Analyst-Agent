"""``data/sample_analyst.db`` 演示数据集：**陷阱必须真的落在库里**。

背景（详见 ``docs/progress/pending-real.md`` §C.1）：`app/eval/golden.py` 里 7 条
``requires_real=True`` 用例是按一份业务数据集写的，而内置 ``sample_enterprise.db``
只有 ``fact_sales + 3 张维表``（字段仅 revenue/orders/customers）。
真 LLM 重跑时 5 题因**字段不存在**而 CLARIFY、2 题因无数据而使质量探测器不触发。

``scripts/generate_analyst_sample.py`` 造一个**超集**库补齐这些实体，并**刻意植入可判定的陷阱**。
本文件守两件事：

1. **陷阱真的有**（且是从**库内数据**算出来的，不是断言生成器的内部变量）；
2. **工具真的能在这个库上跑通**（`sql_query` / `dataset_profile` 不因新 schema 报错）。

数据集是确定性的（固定种子），所以这些断言在任何机器上都稳定。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from app.config import get_settings  # noqa: E402
from scripts import generate_analyst_sample as gen  # noqa: E402


@pytest.fixture(scope="module")
def analyst_db(tmp_path_factory) -> Path:
    """确定性生成到临时目录（不写进仓库）。"""
    db = tmp_path_factory.mktemp("analyst") / "sample_analyst.db"
    gen.main(str(db))
    return db


@pytest.fixture
def analyst_env(analyst_db, monkeypatch):
    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{analyst_db.as_posix()}")
    monkeypatch.setenv("DATA_DB_DIALECT", "sqlite")
    monkeypatch.setenv("MOCK_LLM", "true")
    get_settings.cache_clear()
    yield analyst_db
    get_settings.cache_clear()


def _q1(db: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, params).fetchone()


# --------------------------------------------------------------------------- #
# 1. schema 覆盖：7 条 requires_real 用例要的实体都在
# --------------------------------------------------------------------------- #
def test_schema_covers_all_required_entities(analyst_db):
    with sqlite3.connect(analyst_db) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    # 订单表（r_join_amplification_guard / GMV 同比）
    assert "fact_orders" in tables
    # 流量表 → 转化率的分母（r_ratio_denominator / r_simpson_check）
    assert "fact_traffic" in tables
    # 与既有样例库同构的星型事实表（既有 golden 也可复用）
    assert "fact_sales" in tables

    with sqlite3.connect(analyst_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fact_orders)")}
        prod_cols = {r[1] for r in conn.execute("PRAGMA table_info(dim_product)")}
    assert {"gmv", "order_date", "product_id"} <= cols, "订单表要有 GMV + 日期 + 商品键"
    assert "category" in prod_cols, "商品表要有品类（'各品类营收'）"


def test_eight_channels_for_multiple_comparison(analyst_db):
    """`r_multiple_comparison` 要"逐个比较 **8 个渠道**"。"""
    n = _q1(analyst_db, "SELECT COUNT(*) FROM dim_channel")[0]
    assert n == 8, f"渠道数应为 8，实际 {n}"
    assert len(gen.CHANNELS) == 8


# --------------------------------------------------------------------------- #
# 2. 陷阱一：辛普森悖论（整体升、分层降）—— 从**库内数据**算
# --------------------------------------------------------------------------- #
def _cvr(db: Path, where: str) -> float:
    v, c = _q1(db, f"SELECT SUM(visits), SUM(conversions) FROM fact_traffic WHERE {where}")
    return c / v * 100


def test_simpson_paradox_holds(analyst_db):
    y23, y24 = "substr(stat_date,1,4)='2023'", "substr(stat_date,1,4)='2024'"
    overall_23, overall_24 = _cvr(analyst_db, y23), _cvr(analyst_db, y24)

    # ① 整体**上升**，且贴近 golden 题干说的 "6% → 7%"
    assert overall_24 > overall_23, f"整体应从 6% 升到 7%，实际 {overall_23:.2f}% → {overall_24:.2f}%"
    assert 5.5 <= overall_23 <= 6.5, f"2023 整体转化率应约 6%，实际 {overall_23:.2f}%"
    assert 6.5 <= overall_24 <= 7.5, f"2024 整体转化率应约 7%，实际 {overall_24:.2f}%"

    # ② 但**每个分层都下降** → 这正是辛普森悖论，只看整体会得出相反结论
    hi = ",".join(str(c) for c in gen.HIGH_CVR_CHANNELS)
    lo = ",".join(str(c) for c in gen.LOW_CVR_CHANNELS)
    for label, ids in (("高转化渠道", hi), ("低转化渠道", lo)):
        before = _cvr(analyst_db, f"{y23} AND channel_id IN ({ids})")
        after = _cvr(analyst_db, f"{y24} AND channel_id IN ({ids})")
        assert after < before, f"{label} 必须下降（辛普森），实际 {before:.2f}% → {after:.2f}%"


def test_simpson_mix_shift_is_the_real_cause(analyst_db):
    """升的不是转化能力，而是**高转化渠道的流量占比** —— 数据集要能验证这一点。"""
    y23, y24 = "substr(stat_date,1,4)='2023'", "substr(stat_date,1,4)='2024'"
    hi = ",".join(str(c) for c in gen.HIGH_CVR_CHANNELS)

    def share(where: str) -> float:
        total = _q1(analyst_db, f"SELECT SUM(visits) FROM fact_traffic WHERE {where}")[0]
        hi_v = _q1(analyst_db, f"SELECT SUM(visits) FROM fact_traffic "
                              f"WHERE {where} AND channel_id IN ({hi})")[0]
        return hi_v / total

    assert share(y24) > share(y23), \
        f"高转化渠道流量占比应上升（混合结构漂移），实际 {share(y23):.2%} → {share(y24):.2%}"


# --------------------------------------------------------------------------- #
# 3. 陷阱二：2024-08 GMV 同比约 -12%，且**内部驱动分化**（不拆解就归错因）
# --------------------------------------------------------------------------- #
def test_august_gmv_yoy_matches_premise(analyst_db):
    cur = _q1(analyst_db, "SELECT SUM(gmv) FROM fact_orders WHERE substr(order_date,1,7)='2024-08'")[0]
    base = _q1(analyst_db, "SELECT SUM(gmv) FROM fact_orders WHERE substr(order_date,1,7)='2023-08'")[0]
    yoy = (cur - base) / base * 100
    assert -14 < yoy < -10, f"2024-08 GMV 同比应约 -12%，实际 {yoy:+.1f}%"


def test_august_gmv_has_divergent_category_drivers(analyst_db):
    """有涨有跌才算"需要拆解"；如果全跌就没有拆解价值，题目会退化。"""
    rows = sqlite3.connect(analyst_db).execute(
        "SELECT p.category, SUM(o.gmv) FROM fact_orders o JOIN dim_product p "
        "ON o.product_id = p.product_id WHERE substr(o.order_date,1,7)='2024-08' "
        "GROUP BY p.category").fetchall()
    before = dict(sqlite3.connect(analyst_db).execute(
        "SELECT p.category, SUM(o.gmv) FROM fact_orders o JOIN dim_product p "
        "ON o.product_id = p.product_id WHERE substr(o.order_date,1,7)='2023-08' "
        "GROUP BY p.category").fetchall())
    changes = {cat: (v - before[cat]) / before[cat] * 100 for cat, v in rows}

    assert any(c < -20 for c in changes.values()), f"应有明显下滑的品类，实际 {changes}"
    assert any(c > 5 for c in changes.values()), \
        f"应同时有增长的品类（否则无需拆解），实际 {changes}"


# --------------------------------------------------------------------------- #
# 4. 工具真的能在新 schema 上跑通（这是"数据缺口"被消除的直接证据）
# --------------------------------------------------------------------------- #
def test_tools_work_on_analyst_db(analyst_env):
    from app.core.tools import execute_tool

    # ① 订单表 JOIN 商品表统计各品类营收（r_join_amplification_guard 的主路径）
    res = execute_tool("t1", "sql_query", {
        "sql": "SELECT p.category, SUM(o.gmv) AS gmv FROM fact_orders o "
               "JOIN dim_product p ON o.product_id = p.product_id "
               "GROUP BY p.category ORDER BY gmv DESC"}, "analyst_sess")
    assert res.status == "SUCCESS", res.error
    rows = (res.output or {}).get("rows") or []
    assert len(rows) == 4, f"应返回 4 个品类，实际 {len(rows)}: {rows}"
    assert rows[0]["category"] and rows[0]["gmv"] > 0

    # ② 转化率可被真实查询（r_ratio_denominator / r_simpson_check 的数据前提）
    res = execute_tool("t2", "sql_query", {
        "sql": "SELECT substr(stat_date,1,4) AS y, SUM(conversions)*1.0/SUM(visits) AS cvr "
               "FROM fact_traffic GROUP BY y ORDER BY y"}, "analyst_sess")
    assert res.status == "SUCCESS", res.error
    rows = (res.output or {}).get("rows") or []
    # 行是 dict（sql_query 的输出契约），不是 tuple
    cvrs = [r["cvr"] for r in rows]
    assert len(cvrs) == 2 and cvrs[1] > cvrs[0], f"整体转化率应上升，实际 {cvrs}"

    # ③ dataset_profile 在新表上可用（跨方言修复 + 新 schema）
    res = execute_tool("t3", "dataset_profile", {"table": "fact_orders"}, "analyst_sess")
    assert res.status == "SUCCESS", res.error
    assert (res.output or {}).get("row_count", 0) > 0


def test_rows_and_dimensions_are_non_trivial(analyst_db):
    """数据量要够分析（太小的数据集会让结论不可信）。"""
    assert _q1(analyst_db, "SELECT COUNT(*) FROM fact_orders")[0] > 10_000
    assert _q1(analyst_db, "SELECT COUNT(*) FROM fact_traffic")[0] > 500
    assert _q1(analyst_db, "SELECT COUNT(DISTINCT category) FROM dim_product")[0] == 4
