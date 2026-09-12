#!/usr/bin/env python
"""CONC/01 并发实测：P50/P95 延迟、吞吐、成功率（不是估算）。

```bash
python scripts/bench_concurrency.py --concurrency 8 --requests 40
python scripts/bench_concurrency.py --concurrency 8 --requests 40 --workers 1   # 关工具并行对比
python scripts/bench_concurrency.py --json
```

用 TestClient + 线程池打 `/chat/analyze`（mock 模式，确定性、不花钱）：
- 每线程独立 client（避免共享状态干扰测量）
- 报告 P50/P95/P99、吞吐、成功率、缓存命中率
- `--workers` 覆盖 `PARALLEL_EXECUTOR_WORKERS`，用来量化"工具并行"到底值多少

**结论只认实测数字**；本机单进程数字不等于生产容量。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _one(client, session_id: str, query: str) -> dict:
    t0 = time.perf_counter()
    try:
        r = client.post("/api/v1/chat/analyze",
                        json={"query": query, "session_id": session_id})
        ms = (time.perf_counter() - t0) * 1000
        body = r.json() if r.status_code == 200 else {}
        return {"ms": ms, "code": r.status_code, "status": body.get("status"),
                "cache_hit": bool(body.get("cache_hit"))}
    except Exception as exc:  # 并发下要把异常也计入，而不是让线程静默死掉
        return {"ms": (time.perf_counter() - t0) * 1000, "code": 0, "error": str(exc)[:120]}


def main() -> int:
    ap = argparse.ArgumentParser(description="并发实测")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--requests", type=int, default=40)
    ap.add_argument("--workers", type=int, default=0,
                    help="覆盖 PARALLEL_EXECUTOR_WORKERS（0=用配置默认）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    os.environ["MOCK_LLM"] = "true"          # 实测并发，不烧真实 token
    os.environ.setdefault("REDIS_URL", "")
    if args.workers:
        os.environ["PARALLEL_EXECUTOR_WORKERS"] = str(args.workers)
    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient

    from app.main import app

    # 预置：一半请求重复问（测缓存命中），一半是新问题
    queries = ["分析各区域营收", "分析各渠道订单量"]
    results: list[dict] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        client = TestClient(app)             # 每线程独立 client
        sid = f"bench_c{idx % args.concurrency}"     # 同并发槽复用会话（触发缓存）
        for i in range(args.requests // args.concurrency):
            out = _one(client, sid, queries[i % len(queries)])
            with lock:
                results.append(out)

    threads = []
    t0 = time.perf_counter()
    for i in range(args.concurrency):
        th = threading.Thread(target=worker, args=(i,))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    wall = time.perf_counter() - t0

    ok = [r for r in results if r["code"] == 200 and r.get("status") in ("FINISH", "CLARIFY")]
    lat = sorted(r["ms"] for r in results)
    cache_hits = sum(1 for r in results if r.get("cache_hit"))

    def pct(p: float) -> float:
        if not lat:
            return 0.0
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1)

    report = {
        "concurrency": args.concurrency,
        "requests": len(results),
        "workers": args.workers or "default",
        "wall_s": round(wall, 2),
        "throughput_rps": round(len(results) / wall, 2) if wall else 0,
        "success_rate": round(len(ok) / len(results), 4) if results else 0,
        "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
        "cache_hit_rate": round(cache_hits / len(results), 4) if results else 0,
        "errors": [r.get("error") for r in results if r.get("error")][:3],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"\n# 并发实测（并发 {report['concurrency']} · 请求 {report['requests']} "
          f"· 工具并行 workers={report['workers']}）\n")
    print(f"- 总耗时：**{report['wall_s']}s** · 吞吐 **{report['throughput_rps']} req/s**")
    print(f"- 成功率：**{report['success_rate']}** · 缓存命中率：{report['cache_hit_rate']}")
    print(f"- 延迟：**P50 {report['p50_ms']}ms · P95 {report['p95_ms']}ms · P99 {report['p99_ms']}ms**")
    if report["errors"]:
        print(f"- ⚠ 错误样例：{report['errors']}")
    print("\n> 本机单进程 TestClient 数字；生产容量需在目标部署形态下复测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
