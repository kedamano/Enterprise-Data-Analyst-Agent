"""`dataset_profile` 规模瓶颈：**每列一条查询** → N 次全表扫描。

实测（`metrics.md`，1M 行 × 9 列）：
SQLite 2.87s · PostgreSQL 3.68s · **MySQL 12.57s**，且随**列数**线性恶化
（外推 100 列 ≈ 22s）。它是唯一"随列数线性"的基元。

原实现（`profile_tool.run`）：
```python
for c in columns:                      # N 列
    conn.execute(f"SELECT COUNT(c), COUNT(DISTINCT c) FROM {source}")   # N 次全表扫描
```

修法：**按批合并聚合**——把同一批列的 `COUNT/COUNT(DISTINCT)` 合并进**一条** SQL：

```sql
SELECT COUNT(c1), COUNT(DISTINCT c1), COUNT(c2), COUNT(DISTINCT c2) FROM src
```

- N 列 → `ceil(N/batch)` 次扫描（batch 默认 16，可配）；
- **结果完全等价**：不是抽样、不是近似去重、不改语义——
  所以不需要在输出里标 `sampled`，也不会把"非唯一列在抽样下看着唯一"这种假信号带进 `key_uniqueness`。

> 刻意**不采用** `metrics.md` 提的"抽样 / 近似去重"：`key_uniqueness` 的判据是
> `distinct == row_count`，抽样会让**非唯一列误判为唯一**——正好撞上本项目
> "唯一是弱信号"那条教训。批量化既拿性能又不引入假信号。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.tools import profile_tool


class _CountingConn:
    """包一层连接，记录发出的 SQL 条数（只看列统计用的聚合查询）。"""

    def __init__(self, conn):
        self._conn = conn
        self.agg_sql: list[str] = []

    def execute(self, stmt, *a, **kw):
        s = str(stmt)
        if "COUNT(DISTINCT" in s:
            self.agg_sql.append(s)
        return self._conn.execute(stmt, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def wide_db(tmp_path) -> Path:
    """20 列 × 200 行的小表——用来验证"批量化后结果不变"。"""
    p = tmp_path / "wide.db"
    conn = sqlite3.connect(p)
    cols = ", ".join(f"c{i} INTEGER" for i in range(20))
    conn.execute(f"CREATE TABLE wide (pk INTEGER PRIMARY KEY, {cols})")
    conn.executemany(
        "INSERT INTO wide (pk, c0, c1) VALUES (?, ?, ?)",
        [(i, i % 5, None if i % 3 == 0 else i) for i in range(200)])
    conn.commit()
    conn.close()
    return p


def _stats(engine, columns, batch):
    from sqlalchemy import text

    with engine.connect() as conn:
        counting = _CountingConn(conn)
        out = profile_tool._column_stats(counting, text, '"wide"', list(columns), batch)
        return out, counting.agg_sql


def _engine(p: Path):
    from sqlalchemy import create_engine

    return create_engine(f"sqlite:///{p}")


# --------------------------------------------------------------------------- #
# 一、核心：查询次数必须随 batch 收敛，而不是随列数线性
# --------------------------------------------------------------------------- #
def test_query_count_is_batched_not_per_column(wide_db):
    engine = _engine(wide_db)
    cols = ["pk"] + [f"c{i}" for i in range(20)]
    _, sqls = _stats(engine, cols, batch=8)
    # 21 列 / 每批 8 → 3 条；而不是 21 条
    assert len(sqls) == 3, f"应为 3 条批查询，实际 {len(sqls)} 条"


def test_batching_is_ceil_division(wide_db):
    engine = _engine(wide_db)
    cols = [f"c{i}" for i in range(20)]
    for batch, expected in ((20, 1), (7, 3), (1, 20)):
        _, sqls = _stats(engine, cols, batch=batch)
        assert len(sqls) == expected, f"batch={batch} 应 {expected} 条，实际 {len(sqls)}"


# --------------------------------------------------------------------------- #
# 二、结果必须与"每列单独算"完全一致（批量化不是近似）
# --------------------------------------------------------------------------- #
def test_results_match_per_column_computation(wide_db):
    engine = _engine(wide_db)
    cols = ["pk"] + [f"c{i}" for i in range(20)]

    batched, _ = _stats(engine, cols, batch=8)
    one_by_one, _ = _stats(engine, cols, batch=1)
    assert batched == one_by_one


def test_values_are_correct(wide_db):
    engine = _engine(wide_db)
    out, _ = _stats(engine, ["pk", "c0", "c1"], batch=16)
    # pk: 200 行全唯一、无 null
    assert out["pk"] == {"null_count": 0, "null_ratio": 0.0, "distinct": 200}
    # c0: 值取 i%5 → 5 个不同值，无 null
    assert out["c0"]["distinct"] == 5 and out["c0"]["null_count"] == 0
    # c1: i%3==0 时为 NULL → 67 个 null（0,3,...,198 共 67 个）
    assert out["c1"]["null_count"] == 67
    assert out["c1"]["null_ratio"] == round(67 / 200, 4)


def test_empty_column_list_issues_no_query(wide_db):
    engine = _engine(wide_db)
    out, sqls = _stats(engine, [], batch=8)
    assert out == {} and sqls == []


def test_batch_size_zero_or_negative_falls_back_to_one():
    """配置写坏（0/负数）不能除零崩掉，退化为逐列（仍然正确）。"""
    assert profile_tool._normalize_batch(0) >= 1
    assert profile_tool._normalize_batch(-5) >= 1
    assert profile_tool._normalize_batch(None) >= 1
