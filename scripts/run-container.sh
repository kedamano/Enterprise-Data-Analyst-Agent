#!/usr/bin/env bash
# Data Analyst Agent —— 最小生产运行（单容器）。
# 用法：bash scripts/run-container.sh [image_tag]
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-da-agent:latest}"

echo "==> 构建镜像（首次会安装依赖，torch/sentence-transformers 较大，耐心等待）"
docker build -t "$TAG" .

echo "==> 确保数据目录（卷挂载，持久化 artifacts/traces/knowledge 等）"
mkdir -p ./data

echo "==> 启动（把 .env 传给容器；没配 key 会走 Mock）"
docker rm -f da-agent >/dev/null 2>&1 || true
docker run -d --name da-agent \
  -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -e PYTHON_SANDBOX_MODE=subprocess \
  "$TAG"

echo "==> 健康检查"
sleep 3
curl -s http://127.0.0.1:8000/api/v1/health && echo
echo "==> 打开控制台：http://127.0.0.1:8000/ui"
echo "==> 其余端点：GET /api/v1/health · POST /api/v1/chat/analyze · POST /api/v1/chat/analyze/stream · POST /api/v1/documents/ingest · GET /api/v1/debug/traces"
