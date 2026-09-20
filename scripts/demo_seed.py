"""演示数据 seed 脚本：

1. 在本地生成示例工作区（mini SQLite 数据集 + 示例 job + 预算快照）。
2. 让 README / PPT 上的「一键本地跑通」有具体可执行路径。

用法：
    conda run -n base python scripts/demo_seed.py --workspace ./demo_ws
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def seed_sales_db(path: Path) -> None:
    """写一个小 sales 库作为 analysts 的 demo 数据源。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            region TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            qty INTEGER NOT NULL,
            ordered_at TEXT NOT NULL
        );
        """
    )
    rows = [
        ("North", "Electronics", 1299.00, 1, "2026-08-15"),
        ("North", "Electronics", 899.50, 2, "2026-08-16"),
        ("South", "Apparel", 199.90, 5, "2026-08-17"),
        ("South", "Electronics", 2499.00, 1, "2026-08-18"),
        ("East", "Home", 399.00, 3, "2026-08-19"),
        ("East", "Electronics", 1599.00, 1, "2026-08-20"),
        ("West", "Apparel", 149.50, 4, "2026-08-21"),
        ("West", "Home", 799.00, 2, "2026-08-22"),
        ("North", "Apparel", 259.80, 6, "2026-08-23"),
        ("South", "Home", 599.50, 2, "2026-08-24"),
    ]
    conn.executemany(
        "INSERT INTO orders (region, category, amount, qty, ordered_at) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def seed_demo_jobs(workspace: Path) -> None:
    """在 <workspace>/data/jobs/jobs.db 里写入 2 个 demo job。"""
    try:
        from app.infrastructure.jobs import init_db_path, get_scheduler
        import app.infrastructure.jobs.jobs as jm
        jm._instance = None
    except ImportError as exc:
        print(f"[warn] jobs 模块导入跳过（{exc}）")
        return
    db_path = workspace / "data" / "jobs" / "jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db_path(str(db_path))
    s = get_scheduler(run_fn=lambda sid, q: f"[MOCK report for: {q[:40]}]")
    s.add_job("alice", "demo-tenant", "daily-sales",
              "按 region 汇总 {{today}} 的订单金额与销量",
              schedule="daily@08:00")
    s.add_job("alice", "demo-tenant", "monthly-close",
              "月度经营回顾：{{last_month}} 对比 MoM",
              schedule="monthly@01@09:00")
    print(f"  jobs 写入 {db_path}（2 demo jobs）")


def write_env_example(workspace: Path) -> None:
    """根据 demo 数据源写一个 .env.demo。"""
    env = workspace / ".env.demo"
    env.write_text(
        "# 复制为 .env 即可一键跑 demo\n"
        f"DEMO_WORKSPACE={workspace.resolve()}\n"
        "EDAA_SQLITE_PATH=demo_ws/data/orders.db\n"
        "EDAA_LLM=mock\n"
        "# EDAA_LLM_API_KEY=sk-...\n",
        encoding="utf-8",
    )
    print(f"  env: {env}")


def main():
    parser = argparse.ArgumentParser(description="Seed demo workspace")
    parser.add_argument("--workspace", default="./demo_ws", help="demo 工作区根目录")
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    print(f"[1/3] 生成 demo 数据源: {ws / 'data/orders.db'}")
    seed_sales_db(ws / "data/orders.db")
    print(f"[2/3] 写入 demo jobs")
    seed_demo_jobs(ws)
    print(f"[3/3] 写入 .env.demo")
    write_env_example(ws)
    print("\nDone. 启动服务：")
    print(f"  conda run -n base python -m uvicorn app.main:app --host 0.0.0.0 --port 8000")
    print(f"# 前端：")
    print(f"  cd web && cp .env.demo .env.local && npm install && npm run dev")


if __name__ == "__main__":
    main()
