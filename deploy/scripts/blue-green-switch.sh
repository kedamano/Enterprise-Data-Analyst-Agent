#!/usr/bin/env bash
# ============================================================
# blue-green-switch.sh — 蓝绿部署切换脚本
# 用法: ./deploy/scripts/blue-green-switch.sh <blue|green> <image-tag>
#
# 示例:
#   ./scripts/blue-green-switch.sh blue  v2026.01.15-abc1234
#   ./scripts/blue-green-switch.sh green v2026.01.16-def5678
# ============================================================
set -euo pipefail

NS="edaa"
DEPLOYMENT="edaa-backend"
DOCKER_REPO="edaa/backend"

usage() {
  echo "用法: $0 <blue|green> <image-tag>"
  echo " 示例: $0 blue v2026.01.15-abc1234"
  exit 1
}

[[ $# -ne 2 ]] && usage

COLOR="${1}"
TAG="${2}"

if [[ "${COLOR}" != "blue" && "${COLOR}" != "green" ]]; then
  echo "错误: 颜色必须是 blue 或 green，收到: ${COLOR}"
  usage
fi

TARGET_IMAGE="${DOCKER_REPO}:${TAG}"

echo "==> 蓝绿切换: version=${COLOR}  image=${TARGET_IMAGE}"

echo "==> [1/3] 切换镜像 + label"
kubectl set image -n "${NS}" deployment/"${DEPLOYMENT}" \
  backend="${TARGET_IMAGE}" \
  -l version="${COLOR}"

echo "==> [2/3] 更新 version label"
kubectl label -n "${NS}" deployment "${DEPLOYMENT}" \
  version="${COLOR}" --overwrite

echo "==> [3/3] 等待 rollout 完成"
if kubectl rollout status -n "${NS}" deployment/"${DEPLOYMENT}" --timeout=300s; then
  echo ""
  echo "==> ✅ 切换完成! 当前活动版本: ${COLOR}"
  echo "    Image: ${TARGET_IMAGE}"
  echo "    Pods:"
  kubectl get pods -n "${NS}" -l "app=edaa,component=backend,version=${COLOR}" \
    -o wide --show-labels
else
  echo ""
  echo "==> ❌ Rollout 失败，执行回滚..."
  kubectl rollout undo -n "${NS}" deployment/"${DEPLOYMENT}"
  kubectl rollout status -n "${NS}" deployment/"${DEPLOYMENT}" --timeout=180s || true
  echo "==> 已回滚到上一版本"
  exit 1
fi
