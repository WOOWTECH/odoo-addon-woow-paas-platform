#!/usr/bin/env bash
#
# Seed sandbox DB from dev-sandbox-stg（⛔ 已停用，見下方）
# 把 stg 的 Odoo DB 整個 pg_dump → restore 到 sandbox，避免重新設定 ECPay / paas-operator
#
# Usage:
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh [TARGET_SLUG]
#
# Arguments:
#   TARGET_SLUG    Sandbox name (default: auto-detect from current branch)（停用中，參數不再被讀取）
#
# Examples:
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh
#   .claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh smart-home-sub
#
# ⛔ 2026-10-23 起停用（woow-paas/paas-app-bundle-cd#289、#342）
#
# 來源 dev-sandbox-stg 已退役（Odoo 縮 0、ns 即將由人刪除；TTL 10/29 也會自動刪）。
# stg 平台改由 Fleet 管理的 paas-stg-system/paas-odoo-platform-stg 提供。
# 本腳本在任何 kubectl 之前就 exit 2。原本的 6 步流程（pg_dump → 本機 → 目標沙盒 →
# DROP／restore → 重啟 → filestore）已移除，改寫時對照 git 歷史：
#   git show 686c1e2c:.claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh
#
# 為什麼不直接把來源換成新 release（只改三個常數不夠，而且會碰正在服務的 stg）：
#   - 形狀全不同：PG container 名是 postgresql（不是 postgres，`kubectl cp -c` 會失敗）；
#     Odoo pod 的 label 是 component=server（不是 odoo），pod 內有 odoo＋nginx 兩個
#     container，exec 要 -c odoo；data 卷掛在 /var/lib/odoo（不是 /root/.local/share/Odoo，
#     filestore 的實際路徑改寫時要再確認）；pg_dump 要走 pod 內 POSTGRES_USER／POSTGRES_PASSWORD。
#     （2026-10-23 唯讀查 pod spec 所得，只看 container／label／mountPath／env 名稱）
#   - 會寫入正在服務的 stg：第 1 步把 dump 寫進 stg PG pod 的 /tmp、第 6 步從服務中的
#     web pod 串 tar。舊沙盒當來源時那是開發用沙盒；現在是 Fleet 管的 stg 平台。
#   - woow_stg 約 105 MB（DB 大小；dump 大小未量），很可能超過 kubectl exec／cp 串流已知會尾端截斷的 ~20 MB
#     （docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md），
#     要改成叢集內 pod-to-pod 搬運才可靠。
#   - 下游會 DROP DATABASE 目標 DB，改寫後必須在真叢集上驗過才能重新啟用。
# 改寫前可參考 seed-from-prod.sh／prod-clone/（叢集內搬運＋中和）的做法。

set -euo pipefail

RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# --- 停用守衛：在任何 kubectl 之前結束（沒有繞過旗標）---
{
    echo -e "${RED}⛔ seed-from-stg 已停用：來源 dev-sandbox-stg 已退役（woow-paas/paas-app-bundle-cd#289）。${NC}"
    echo -e "${YELLOW}stg 平台現在是 Fleet 管理的 paas-stg-system/paas-odoo-platform-stg（DB woow_stg）。${NC}"
    echo -e "${YELLOW}本腳本尚未改寫成以新 release 為來源（PG／Odoo 的 pod 形狀、filestore 路徑都不同，${NC}"
    echo -e "${YELLOW}且會寫入正在服務的 stg），理由與改寫要點見本檔檔頭。${NC}"
    echo -e "${YELLOW}需要 stg 形狀的資料時，請先開 issue 討論來源與搬運方式；prod clone 走 seed-from-prod.sh。${NC}"
} >&2
exit 2
