"""`dataset_profile` 宽表上界：**限定被画像的列数**。

为什么批量化不够（实测，1M 行 × 9 列）
--------------------------------------
`_column_stats` 把 N 条查询合并成 `ceil(N/batch)` 条，只省掉**重复扫表**：

| 后端 | batch=1（旧） | batch=16 | 提升 |
|---|---|---|---|
| SQLite | 1.55s | 1.33s | 1.17× |
| PostgreSQL | 2.06s | 1.52s | **1.35×** |

**没有 9×**——因为主要成本在每列 `COUNT(DISTINCT)` 的**去重本身**（每个 distinct
聚合各建一份临时结构），而不是表扫描次数。所以**列数因子依然线性**：
30 列的表仍要付 30 份去重代价，宽表照样秒级阻塞。

真正能把宽表成本**封顶**的是 `profile_max_columns`：
按**有用程度**排序后只画像前 N 列，其余显式记入 `columns_skipped`。

优先级（越前越先保）：**声明的键 > 声明的日期列 > 名似主键的列 > 名似日期的列 > 其余（保持库内顺序）**。
声明过的键/日期列**一定入选**——否则 `key_uniqueness` / `date_continuity` 会凭空失效。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.tools import profile_tool


@pytest.fixture
def wide_db(tmp_path):
    p = tmp_path / "wide.db"
    c = sqlite3.connect(p)
    cols = ", ".join(f"c{i} INTEGER" for i in range(2, 12))
    c.execute(f"CREATE TABLE wide (sale_id INTEGER PRIMARY KEY, c0 INT, c1 INT, "
              f"sale_date TEXT, {cols})")
    c.executemany("INSERT INTO wide VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  [(i, i, i % 5, f"2024-01-{i % 28 + 1:02d}",
                    *[i % 7 for _ in range(10)]) for i in range(100)])
    c.commit()
    c.close()
    return p


def _run(p, monkeypatch, **env):
    import app.config as C

    monkeypatch.setenv("DATA_DB_URL", f"sqlite:///{p}")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    C.get_settings.cache_clear()
    return profile_tool.run({"table": "wide"})


# --------------------------------------------------------------------------- #
# 一、优先级排序
# --------------------------------------------------------------------------- #
def test_prioritize_puts_declared_key_first():
    from app.core.tools.profile_tool import _prioritize_columns

    got = _prioritize_columns(["a", "b", "c", "d"], keys=["c"], date_column="")
    assert got[0] == "c", got


def test_prioritize_puts_declared_date_before_rest():
    from app.core.tools.profile_tool import _prioritize_columns

    got = _prioritize_columns(["a", "created_at", "b"], keys=[], date_column="created_at")
    assert got[0] == "created_at", got


def test_prioritize_prefers_id_like_and_date_like():
    from app.core.tools.profile_tool import _prioritize_columns

    got = _prioritize_columns(["name", "amt", "order_id", "sale_date"], keys=[], date_column="")
    assert got[0] == "order_id" and got[1] == "sale_date", got


def test_prioritize_keeps_original_order_within_groups():
    from app.core.tools.profile_tool import _prioritize_columns

    got = _prioritize_columns(["z1", "z2", "z3"], keys=[], date_column="")
    assert got == ["z1", "z2", "z3"]


def test_prioritize_never_drops_or_duplicates():
    from app.core.tools.profile_tool import _prioritize_columns

    src = ["a", "order_id", "b", "sale_date", "a"]
    got = _prioritize_columns(src, keys=["b"], date_column="sale_date")
    assert sorted(got) == sorted(set(src)), got


# --------------------------------------------------------------------------- #
# 二、上限生效 + 可见
# --------------------------------------------------------------------------- #
def test_cap_limits_profiled_columns(wide_db, monkeypatch):
    out = _run(wide_db, monkeypatch, PROFILE_MAX_COLUMNS=4)
    assert out["ok"], out
    assert len(out["columns"]) == 4
    assert len(out["columns_skipped"]) == out["columns_total"] - 4


def test_declared_key_survives_cap(wide_db, monkeypatch):
    """声明的键必须入选——否则 key_uniqueness 会凭空失效。"""
    out = _run(wide_db, monkeypatch, PROFILE_MAX_COLUMNS=2)
    assert "sale_id" in out["columns"], out["columns"]
    assert out["key_uniqueness"]["declared_key"] == ["sale_id"]


def test_declared_date_survives_cap(wide_db, monkeypatch):
    out = _run(wide_db, monkeypatch, PROFILE_MAX_COLUMNS=2)
    assert "sale_date" in out["columns"], out["columns"]


def test_zero_cap_means_no_cap(wide_db, monkeypatch):
    """0 = 不限制（向后兼容：默认行为不变）。"""
    out = _run(wide_db, monkeypatch, PROFILE_MAX_COLUMNS=0)
    assert out["ok"], out
    assert out["columns_skipped"] == []
    assert "c11" in out["columns"]


def test_skipped_columns_are_reported(wide_db, monkeypatch):
    """截断必须**可见**（否则"画像过了"会被误读成"全列都健康"）。"""
    out = _run(wide_db, monkeypatch, PROFILE_MAX_COLUMNS=3)
    assert out["columns_skipped"], "截断后必须报出未画像的列"
    assert all(c not in out["columns"] for c in out["columns_skipped"])
