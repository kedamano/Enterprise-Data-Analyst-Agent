#!/usr/bin/env bash
# ============================================================
# kind-bootstrap.sh — 一键创建本地 Kind 集群 + 基础设施
# 用法: ./deploy/scripts/kind-bootstrap.sh
# ============================================================
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-edaa-local}"
K8S_VERSION="${K8S_VERSION:-v1.30.0}"

echo "==> [1/5] 创建 Kind 集群: ${CLUSTER_NAME}"
cat <<EOF | kind create cluster --name "${CLUSTER_NAME}" --config=-
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 80
        protocol: TCP
      - containerPort: 443
        hostPort: 443
        protocol: TCP
EOF

echo "==> [2/5] 安装 Ingress-NGINX"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s

echo "==> [3/5] 安装 cert-manager"
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.0/cert-manager.yaml
kubectl wait --namespace cert-manager \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/instance=cert-manager \
  --timeout=120s

echo "==> [4/5] 安装 kube-prometheus-stack (Prometheus + Grafana)"
if ! helm repo list 2>/dev/null | grep -q prometheus-community; then
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
  helm repo update
fi
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --wait --timeout=300s

echo "==> [5/5] 部署 edaa 应用"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
kubectl apply -f "${SCRIPT_DIR}/../k8s/namespace.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/configmap.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/postgres.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/redis.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/backend.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/frontend.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/ingress.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/networkpolicy.yaml"
kubectl apply -f "${SCRIPT_DIR}/../k8s/servicemonitor.yaml"

# 自签 CA 用于本地开发（替代 Let's Encrypt）
echo "==> 安装本地自签 ClusterIssuer"
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: edaa-local-tls
  namespace: edaa
spec:
  secretName: edaa-tls
  issuerRef:
    name: selfsigned
    kind: ClusterIssuer
  dnsNames:
    - edaa.localhost
    - localhost
EOF

echo ""
echo "========================================"
echo " 集群就绪！访问方式:"
echo "  kubectl port-forward -n edaa svc/edaa-frontend 8080:80"
echo "  open http://localhost:8080"
echo "  或配置 /etc/hosts: 127.0.0.1 edaa.localhost"
echo "========================================"
