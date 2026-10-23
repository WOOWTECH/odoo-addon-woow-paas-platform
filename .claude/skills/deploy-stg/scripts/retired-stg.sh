#!/usr/bin/env bash
#
# dev-sandbox-stg 退役守衛（woow-paas/paas-app-bundle-cd#289、#342）
#
# 2026-10-23 起 stg 平台 Odoo 由 Fleet 管理的 release 提供：
#   namespace paas-stg-system ／ release paas-odoo-platform-stg（chart woow-odoo）／ DB woow_stg
# 舊沙盒 dev-sandbox-stg 已縮 0、等人刪（TTL 10/29 也會自動刪）。
#
# 🔴 為什麼要擋、而且擋在任何寫入之前：deploy-stg 以前預設 slug=stg，照預設跑會
#    經 plugin create.sh 把 dev-sandbox-stg **重建**出來。重建出來的沙盒不受 Fleet 管，
#    stg operator 的沙盒 NetworkPolicy 規則（選所有平台建的沙盒）也會放行它，
#    可能和新 release 的 cron 雙跑、同時對 stg operator 下指令。
#    deploy-stg 在部署前還會到 paas-odoo-ci 建 tag 觸發 CI，那也是遠端寫入 ⇒
#    守衛必須在參數解析完就判，不能等到 create.sh。
#
# 🔴 刻意**沒有**繞過旗標或環境變數。stg 換版的唯一正路是 paas-app-bundle-cd 的
#    image pin PR；真的需要舊沙盒形狀，請用別的 slug 建一個新沙盒。
#
# 本檔只放純函式（不碰 kubectl、檔案系統、網路），由 deploy-stg.sh 與
# verify-addon-version.sh source，測試在 tests/test-retired-stg-guard.sh。

# 退役目標（集中一處，兩支腳本與測試共用）
DSTG_RETIRED_SLUG="stg"
DSTG_RETIRED_NAMESPACE="dev-sandbox-stg"

# dstg_normalize_slug <name> —— 與 woow-dev plugin lib/common.sh 的 compute_slug 同一套規則：
# 非英數換 '-' → 轉小寫 → 連續 '-' 收成一個 → 去頭尾 '-' → 截 40 字。
# 一定要先正規化再比：`--slug STG`、`--slug stg.`、`--slug -stg-` 經 create.sh
# 正規化後都是 stg，只比字面值就會被這些寫法繞過。
# 不用 ${s,,}：macOS 內建 bash 3.2 沒有這個語法。
dstg_normalize_slug() {
    local s="${1:-}"
    s="${s//[^a-zA-Z0-9]/-}"
    s="$(printf '%s' "$s" | tr '[:upper:]' '[:lower:]')"
    while [[ "$s" == *--* ]]; do
        s="${s//--/-}"
    done
    s="${s#-}"
    s="${s%-}"
    printf '%s\n' "${s:0:40}"
}

# dstg_is_retired_target <slug> [namespace] —— 目標是已退役的 dev-sandbox-stg 就回 0。
# slug（正規化後）等於 stg，或 namespace 字面等於 dev-sandbox-stg，任一成立即算。
dstg_is_retired_target() {
    local slug="${1:-}" ns="${2:-}"
    if [ -n "$slug" ] && [ "$(dstg_normalize_slug "$slug")" = "$DSTG_RETIRED_SLUG" ]; then
        return 0
    fi
    if [ -n "$ns" ] && [ "$ns" = "$DSTG_RETIRED_NAMESPACE" ]; then
        return 0
    fi
    return 1
}

# dstg_print_retired_notice <呼叫端名稱> —— 退役說明，一律印到 stderr。
dstg_print_retired_notice() {
    local caller="${1:-deploy-stg}"
    cat >&2 <<EOF
⛔ ${caller}：拒絕執行 —— 目標是已退役的 ${DSTG_RETIRED_NAMESPACE}（slug=${DSTG_RETIRED_SLUG}）。

stg 平台 Odoo 已在 woow-paas/paas-app-bundle-cd#289 搬到 Fleet 管理的 release：
  namespace paas-stg-system ／ release paas-odoo-platform-stg（chart woow-odoo）／ DB woow_stg
  外部 https://stg.woowtech.io 自 2026-10-23 13:49Z 起由它提供。
照舊流程對 ${DSTG_RETIRED_NAMESPACE} 動作會把舊沙盒重建出來：它不受 Fleet 管，stg operator 的
沙盒 NetworkPolicy 規則也會放行它，可能和新 release 的 cron 雙跑。

stg 換版的正確流程：
  在 woow-paas/paas-app-bundle-cd 改 paas-odoo-platform-stg/values-stg.yaml 的 image pin
  （image.digest＋image.tag），開 PR → 覆核 → merge，由 Fleet 套用。
  步驟與驗收：paas-app-bundle-cd 的 docs/platform-odoo-stg-runbook.md
  pin 位置：  paas-app-bundle-cd 的 docs/sop-cd.md §4

本 skill 現在只適用其他沙盒：請改用 --slug <其他沙盒名>。此守衛沒有繞過旗標。
EOF
}
