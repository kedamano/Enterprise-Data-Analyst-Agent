# EDAA Kubernetes 部署指南

> Enterprise Data Analyst Agent — 单集群多 Namespace 蓝绿部署

## 目录结构

```
deploy/
├── k8s/                        # Kubernetes 清单
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── secret.yaml.template
│   ├── postgres.yaml
│   ├── redis.yaml
│   ├── backend.yaml
│   ├── frontend.yaml
│   ├── ingress.yaml
│   ├── networkpolicy.yaml
│   └── servicemonitor.yaml
├── Dockerfile.backend          # FastAPI 多阶段构建
├── Dockerfile.frontend         # React + nginx 多阶段构建
├── nginx.conf                  # Frontend nginx 配置
├── .dockerignore
├── README.md
└── scripts/
    ├── kind-bootstrap.sh       # 本地 Kind 集群一键搭建
    ├── blue-green-switch.sh    # 蓝绿切换
    └── backup-pg.sh            # PostgreSQL 备份
```

---

## 快速开始（本地 Kind）

### 前置依赖

| 工具   | 版本     | 用途              |
|--------|----------|-------------------|
| Docker | >= 24    | 容器运行时        |
| kind   | >= 0.20  | 本地 K8s 集群     |
| kubectl | >= 1.28 | 集群管理          |
| Helm   | >= 3.12  | Prometheus 安装   |

### 一键 bootstrap

```bash
./deploy/scripts/kind-bootstrap.sh
```

脚本依次完成：
1. 创建 Kind 集群（含 ingress 端口映射 80/443）
2. 安装 Ingress-NGINX Controller
3. 安装 cert-manager
4. 安装 kube-prometheus-stack（Prometheus + Grafana）
5. 部署 EDAA 所有组件
6. 创建本地自签证书（`edaa.localhost`）

### 访问应用

```bash
# 端口转发
kubectl port-forward -n edaa svc/edaa-frontend 8080:80
open http://localhost:8080

# 或配置 hosts（推荐，可走真实 Ingress）
echo "127.0.0.1 edaa.localhost" | sudo tee -a /etc/hosts
open https://edaa.localhost
```

---

## 云环境部署（EKS / GKE / AKS）

### 前置准备

#### 1. IAM 与 IRSA（EKS）

```bash
# 创建 OIDC Provider
eksctl utils associate-iam-oidc-provider --cluster <cluster-name> --approve

# 创建 ServiceAccount 所需 IAM Role
eksctl create iamserviceaccount \
  --name edaa-secrets \
  --namespace edaa \
  --cluster <cluster-name> \
  --attach-policy-arn arn:aws:iam::<acct>:policy/edaa-secrets-policy \
  --approve
```

#### 2. Ingress-NGINX

```bash
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"="nlb"
```

#### 3. cert-manager

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.15.0/cert-manager.yaml
```

### 部署应用

```bash
# 1. 创建 namespace + config
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml

# 2. 处理 Secret（见下方安全方案）
kubeseal --format=yaml < deploy/k8s/secret.yaml.template > deploy/k8s/sealed-secret.yaml
kubectl apply -f deploy/k8s/sealed-secret.yaml

# 3. 部署有状态服务
kubectl apply -f deploy/k8s/postgres.yaml
kubectl apply -f deploy/k8s/redis.yaml

# 4. 部署应用
kubectl apply -f deploy/k8s/backend.yaml
kubectl apply -f deploy/k8s/frontend.yaml
kubectl apply -f deploy/k8s/ingress.yaml
kubectl apply -f deploy/k8s/networkpolicy.yaml
kubectl apply -f deploy/k8s/servicemonitor.yaml
```

### 更新域名

编辑 `deploy/k8s/ingress.yaml` 中 `host` 字段与 `deploy/k8s/configmap.yaml` 的 `DOMAIN`。

---

## Secret 管理方案（二选一）

### 方案 A：Sealed Secrets（Bitnami，推荐）

```bash
# 安装控制器
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.26.0/controller.yaml

# 安装 CLI
# brew install kubeseal  或  GitHub releases 下载

# 加密
kubeseal --format=yaml < deploy/k8s/secret.yaml.template > deploy/k8s/sealed-secret.yaml

# 提交 sealed-secret.yaml 到 Git（已加密，无法被集群外解密）
git add deploy/k8s/sealed-secret.yaml
git commit -m "chore: sealed secrets"

# 部署
kubectl apply -f deploy/k8s/sealed-secret.yaml
```

### 方案 B：External Secrets Operator + AWS Secrets Manager

```bash
# 安装 ESO
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace

# 创建 SecretStore + ExternalSecret
kubectl apply -f - <<EOF
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: edaa-secrets
  namespace: edaa
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: edaa-secrets
  data:
    - secretKey: SECRET_KEY
      remoteRef:
        key: edaa/production
        property: SECRET_KEY
    # ...其他条目
EOF
```

---

## 蓝绿部署

### 切换脚本

```bash
# 切到 blue 版本，使用 v2026.01.15-abc1234 镜像
./deploy/scripts/blue-green-switch.sh blue v2026.01.15-abc1234

# 切到 green 版本，使用 v2026.01.16-def5678 镜像
./deploy/scripts/blue-green-switch.sh green v2026.01.16-def5678
```

### 手动切换流程

```bash
# 当前 blue 活跃，部署 green 镜像
kubectl set image -n edaa deployment/edaa-backend \
  backend=edaa/backend:v2.0.0-green -l version=green
kubectl label -n edaa deployment edaa-backend version=green --overwrite
kubectl rollout status -n edaa deployment/edaa-backend --timeout=300s

# 验证通过后，确认切换
kubectl get pods -n edaa -l component=backend,version=green
```

---

## 回滚

```bash
# 快速回滚上一版本
kubectl rollout undo -n edaa deployment/edaa-backend
kubectl rollout undo -n edaa deployment/edaa-frontend

# 查看历史
kubectl rollout history -n edaa deployment/edaa-backend

# 回滚到指定版本
kubectl rollout undo -n edaa deployment/edaa-backend --to-revision=3
```

---

## 备份

### 手动备份

```bash
./deploy/scripts/backup-pg.sh
# 输出: /tmp/edaa-backups/edaa_20260115_143022.sql.gz
```

### CronJob 定时备份（可选）

将 `backup-pg.sh` 包装后通过 Kubernetes CronJob 每日 02:00 执行。

---

## 监控与告警

ServiceMonitor 自动抓取 backend 的 `/metrics` 端口 8000。

```bash
# 查看 ServiceMonitor
kubectl get servicemonitor -n edaa

# 访问 Grafana（需先配置 Ingress 或 port-forward）
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
open http://localhost:3000
```

---

## 故障排查

| 症状                          | 排查命令                                                  |
|-------------------------------|----------------------------------------------------------|
| Pod Pending                   | `kubectl describe pod <pod-name> -n edaa`                |
| ImagePullBackOff              | 检查镜像 tag 与仓库 secret                               |
| CrashLoopBackOff              | `kubectl logs <pod-name> -n edaa --previous`             |
| Service 不通                  | `kubectl get endpoints -n edaa`                          |
| Ingress 502                   | `kubectl logs -n ingress-nginx deployment/ingress-nginx-controller` |
| HPA 不扩容                    | `kubectl get hpa -n edaa` / `kubectl describe hpa`       |
| PVC Pending                   | `kubectl get pvc -n edaa` / 检查 storageClassName        |
