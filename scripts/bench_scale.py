#!/usr/bin/env python
"""CONC/01 规模实测：真实行数下的工具耗时（不是估算）。

```bash
# 默认：临时 SQLite（独立临时库，不污染样例库）
python scripts/bench_scale.py --rows 2000000
python scripts/bench_scale.py --rows 200000 --json

# 真库（跨方言同口径对比；PG 用 COPY 快速装载）
python scripts/bench_scale.py --rows 1000000 --backend postgres
python scripts/bench_scale.py --rows 1000000 --backend mysql \
  --dsn 'mysql+pymysql://root:pw@127.0.0.1:3306/da_agent'
```

造一张 N 行的事实表（默认表名 ``bench_fact``，**跑前会 DROP IF EXISTS 同名表**），分别测：
- `sql_query`：全表聚合 / 明细 LIMIT / 分组聚合 / 维度下钻 JOIN
- `dataset_profile`：质量基元（逐列 COUNT(DISTINCT) —— 这是最容易随**列数**线性恶化的地方）
- 语义采集：维表取值（有界，应与行数无关）

输出 markdown 表（可直接贴进 perf 基线）。**结论只认实测数字。**
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SQLITE_BACKENDS = {"sqlite"}
_REAL_BACKENDS = {"postgres", "mysql"}

# backend → 规范 dialect（与 app.core.tools.datasource._guess_dialect 对齐：pg 是 "postgresql"）
_DIALECT_OF = {"postgres": "postgresql", "mysql": "mysql", "sqlite": "sqlite"}


def _row_batch(rows: int, size: int = 50_000):
    """按批产出事实表行（避免一次性把百万行堆在内存里）。"""
    for start in range(1, rows + 1, size):
        yield [
            (i, f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", (i % 20) + 1,
             (i % 50) + 1, (i % 5) + 1, float((i * 7919) % 100000) / 100.0,
             i % 30, i % 17, f"n{i % 1000}")
            for i in range(start, min(start + size, rows + 1))
        ]


def build_sqlite(rows: int, db_path: Path) -> float:
    t0 = time.perf_counter()
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE IF EXISTS bench_fact")
    con.execute("""
        CREATE TABLE bench_fact (
            sale_id INTEGER PRIMARY KEY, sale_date TEXT, region_id INTEGER,
            product_id INTEGER, channel_id INTEGER, revenue REAL,
            orders INTEGER, customers INTEGER, note TEXT
        )""")
    con.execute("DROP TABLE IF EXISTS dim_region")
    con.execute("CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT)")
    con.executemany("INSERT INTO dim_region VALUES (?,?)",
                    [(i, f"区域{i}") for i in range(1, 21)])
    for batch in _row_batch(rows):
        con.executemany("INSERT INTO bench_fact VALUES (?,?,?,?,?,?,?,?,?)", batch)
    con.execute("CREATE INDEX idx_region ON bench_fact(region_id)")
    con.commit()
    con.close()
    return round(time.perf_counter() - t0, 1)


def build_real(rows: int, dsn: str, dialect: str) -> float:
    """在真库上建表 + 装载（PG 用 COPY，其它用 SQLAlchemy 批量 insert）。"""
    from sqlalchemy import (Column, Float, Integer, MetaData, String, Table,
                            create_engine, text)

    engine = create_engine(dsn)
    t0 = time.perf_counter()
    meta = MetaData()
    fact = Table(
        "bench_fact", meta,
        Column("sale_id", Integer, primary_key=True),
        Column("sale_date", String(10)),
        Column("region_id", Integer),
        Column("product_id", Integer),
        Column("channel_id", Integer),
        Column("revenue", Float),
        Column("orders", Integer),
        Column("customers", Integer),
        Column("note", String(16)),
    )
    dim = Table(
        "dim_region", meta,
        Column("region_id", Integer, primary_key=True),
        Column("region_name", String(32)),
    )
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS bench_fact"))
        conn.execute(text("DROP TABLE IF EXISTS dim_region"))
    meta.create_all(engine)

    if dialect == "postgres":
        # COPY 是最快的装载路径（psycopg2 特有）；数据无 NULL/制表符/换行，文本格式安全。
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            cur.executemany("INSERT INTO dim_region VALUES (%s,%s)",
                            [(i, f"区域{i}") for i in range(1, 21)])
            buf = io.StringIO()
            for batch in _row_batch(rows):
                for r in batch:
                    buf.write("\t".join(str(x) for x in r) + "\n")
            buf.seek(0)
            cur.copy_expert("COPY bench_fact FROM STDIN", buf)
            raw.commit()
        finally:
            raw.close()
    else:
        # ⚠️ SQLAlchemy 2.0 的 executemany **只接受 dict 列表**（元组会抛
        # `ArgumentError: List argument must consist only of dictionaries`），
        # 与 1.x 的“位置参数列表”写法不兼容——PG 分支走 raw cursor 所以没暴露。
        _cols = ("sale_id", "sale_date", "region_id", "product_id", "channel_id",
                 "revenue", "orders", "customers", "note")

        def _as_dicts(batch):
            return [dict(zip(_cols, r)) for r in batch]

        with engine.begin() as conn:
            conn.execute(dim.insert(), [{"region_id": i, "region_name": f"区域{i}"}
                                        for i in range(1, 21)])
            for batch in _row_batch(rows):
                conn.execute(fact.insert(), _as_dicts(batch))

    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX idx_region ON bench_fact(region_id)"))
    engine.dispose()
    return round(time.perf_counter() - t0, 1)


def timeit(fn):
    t0 = time.perf_counter()
    res = fn()
    return round((time.perf_counter() - t0) * 1000, 1), res


def main() -> int:
    ap = argparse.ArgumentParser(description="规模实测（SQLite / 真库同口径）")
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--backend", choices=sorted(_SQLITE_BACKENDS | _REAL_BACKENDS),
                    default="sqlite", help="目标后端（默认 sqlite：临时独立库）")
    ap.add_argument("--dsn", default="", help="真库 DSN；不传则用 .env 的 POSTGRES_DSN")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    backend = args.backend
    if backend == "sqlite":
        tmp = Path(tempfile.mkdtemp(prefix="bench_scale_"))
        db = tmp / "bench.db"
        target_desc = str(db)
        print(f"造数中：{args.rows:,} 行 → {db}", file=sys.stderr)
        build_s = build_sqlite(args.rows, db)
        data_db_url, dialect = f"sqlite:///{db.as_posix()}", "sqlite"
    else:
        from app.config import get_settings

        dsn = args.dsn.strip() or (get_settings().postgres_dsn if backend == "postgres" else "")
        if not dsn:
            print(f"[错误] backend={backend} 需要 --dsn（或在 .env 配 POSTGRES_DSN）",
                  file=sys.stderr)
            return 2
        target_desc = dsn.split("@")[-1]
        print(f"造数中：{args.rows:,} 行 → {backend} {target_desc}", file=sys.stderr)
        build_s = build_real(args.rows, dsn, backend)
        data_db_url, dialect = dsn, _DIALECT_OF[backend]

    print(f"装载耗时：{build_s}s", file=sys.stderr)

    import os
    os.environ["DATA_DB_URL"] = data_db_url
    os.environ["DATA_DB_DIALECT"] = dialect
    os.environ.setdefault("MOCK_LLM", "true")
    os.environ.setdefault("REDIS_URL", "")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.core.tools import execute_tool

    cases = [
        ("全表聚合 COUNT/SUM", "sql_query",
         {"sql": "SELECT COUNT(*) AS n, SUM(revenue) AS s FROM bench_fact"}),
        ("分组聚合（20 组）", "sql_query",
         {"sql": "SELECT region_id, SUM(revenue) FROM bench_fact GROUP BY region_id"}),
        ("明细 LIMIT 100", "sql_query",
         {"sql": "SELECT * FROM bench_fact LIMIT 100"}),
        ("维度下钻 JOIN 维表", "sql_query",
         {"sql": "SELECT r.region_name, SUM(f.revenue) FROM bench_fact f "
                 "JOIN dim_region r ON f.region_id = r.region_id GROUP BY r.region_name"}),
        ("dataset_profile（9 列，逐列 COUNT DISTINCT）", "dataset_profile",
         {"table": "bench_fact"}),
    ]

    rows_out = []
    for name, tool, params in cases:
        ms, res = timeit(lambda t=tool, p=params: execute_tool("bench", t, p, "bench_sess"))
        rows_out.append({"case": name, "ms": ms, "status": res.status,
                         "rows": len((res.output or {}).get("rows") or []),
                         "error": (res.error or "")[:80]})

    sem_ms, _ = timeit(lambda: __import__(
        "app.core.semantics", fromlist=["collect_semantics"]).collect_semantics("bench_sem", use_cache=False))

    report = {"rows": args.rows, "backend": backend, "target": target_desc,
              "build_s": build_s, "cases": rows_out, "semantics_ms": sem_ms}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"\n# 规模实测（{args.rows:,} 行 · 9 列 · {backend}）\n")
    print("| 场景 | 耗时(ms) | 状态 | 返回行数 |")
    print("|---|---|---|---|")
    for r in rows_out:
        print(f"| {r['case']} | {r['ms']} | {r['status']} | {r['rows']} |")
    print(f"| 业务语义采集（维表取值，应与会话规模无关） | {sem_ms} | OK | - |")
    failed = [r for r in rows_out if r["status"] != "SUCCESS"]
    if failed:
        print("\n**失败明细（必须修，不能只报耗时）**")
        for r in failed:
            print(f"- {r['case']} → {r['error']}")
    print(f"\n> 装载（建表+写 {args.rows:,} 行+建索引）耗时 {build_s}s；"
          f"目标：{backend} {target_desc}")
    print(f"> 结论只认实测：以上为本机 {backend} 单机数字，非分布式/列存引擎基线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
