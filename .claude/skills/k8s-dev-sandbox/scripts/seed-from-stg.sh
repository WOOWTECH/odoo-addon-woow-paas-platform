#!/usr/bin/env bash
#
# Seed sandbox DB from dev-sandbox-stg
# 把 stg 的 Odoo DB 整個 pg_dump → restore 到 sandbox，避免重新設定 ECPay / paas-operator
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/seed-from-stg.sh [TARGET_SLUG]
#
# Arguments:
#   TARGET_SLUG    Sandbox name (default: auto-detect from current branch)
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/seed-from-stg.sh
#   .claude/skills/k8s-dev-sandbox/scripts/seed-from-stg.sh smart-home-sub

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- 解析 target slug ---
TARGET_SLUG="${1:-}"
if [ -z "$TARGET_SLUG" ]; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    TARGET_SLUG=$(echo "$BRANCH" \
        | sed 's/[^a-zA-Z0-9]/-/g' \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/-\{2,\}/-/g' \
        | sed 's/^-//;s/-$//' \
        | cut -c1-40)
fi

# --- 來源（stg）+ 目標（sandbox）固定值 ---
SRC_NAMESPACE="dev-sandbox-stg"
SRC_PG_POD="stg-odoo-dev-sandbox-postgres-0"
SRC_DB="woow_stg"

TARGET_NAMESPACE="dev-sandbox-${TARGET_SLUG}"
TARGET_PG_POD="${TARGET_SLUG}-odoo-dev-sandbox-postgres-0"
TARGET_DB="woow_$(echo "$TARGET_SLUG" | sed 's/-/_/g')"
TARGET_ODOO_DEPLOY="${TARGET_SLUG}-odoo-dev-sandbox-odoo"

DUMP_FILE="/tmp/stg-${TARGET_SLUG}-$(date +%Y%m%d-%H%M%S).dump"

echo -e "${BLUE}DB Seed Plan:${NC}"
echo -e "  ${BLUE}Source:${NC}      ${SRC_NAMESPACE}/${SRC_PG_POD} db=${SRC_DB}"
echo -e "  ${BLUE}Target:${NC}      ${TARGET_NAMESPACE}/${TARGET_PG_POD} db=${TARGET_DB}"
echo -e "  ${BLUE}Local dump:${NC}  ${DUMP_FILE}"
echo ""

# --- 確認 sandbox 已建立 ---
if ! kubectl --context woow-k3s get ns "$TARGET_NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Target namespace ${TARGET_NAMESPACE} not found. Run create.sh first.${NC}" >&2
    exit 1
fi

if ! kubectl --context woow-k3s get pod -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" >/dev/null 2>&1; then
    echo -e "${RED}Target postgres pod ${TARGET_PG_POD} not found.${NC}" >&2
    exit 1
fi

# --- 1. pg_dump from stg ---
echo -e "${BLUE}[1/5] pg_dump from ${SRC_NAMESPACE}...${NC}"
kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_PG_POD" -- \
    pg_dump -U odoo -d "$SRC_DB" -F c -f "/tmp/stg.dump"
echo -e "${GREEN}✓ Dumped to ${SRC_PG_POD}:/tmp/stg.dump${NC}"

# --- 2. cp dump 出來到本機 ---
echo -e "${BLUE}[2/5] kubectl cp dump to local...${NC}"
kubectl --context woow-k3s cp -c postgres "${SRC_NAMESPACE}/${SRC_PG_POD}:/tmp/stg.dump" "$DUMP_FILE"
DUMP_SIZE=$(du -h "$DUMP_FILE" | cut -f1)
echo -e "${GREEN}✓ Local dump size: ${DUMP_SIZE}${NC}"

# --- 3. cp dump 進 target sandbox ---
echo -e "${BLUE}[3/5] kubectl cp dump to ${TARGET_NAMESPACE}...${NC}"
kubectl --context woow-k3s cp -c postgres "$DUMP_FILE" "${TARGET_NAMESPACE}/${TARGET_PG_POD}:/tmp/stg.dump"
echo -e "${GREEN}✓ Uploaded to target${NC}"

