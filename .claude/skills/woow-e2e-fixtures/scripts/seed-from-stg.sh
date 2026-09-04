#!/usr/bin/env bash
#
# Seed sandbox DB from dev-sandbox-stg
# 把 stg 的 Odoo DB 整個 pg_dump → restore 到 sandbox，避免重新設定 ECPay / paas-operator
#
# Usage:
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh [TARGET_SLUG]
#
# Arguments:
#   TARGET_SLUG    Sandbox name (default: auto-detect from current branch)
#
# Examples:
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh smart-home-sub

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
    echo -e "${RED}Target namespace ${TARGET_NAMESPACE} not found. Create it via /woow-dev:k8s-dev-sandbox create first.${NC}" >&2
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
# 源頭側的 size／sha256 是後面每一段搬運的對照組——沒有它，「本機檔＝本機檔」的
# 校驗零鑑別力（見 docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md）。
SRC_SIZE=$(kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_PG_POD" -- stat -c %s /tmp/stg.dump)
SRC_SHA=$(kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_PG_POD" -- sha256sum /tmp/stg.dump | cut -d' ' -f1)
echo -e "${GREEN}✓ Dumped to ${SRC_PG_POD}:/tmp/stg.dump (${SRC_SIZE} bytes, sha256 ${SRC_SHA:0:12}…)${NC}"

# --- 2. cp dump 出來到本機 ---
echo -e "${BLUE}[2/5] kubectl cp dump to local...${NC}"
kubectl --context woow-k3s cp -c postgres "${SRC_NAMESPACE}/${SRC_PG_POD}:/tmp/stg.dump" "$DUMP_FILE"
# 🔴 `kubectl cp`（＝exec stdout 串流）從 pod 拉 >~20 MB 會尾端截斷且 exit 0；截斷的 dump
# `pg_restore -l` 照樣列完整 TOC，restore 卻「部分成功」。這裡用源頭 size＋sha256 對照，
# 不相等就停，不要往下 drop target DB。
LOCAL_SIZE=$(stat -f %z "$DUMP_FILE" 2>/dev/null || stat -c %s "$DUMP_FILE")
LOCAL_SHA=$( (shasum -a 256 "$DUMP_FILE" 2>/dev/null || sha256sum "$DUMP_FILE") | cut -d' ' -f1)
if [ "$LOCAL_SIZE" != "$SRC_SIZE" ] || [ "$LOCAL_SHA" != "$SRC_SHA" ]; then
    echo -e "${RED}✗ Local dump does not match source: size ${LOCAL_SIZE} vs ${SRC_SIZE}, sha256 ${LOCAL_SHA:0:12}… vs ${SRC_SHA:0:12}…${NC}" >&2
    echo -e "${RED}  kubectl exec/cp stdout 串流截斷（已知坑：docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md）。${NC}" >&2
    echo -e "${RED}  重跑一次通常不會好；dump 超過 ~20 MB 請改叢集內 pod-to-pod 搬運（同 doc 的修法 1）。target 尚未動到。${NC}" >&2
    rm -f "$DUMP_FILE"
    exit 1
fi
echo -e "${GREEN}✓ Local dump verified: ${LOCAL_SIZE} bytes, sha256 matches source${NC}"

# --- 3. cp dump 進 target sandbox ---
echo -e "${BLUE}[3/5] kubectl cp dump to ${TARGET_NAMESPACE}...${NC}"
kubectl --context woow-k3s cp -c postgres "$DUMP_FILE" "${TARGET_NAMESPACE}/${TARGET_PG_POD}:/tmp/stg.dump"
# 上傳方向目前沒觀察到截斷，但便宜的校驗照做；`pg_restore -f /dev/null` 是全讀（-l 只讀檔頭），
# 這是「餵進 pg_restore 前」最後一道能證明 archive 完整的檢查——放在 drop DB 之前。
TARGET_SHA=$(kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" -- sha256sum /tmp/stg.dump | cut -d' ' -f1)
if [ "$TARGET_SHA" != "$SRC_SHA" ]; then
    echo -e "${RED}✗ Uploaded dump sha256 ${TARGET_SHA:0:12}… != source ${SRC_SHA:0:12}…; aborting before touching ${TARGET_DB}.${NC}" >&2
    exit 1
fi
if ! kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_PG_POD" -- pg_restore -f /dev/null /tmp/stg.dump; then
    echo -e "${RED}✗ pg_restore -f /dev/null failed on target: archive incomplete or unreadable; aborting before touching ${TARGET_DB}.${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ Uploaded to target and verified (sha256 match, full-read OK)${NC}"

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
    # 🔴 源頭這一半仍是 exec stdout 串流：>~20 MB 會尾端截斷——tar 的 "Unexpected EOF"
    # 就是截斷本身，不是良性訊息，所以不再 grep 掉、也不再 `|| true`。真正的判準是
    # 下面「target 檔數／bytes 對源頭」；不相等就 fail-loud（DB 已 restore 完，但
    # filestore 不完整照樣會讓 asset／logo 500，別讓它靜默成功）。
    kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_ODOO_POD" -- \
        tar cf - -C "/root/.local/share/Odoo/filestore/${SRC_DB}" . \
      | kubectl --context woow-k3s exec -i -n "$TARGET_NAMESPACE" "$TARGET_ODOO_POD" -- \
        tar xf - -C "/root/.local/share/Odoo/filestore/${TARGET_DB}/" || true
    SRC_FS=$(kubectl --context woow-k3s exec -n "$SRC_NAMESPACE" "$SRC_ODOO_POD" -- \
        sh -c "cd /root/.local/share/Odoo/filestore/${SRC_DB} && echo \$(find . -type f | wc -l) \$(du -sb . | cut -f1)")
    DST_FS=$(kubectl --context woow-k3s exec -n "$TARGET_NAMESPACE" "$TARGET_ODOO_POD" -- \
        sh -c "cd /root/.local/share/Odoo/filestore/${TARGET_DB} && echo \$(find . -type f | wc -l) \$(du -sb . | cut -f1)")
    if [ "$SRC_FS" != "$DST_FS" ]; then
        echo -e "${RED}✗ filestore mismatch: source (files bytes) = ${SRC_FS}, target = ${DST_FS}${NC}" >&2
        echo -e "${RED}  exec stdout 串流截斷（docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md）；DB 已 restore、filestore 不完整。${NC}" >&2
        exit 1
    fi
    echo -e "${GREEN}✓ filestore synced and verified (files bytes: ${DST_FS})${NC}"
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
