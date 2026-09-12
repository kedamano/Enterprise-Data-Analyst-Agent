#!/usr/bin/env python
"""SEMANTIC/01 离线语义入库：采集业务语义 → 写入知识库（幂等）。

```bash
python scripts/build_semantics.py            # 采集并入库
python scripts/build_semantics.py --dry-run  # 只打印，不写库
```

为什么是**离线脚本**而不是分析期间自动写：
分析请求全程只读（§22 `NO WRITE ACCESS`）。把写库放进请求路径等于"只读分析"在运行中改状态，
还会引入并发写与租户隔离问题。采集→注入走读路径（每次分析实时采集 + 缓存），
落库走这个脚本，两个约束都满足。

幂等：`source` 带内容哈希，重复执行不会堆重复分片。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.core.semantics import collect_semantics, describe_semantics  # noqa: E402


def build_text() -> tuple[str, str]:
    """返回 ``(语义文本, source 标识)``。"""
    sem = collect_semantics("", use_cache=False)
    text = describe_semantics(sem, max_dims=20, max_values=30)
    digest = hashlib.sha1(
        (get_settings().data_db_url + text).encode("utf-8")).hexdigest()[:12]
    return text, f"semantics:{digest}"


def main() -> int:
    ap = argparse.ArgumentParser(description="采集业务语义并写入知识库")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--tenant", default=None, help="多租户下的租户标识")
    args = ap.parse_args()

    text, source = build_text()
    if not text:
        print("采集为空（无 dim_* 维表或数据源不可用）——未写入任何内容。")
        return 1

    print(text)
    print()
    if args.dry_run:
        print(f"[dry-run] source={source}")
        return 0

    from app.core.tools.knowledge_tool import get_store

    try:
        n = get_store().add(text, source=source, tenant=args.tenant)
    except TypeError:  # 兼容不带 tenant 参数的实现
        n = get_store().add(text, source=source)
    print(f"已写入知识库：source={source}，分片 {n} 条（幂等，可重复执行）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
