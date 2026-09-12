#!/usr/bin/env bash
# E7/01 容器冒烟：build → run → 等健康 → 跑一次分析 → 记录镜像体积。
#
# 用法：./scripts/smoke_container.sh
# 退出码：0=通过  1=失败  2=**跳过**（docker 不可用）
#
# 「跳过」必须是可识别的独立状态：CI 里既不能当通过（假绿），也不能当失败（误报）。
set -u

IMAGE="${IMAGE:-da-analyst:prod}"
PORT="${PORT:-18000}"
NAME="da_smoke_$$"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIMEOUT_S="${SMOKE_TIMEOUT_S:-120}"

skip() { echo "[SKIP] $1"; exit 2; }
fail() { echo "[FAIL] $1"; exit 1; }

if [ "${SMOKE_FORCE_SKIP:-0}" = "1" ]; then
  skip "SMOKE_FORCE_SKIP=1（显式跳过容器冒烟，不计入通过）"
fi
command -v docker >/dev/null 2>&1 || skip "docker 不可用 —— 容器冒烟未执行（不计入通过）"
docker info >/dev/null 2>&1 || skip "docker daemon 未运行 —— 容器冒烟未执行（不计入通过）"

[ -d "$ROOT/web/dist" ] || fail "缺少 web/dist：请先执行 (cd web && npm run build)"

echo "== 1/5 构建镜像 $IMAGE =="
docker build -f "$ROOT/Dockerfile.prod" -t "$IMAGE" "$ROOT" || fail "镜像构建失败"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== 2/5 启动容器（mock 模式，避免依赖真实 key）=="
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" \
  -e MOCK_LLM=true -e REDIS_URL= "$IMAGE" >/dev/null || fail "容器启动失败"

echo "== 3/5 等待健康（最多 ${TIMEOUT_S}s）=="
ok=0
for _ in $(seq 1 "$TIMEOUT_S"); do
  if curl -sf "http://127.0.0.1:${PORT}/api/v1/health" >/tmp/smoke_health.json 2>/dev/null; then
    ok=1; break
  fi
  sleep 1
done
[ "$ok" = "1" ] || { docker logs "$NAME" 2>&1 | tail -20; fail "健康检查超时"; }
echo "    health: $(cat /tmp/smoke_health.json | head -c 200)"

echo "== 4/5 跑一次分析（mock）=="
code=$(curl -s -o /tmp/smoke_analyze.json -w '%{http_code}' -X POST \
  "http://127.0.0.1:${PORT}/api/v1/chat/analyze" \
  -H 'content-type: application/json' \
  --data-binary '{"query":"analyze revenue by region","session_id":"smoke"}')
[ "$code" = "200" ] || { cat /tmp/smoke_analyze.json | head -c 300; fail "analyze 返回 $code"; }
# 纯 grep/sed 判断，不依赖宿主 python（容器外未必有）
if grep -q '"status":"FINISH"' /tmp/smoke_analyze.json; then
  status=FINISH
elif grep -q '"status":"CLARIFY"' /tmp/smoke_analyze.json; then
  status=CLARIFY
else
  cat /tmp/smoke_analyze.json | head -c 300
  fail "analyze 未产出报告（status 既非 FINISH 也非 CLARIFY）"
fi
echo "    分析状态: $status"

echo "== 5/5 镜像体积 =="
size=$(docker image inspect "$IMAGE" --format '{{.Size}}')
mb=$(( size / 1024 / 1024 ))
echo "    镜像体积: ${mb} MB（目标 <2048 MB）"
[ "$mb" -lt 2048 ] || echo "    [WARN] 超过 2GB 目标，请检查是否漏了多阶段/清理"

echo "[PASS] 容器冒烟通过"
exit 0
