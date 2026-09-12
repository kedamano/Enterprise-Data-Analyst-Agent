"""Generate a small but realistic SQLite sample enterprise database.

Creates ``data/sample_enterprise.db`` with a star-schema (dimensions + fact)
so the agent's schema_search / dataset_profile / sql_query tools have
something concrete to analyse. Idempotent – drops & recreates tables.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

DB = "data/sample_enterprise.db"


def main() -> None:
    import pathlib
    pathlib.Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.executescript(
        """
        DROP TABLE IF EXISTS fact_sales;
        DROP TABLE IF EXISTS dim_region;
        DROP TABLE IF EXISTS dim_product;
        DROP TABLE IF EXISTS dim_channel;

        CREATE TABLE dim_region (region_id INTEGER PRIMARY KEY, region_name TEXT, country TEXT);
        CREATE TABLE dim_product (product_id INTEGER PRIMARY KEY, product_name TEXT, category TEXT);
        CREATE TABLE dim_channel (channel_id INTEGER PRIMARY KEY, channel_name TEXT);

        CREATE TABLE fact_sales (
            sale_id INTEGER PRIMARY KEY,
            sale_date TEXT,
            region_id INTEGER,
            product_id INTEGER,
            channel_id INTEGER,
            revenue REAL,
            orders INTEGER,
            customers INTEGER
        );
        """
    )
    regions = [(1, "华东", "CN"), (2, "华北", "CN"), (3, "华南", "CN"), (4, "西部", "CN"), (5, "境外", "OVERSEAS")]
    products = [(1, "企业版SaaS", "Software"), (2, "硬件终端", "Hardware"),
                (3, "咨询服务", "Service"), (4, "培训", "Service")]
    channels = [(1, "直销"), (2, "合作伙伴"), (3, "线上")]
    cur.executemany("INSERT INTO dim_region VALUES (?,?,?)", regions)
    cur.executemany("INSERT INTO dim_product VALUES (?,?,?)", products)
    cur.executemany("INSERT INTO dim_channel VALUES (?,?)", channels)

    rng = random.Random(42)
    start = date(2024, 1, 1)
    rows = []
    sid = 1
    for week in range(52):
        d = start + timedelta(days=7 * week)
        for rid, _, _ in regions:
            for pid, _, _ in products:
                for cid, _ in channels:
                    # 境外(channel/region) slightly lower base to make a visible driver
                    base = 50000 if rid != 5 else 30000
                    rev = base * rng.uniform(0.7, 1.4)
                    if rid == 2 and week >= 30:  # 华北下半年下滑，用于根因演示
                        rev *= 0.6
                    rows.append((sid, d.isoformat(), rid, pid, cid, round(rev, 2),
                                 rng.randint(20, 200), rng.randint(10, 120)))
                    sid += 1
    cur.executemany(
        "INSERT INTO fact_sales VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()
    print(f"Sample DB created: {DB} ({len(rows)} fact rows)")


if __name__ == "__main__":
    main()