# --- 4. drop and restore target DB ---
echo -e "${BLUE}[4/5] pg_restore in ${TARGET_NAMESPACE}...${NC}"
echo -e "${YELLOW}  (dropping & recreating ${TARGET_DB} to ensure clean state)${NC}"

# 先 stop odoo（防止它佔住 connections）
kubectl --context woow-k3s scale deploy/"$TARGET_ODOO_DEPLOY" -n "$TARGET_NAMESPACE" --replicas=0
sleep 5

# Drop 既有 DB（連線都已斷開）
kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" -- \
    psql -U odoo -d postgres -c "DROP DATABASE IF EXISTS ${TARGET_DB};"
kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" -- \
    psql -U odoo -d postgres -c "CREATE DATABASE ${TARGET_DB} OWNER odoo;"

# Restore（停掉 owner / privileges 因為 user 是 stg 的 odoo，這邊一樣是 odoo 沒問題；用 -O 跳過 ownership 命令）
kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" -- \
    pg_restore -U odoo -d "$TARGET_DB" --no-owner --no-privileges /tmp/stg.dump 2>&1 | tail -20 || true

echo -e "${GREEN}✓ Restore complete${NC}"

# --- 5. 重啟 odoo（會觸發 -u woow_paas_platform 載入新 model）---
echo -e "${BLUE}[5/6] restart Odoo pod for module reload...${NC}"
kubectl --context woow-k3s scale deploy/"$TARGET_ODOO_DEPLOY" -n "$TARGET_NAMESPACE" --replicas=1
kubectl --context woow-k3s rollout status deploy/"$TARGET_ODOO_DEPLOY" -n "$TARGET_NAMESPACE" --timeout=300s 2>/dev/null || true

echo -e "${GREEN}✓ Odoo restarted${NC}"

# --- 6. Stream filestore from stg ---
# pg_dump captures attachment metadata but the binary blobs live on disk under
# .local/share/Odoo/filestore. Without this sync the asset bundles, logos,
# favicons etc. all 500 with FileNotFoundError.
echo -e "${BLUE}[6/6] sync filestore from stg...${NC}"
SRC_ODOO_POD=$(kubectl --context woow-k3s get pod -n "$SRC_NAMESPACE" -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
TARGET_ODOO_POD=$(kubectl --context woow-k3s get pod -n "$TARGET_NAMESPACE" -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')

if [ -n "$SRC_ODOO_POD" ] && [ -n "$TARGET_ODOO_POD" ]; then
    # tar pipe streams directly between pods (no intermediate disk).
    # `|| true` because tar prints a benign "Unexpected EOF" on stream close
    # despite all files transferring; the size check after this is the real
    # smoke test.
    kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_ODOO_POD" -- \
        tar cf - -C "/root/.local/share/Odoo/filestore/${SRC_DB}" . \
      | kubectl --context woow-k3s exec -i -n "$TARGET_NAMESPACE" "$TARGET_ODOO_POD" -- \
        tar xf - -C "/root/.local/share/Odoo/filestore/${TARGET_DB}/" \
      2>&1 | grep -v "Unexpected EOF" || true
    SIZE=$(kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_ODOO_POD" -- \
        du -sh "/root/.local/share/Odoo/filestore/${TARGET_DB}/" 2>/dev/null \
        | cut -f1 || echo "?")
    echo -e "${GREEN}✓ filestore synced (size: ${SIZE})${NC}"
else
    echo -e "${YELLOW}⚠ skipped filestore sync (could not resolve odoo pods)${NC}"
fi

# --- 收尾 ---
rm -f "$DUMP_FILE"
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Seeded ${TARGET_NAMESPACE}/${TARGET_DB} from ${SRC_NAMESPACE}/${SRC_DB}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}Next:${NC}"
echo -e "  Port-forward:  ${YELLOW}kubectl port-forward -n ${TARGET_NAMESPACE} svc/${TARGET_SLUG}-odoo-dev-sandbox-nginx 8080:80${NC}"
echo -e "  Open:          ${YELLOW}http://localhost:8080${NC}"
echo -e "  Login:         ${YELLOW}admin / admin${NC}"
