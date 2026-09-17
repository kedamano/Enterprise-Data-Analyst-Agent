#!/usr/bin/env bash
# 一键跑「不需要外网」的回归测试 —— CI 离线门的本地同体验。
#
# 覆盖：MOCK_LLM=true + AUTH_ENABLED=false + 无 REDIS/PG/MILVUS。
# 排出：real-llm 套（test_agent_real.py）+ 真实中间件（test_pg_live /
#        test_redis_live / test_milvus_live / test_mysql_live）。
#
# 同 CI 的关键参数：
#   - mock LLM → 依赖网络的部分命中本地 mock
#   - AUTH_ENABLED=false → 不触发 AuthCentre / AuthGate
#   - REDIS_DSN / POSTGRES_DSN / MILVUS_HOST 都为空 → 代码走降级路径
#
# 用法：
#   bash scripts/run_offline_tests.sh
#   bash scripts/run_offline_tests.sh -k "watermark"   # 跑单模块

set -euo pipefail
cd "$(dirname "$0")/.."

export MOCK_LLM=true
export AUTH_ENABLED=false
export REDIS_URL=
export REDIS_DSN=
export POSTGRES_DSN=
export MYSQL_DSN=
export MILVUS_HOST=
export MILVUS_LITE_PATH=

# POSIX 兼容：脚本是 bash，直接执行。
# shellcheck disable=SC2046
python -m pytest tests/ -q \
  --ignore=tests/test_agent_real.py \
  --ignore=tests/test_pg_live.py \
  --ignore=tests/test_redis_live.py \
  --ignore=tests/test_milvus_live.py \
  --ignore=tests/test_mysql_live.py \
  --ignore=tests/test_milvus_live.py \
  -x \
  "$@"
