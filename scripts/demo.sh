#!/usr/bin/env bash
# 3 分钟演示 · 脚本化 dry-run（D18 产出）
#
# 用法：
#   MOCK_LLM=true ./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000   # 另开一个终端
#   ./scripts/demo.sh [BASE_URL]        # 默认 http://127.0.0.1:8000
#
# 为什么不用 `curl -d '{"query":"中文..."}'`：
#   Windows Git Bash 下会把中文参数按本地代码页传给原生 curl，服务端报
#   `{"detail":"There was an error parsing the body"}`（管道根本没跑）。
#   本脚本统一把请求体写成 UTF-8 文件再 `--data-binary @file`，跨平台稳定。
set -u

BASE="${1:-http://127.0.0.1:8000}"
API="$BASE/api/v1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BODY="$ROOT/.demo_body.json"
TIMEOUT="${DEMO_TIMEOUT:-300}"

ask() {  # ask <session_id> <query> [额外 JSON 字段，如 '"force_full_rerun":true']
  local sid="$1" q="$2" extra="${3:-}"
  printf '{"query":"%s","session_id":"%s"%s}' "$q" "$sid" "${extra:+,$extra}" > "$BODY"
  curl -s -m "$TIMEOUT" -X POST "$API/chat/analyze" \
    -H 'content-type: application/json' --data-binary @"$BODY"
}

hr() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

hr "健康检查"
curl -s -m 10 "$API/health" | jq -c .

hr "① E1 溯源：数值 → sql_id → SQL"
ask demo "分析最近营收变化的原因，按地区维度下钻" \
  | jq -c '{status, mode, findings: (.findings | length)}'
curl -s -m 30 "$API/chat/analyze/trace/demo" \
  | jq -c '{coverage, first_claim: .claims[0].evidence[0].sql_id}'

hr "② E2 写码：自由 SQL / 自由 Python + 产物"
ask demo_sql "只要SQL：给出各渠道营收趋势的语句" \
  | jq -r '.report' | head -12
ask demo_py "帮我写个 Python 脚本分析各区域营收" \
  | jq -c '{mode, has_code_block: (.report | contains("```python"))}'
curl -s -m 30 "$API/chat/analyze/artifacts/demo_py" | jq -c '.artifacts | length'

hr "③ E3 迭代：只跑目标阶段 / 守卫回退 / 显式开关"
echo "-- 首轮：全链 --"
ask demo_it "看看各区域营收" \
  | jq -c '{mode, tools: [.tool_results[].tool]}'
echo "-- 下钻：只跑目标阶段 --"
ask demo_it "基于上一结果，下钻到区域看营收" \
  | jq -c '{mode, kind: .iteration.kind, stages: .iteration.stages, tools: [.tool_results[].tool]}'
echo "-- 改期（期间内）：只跑目标阶段 --"
ask demo_it "基于上一结果，改为 2024年3月 的营收" \
  | jq -c '{mode, kind: .iteration.kind}'
echo "-- 改期（越界）：守卫拦下 → 回退全链 --"
ask demo_it "基于上一结果，改为 2025年1月 的营收" \
  | jq -c '{mode, iteration}'
echo "-- 显式开关：强制全链 --"
ask demo_it "基于上一结果，只看 region 1" '"force_full_rerun":true' \
  | jq -c '{mode, iteration}'

rm -f "$BODY"
printf '\n演示 dry-run 结束。\n'
