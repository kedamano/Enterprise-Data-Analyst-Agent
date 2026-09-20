#!/usr/bin/env bash
# ============================================================
# backup-pg.sh — PostgreSQL 备份脚本
# 使用方式:
#   1) 手动:    ./deploy/scripts/backup-pg.sh
#   2) CronJob: 部署 manifests 中的 PostgresBackup CronJob
# ============================================================
set -euo pipefail

NS="${NAMESPACE:-edaa}"
PG_POD="${PG_POD:-postgres-0}"
PG_USER="${PG_USER:-edaa_user}"
PG_DB="${PG_DB:-edaa}"
BACKUP_DIR="${BACKUP_DIR:-/tmp/edaa-backups}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="edaa_${TIMESTAMP}.sql.gz"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILE}"

mkdir -p "${BACKUP_DIR}"

echo "==> [备份开始] ${BACKUP_PATH}"

kubectl exec -n "${NS}" "${PG_POD}" -- \
  pg_dump -U "${PG_USER}" -d "${PG_DB}" --clean --if-exists \
  | gzip > "${BACKUP_PATH}"

SIZE="$(du -h "${BACKUP_PATH}" | cut -f1)"
echo "==> [备份完成] 大小: ${SIZE}"

# 清理过期备份
echo "==> [清理] 删除 ${RETENTION_DAYS} 天前的备份"
find "${BACKUP_DIR}" -name "edaa_*.sql.gz" -mtime +"${RETENTION_DAYS}" -delete

echo "==> 当前备份列表:"
ls -lhS "${BACKUP_DIR}"
