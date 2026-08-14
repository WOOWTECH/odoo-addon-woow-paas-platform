#!/usr/bin/env bash
#
# Deploy Staging (paas-odoo)
# 觸發 Gitea CI 建置 staging image，等待完成後部署到 K8s dev sandbox。
#
# Usage:
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh [OPTIONS] [ADDON_REF]
#
# Options:
#   --slug <name>         Sandbox slug (default: stg)
#   --image <full-image>  Skip CI build, use existing image (format: registry/repo:tag)
#   --skip-build          Skip CI build, only poll CI status for existing tag
#   --ingress             Enable Ingress ({slug}.woowtech.io)
#   --tag <tag>           Override git tag (default: stg-YYYYMMDD.HHMM, docker tag auto-derived)
#   --timeout <seconds>   Build wait timeout (default: 900 = 15 min)
#   --interval <seconds>  Poll interval (default: 30)
#   --context <name>      kubectl context (default: woow-k3s; override only when
#                         intentionally targeting a different cluster)
#   --post-check-timeout <seconds>
#                         addon 版號 post-check 的收斂等待秒數 (default: 300)。
#                         部署完成後比對 DB 的 ir_module_module 版號與 image 內
#                         manifest；不一致就非 0 退出（#665）。
#   -h, --help            Show usage
#
# Prerequisites:
#   tea CLI with login configured for git-prod.woowtech.io
#   (run: tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>)
#
# Examples:
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh v18.0.1.0.5
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --slug stg-test
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20260329.1400

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- Project root ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# --- Configuration ---
GITEA_URL="https://git-prod.woowtech.io"
CI_REPO="woow-paas/paas-odoo-ci"
REGISTRY_HOST="jcr-prod.woowtech.io"
IMAGE_PATH="woow-paas-docker-local/paas-odoo-stg"

# --- Defaults ---
SLUG="stg"
ADDON_REF=""
IMAGE_OVERRIDE=""
GIT_TAG_OVERRIDE=""
SKIP_BUILD=false
INGRESS=false
POLL_TIMEOUT=900
POLL_INTERVAL=30
# DB 前綴：deploy-stg 是這個值的來源（傳給 create.sh 決定 DB 名，也給 post-check
# 推同一個 DB 名）。以前這串 woow_ 在兩個 CREATE_ARGS 各寫死一次，抽成變數是為了讓
# post-check 與實際部署的 DB 必然是同一顆——不然 gate 可能對著另一個 DB 判綠燈。
DB_PREFIX="woow_"
# addon 版號 post-check 的收斂等待（#665）。綠燈通常是瞬間的（create.sh 回來時
# odoo pod 已 ready）；這個 timeout 是留給「升級真的正在跑」的情形，避免把慢升級
# 誤判成漏升。真的漏升時會等滿再紅——那是刻意的，寧可慢也不要假綠。
POST_CHECK_TIMEOUT=300
# Staging always targets woow-k3s — this is the only valid stg destination,
# and hardcoding the default closes the cross-cluster footgun (current
# kubectl context could be anything, e.g. docker-desktop). --context still
# overrides for the rare case (e.g. testing the pipeline against a
# secondary cluster), and the override is logged loudly so it's never
# accidental.
KUBE_CONTEXT="${KUBE_CONTEXT:-woow-k3s}"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --slug)
            SLUG="$2"
            shift 2
            ;;
        --image)
            IMAGE_OVERRIDE="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --ingress)
            INGRESS=true
            shift
            ;;
        --tag)
            GIT_TAG_OVERRIDE="$2"
            shift 2
            ;;
        --timeout)
            POLL_TIMEOUT="$2"
            shift 2
            ;;
        --interval)
            POLL_INTERVAL="$2"
            shift 2
            ;;
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        --post-check-timeout)
            POST_CHECK_TIMEOUT="$2"
            shift 2
            ;;
        -h|--help)
            # 印檔頭註解（第 3 行起，到第一個非註解行為止）。原本寫死 `head -29 |
            # tail -27`，新增一個選項就會把說明靜默截掉一行——而截掉的樣子跟「本來
            # 就沒寫」完全一樣。
            awk 'NR>2 && /^#/ {sub(/^# ?/, ""); print; next} NR>2 {exit}' "$0"
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            exit 1
            ;;
        *)
            ADDON_REF="$1"
            shift
            ;;
    esac
done

# --- Prerequisites ---
echo -e "${BLUE}Checking prerequisites...${NC}"

for cmd in tea jq kubectl helm git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Missing required tool: ${cmd}${NC}" >&2
        exit 1
    fi
done

# --- Step 1.5：本機 checkout vs 這次要部署的內容（#723）---
#
# 這裡以前（只寫在 SKILL.md、沒有實作）算的是「領先」：
#     AHEAD=$(git rev-list --count @{upstream}..HEAD)
# 領先確實該報 —— 未 push 的 commit 不會被 CI clone 到、不會進 image。但單向的
# `A..B` 天生只回答一半：**落後時 AHEAD=0，整道檢查全綠**。本機比 remote 舊時零
# 訊號，部署者會理所當然地以為「我看到的 code ＝ 部上去的 code」。
#
# 2026-08-14 的實例：本機 develop 落後 origin/develop 22 個 commit（manifest .132
# vs .136），`AHEAD=0` 讓派工者把「develop 與 upstream 同步」寫進工單，再據此推出
# 「DB 版號已等於 develop manifest ⇒ post-check 必然綠、綠燈沒有鑑別力」。兩句都錯
# （版號實際從 .132 升到 .136），而且是靠 teammate 回報時提一句才發現的。
#
# 🔴 落後**不阻擋部署**：CI 一律 clone remote（push `stg-*` tag 時 paas-odoo-ci 的
#    Validate addon ref 套 staging 慣例 `develop`），落後不影響 image 內容，只影響
#    部署者的預期。這是資訊，不是錯誤 —— 做成 gate 只會被 `|| true` 掉。
#
# 兩個方向一次拿到：`git rev-list --left-right --count A...B` 印 "<只在 A><TAB><只在 B>"，
# 取 A=@{upstream}、B=HEAD ⇒ 就是 "<behind> <ahead>"。
echo -e "${BLUE}Checking local checkout vs remote...${NC}"

# manifest 版號共用 verify-addon-version.sh 的 parser（它 source 進來只提供純函式、
# 不跑 main）。刻意不在這裡再寫一份 regex —— 兩份解析遲早漂移成不同答案。
# 檔案不在就只報 commit 差距：版號是輔助資訊，缺它不該擋部署（post-check 那道 gate
# 才對它 fail-loud）。
if [ -f "${SCRIPT_DIR}/verify-addon-version.sh" ]; then
    # shellcheck source=./verify-addon-version.sh
    source "${SCRIPT_DIR}/verify-addon-version.sh"
fi

# <git-ref> → 該 ref 的 manifest 版號；讀不到印 "?"。
# 🔴 一律走 `git show <ref>:<path>`，**不讀 working tree** —— 拿本機檔案冒充 remote
#    的值，正是 #723 那個錯誤前提的來源。
manifest_version_at_ref() {
    local ref="$1" out=""
    if declare -f extract_manifest_version >/dev/null 2>&1; then
        out="$(git -C "$PROJECT_ROOT" show "${ref}:src/__manifest__.py" 2>/dev/null \
               | extract_manifest_version 2>/dev/null || true)"
    fi
    printf '%s' "${out:-?}"
}

GIT_BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(unknown)')"
UPSTREAM_REF="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true)"

if [ -n "$IMAGE_OVERRIDE" ]; then
    # --image 這條路根本不 build：部署內容由那顆既有 image 決定，與本機、與 remote
    # develop 都無關。印本機/remote 版號在這裡是純噪音，只講清楚這件事。
    echo -e "  本次使用 --image 指定既有 image，不觸發 CI build。"
    echo -e "  ⇒ 部署內容由 ${IMAGE_OVERRIDE} 決定，與本機 checkout（${GIT_BRANCH}）無關。"
elif [ -z "$UPSTREAM_REF" ]; then
    echo -e "${YELLOW}  branch ${GIT_BRANCH} 沒有 upstream，無從比對本機與 remote。${NC}"
    echo -e "${YELLOW}  CI 仍會 clone remote develop —— 這次部署的內容與本機 checkout 無關。${NC}"
else
    # 比對基準是 remote-tracking ref，它只反映**上一次 fetch**。不先更新的話，
    # 「同步」可能是拿一份過期的 origin/develop 比出來的 —— 那是假綠，比沒有這道
    # 檢查更糟。fetch 只動 remote-tracking ref，不碰 working tree 也不動本地分支。
    if ! git -C "$PROJECT_ROOT" fetch --quiet origin 2>/dev/null; then
        echo -e "${YELLOW}  （git fetch 失敗；以下比對基於上一次 fetch 的 remote-tracking ref，可能過期）${NC}"
    fi
    SYNC_COUNTS="$(git -C "$PROJECT_ROOT" rev-list --left-right --count "${UPSTREAM_REF}...HEAD" 2>/dev/null || printf '0\t0')"
    # `|| true`：herestring 會補尾端換行所以 read 正常回 0，但這道檢查是純資訊性的，
    # 不值得為了任何一種讀取失敗讓 set -e 把整支部署腳本殺掉。
    read -r BEHIND AHEAD <<< "$SYNC_COUNTS" || true
    BEHIND="${BEHIND:-0}"
    AHEAD="${AHEAD:-0}"

    if [ "$AHEAD" -gt 0 ]; then
        echo -e "${YELLOW}  ⚠️  本機有 ${AHEAD} 個未 push 的 commit —— 這些不會被包進 image：${NC}"
        # 截斷並**明說截了幾個**：branch 的 upstream 不是 develop 時 AHEAD 可能是三位數
        # （實測對 origin/main 為 886），整串倒出來會把下面真正要看的訊息推出螢幕。
        git -C "$PROJECT_ROOT" log --oneline --max-count=10 "${UPSTREAM_REF}..HEAD" | sed 's/^/      /'
        if [ "$AHEAD" -gt 10 ]; then
            echo -e "      …（共 ${AHEAD} 個，只列最新 10 個）"
        fi
    fi

    if [ "$BEHIND" -gt 0 ]; then
        LOCAL_VERSION="$(manifest_version_at_ref HEAD)"
        REMOTE_VERSION="$(manifest_version_at_ref "$UPSTREAM_REF")"
        echo -e "${YELLOW}  ⚠️  本機 ${GIT_BRANCH} 落後 ${UPSTREAM_REF} ${BEHIND} 個 commit。${NC}"
        echo -e "      manifest 版號：本機 HEAD = ${LOCAL_VERSION} ／ ${UPSTREAM_REF} = ${REMOTE_VERSION}"
        echo -e "      ⇒ 這次部署的內容來自 remote，不是你手上這份。落後**不阻擋部署**（CI 吃 remote，image 是對的）。"
        echo -e "${YELLOW}      但別拿本機版號推論 post-check 會是什麼結果 —— 那正是 #723 的來源。${NC}"
    fi

    if [ "$AHEAD" -eq 0 ] && [ "$BEHIND" -eq 0 ]; then
        echo -e "  ${GREEN}本機 ${GIT_BRANCH} 與 ${UPSTREAM_REF} 同步${NC}（manifest $(manifest_version_at_ref HEAD)）"
    fi

    # upstream 不是 origin/develop 時，上面比的那個 ref 根本不是這次會被烤的東西 ——
    # 不講清楚的話，這段新輸出自己就會變成下一個錯誤前提。
    if [ "$UPSTREAM_REF" != "origin/develop" ]; then
        DEVELOP_VERSION="$(manifest_version_at_ref origin/develop)"
        echo -e "${YELLOW}  注意：stg build 一律烤 develop（paas-odoo-ci 對 stg-* tag 的 staging 慣例），${NC}"
        echo -e "${YELLOW}  與上面比對的 ${UPSTREAM_REF} 不是同一個 ref。origin/develop manifest = ${DEVELOP_VERSION}${NC}"
    fi
fi
echo ""

# kubectl context 守衛（#32）。原本這裡是「驗存在 → warn → kubectl config use-context」，
# 有兩個問題，共用 helper 一次收掉：
#   Part A  use-context 改的是 ~/.kube/config —— 腳本跑完，操作者 shell 的 default
#           context 就停在這裡設的值。#32 的重現步驟就是這條。改用 KUBECONFIG overlay，
#           切換只作用在本次執行，全域全程不動。
#   Part B  「非 woow-k3s 只警告」是可以被略過的訊號。改為對 .sandbox.yml 宣告的
#           allowed_kube_contexts_stg 做值域比對，不在名單內就擋；真要打別的叢集用
#           SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 明示（大聲警告）。
# shellcheck source=../../lib/kube-context.sh
source "${PROJECT_ROOT}/.claude/skills/lib/kube-context.sh"
WK_REPO_ROOT="$PROJECT_ROOT"
if ! wk_require_context "$KUBE_CONTEXT" stg "$PROJECT_ROOT"; then
    echo -e "${YELLOW}Default for stg is 'woow-k3s'; override with --context only if you really mean it.${NC}" >&2
    exit 1
fi

# --- Resolve woow-dev plugin's k8s-dev-sandbox create.sh ---
#
# deploy-stg 把「建沙盒」委派給 woow-dev plugin 的 create.sh（即
# /woow-dev:k8s-dev-sandbox 的引擎），不再自帶一份重複的 create.sh。
# CLAUDE_PLUGIN_ROOT 在純 bash 不可用，故依序解析 plugin 安裝路徑：
#   1) WOOWPRD_PLUGIN_ROOT env 覆寫（最高優先；指到的檔存在才採用，否則往下）
#   2) 本機 dev copy（plugins-local，開發中的活版本）
#   3) marketplace cache（cache/<market>/<plugin>/<ver>/skills/...）。plugin 目錄名
#      改過：現行 `woow-dev`（優先），舊 `woowprd`（相容）。同名可能並存多版本目錄
#      → 收集後 version-sort 倒序取最新（1.10.0 > 1.9.0）。
#   4) fallback：不綁 plugin 名，cache 根下遞迴搜同一相對路徑，避免未來 plugin 再
#      改名（如 woow-dev → 其他）整組又壞掉。
# 註：bash nullglob off，glob 不匹配會留 literal → `[ -f ]` 失敗自動跳過。
# 註：本函式只在 `if ! ...`（set -e 暫停）context 呼叫，故內部非零回傳不會誤殺主流程；
#     每個成功分支都顯式 `return 0`，避免 pipefail 下 sort|head 的 SIGPIPE 汙染回傳值。
resolve_plugin_create_sh() {
    local rel="skills/k8s-dev-sandbox/scripts/create.sh"
    local cache_root="$HOME/.claude/plugins/cache"
    local c glob_mid newest

    # 優先序 1-2：確定路徑（env override 最高優先），命中即回。
    for c in \
        ${WOOWPRD_PLUGIN_ROOT:+"${WOOWPRD_PLUGIN_ROOT}/${rel}"} \
        "$HOME/.claude/plugins-local/woowprd/${rel}"; do
        [ -f "$c" ] && { printf '%s\n' "$c"; return 0; }
    done

    # 優先序 3：marketplace cache，現行名 woow-dev 優先、舊名 woowprd 相容。
    for glob_mid in woow-dev woowprd; do
        local -a hits=()
        for c in "$cache_root/"*"/${glob_mid}/"*"/${rel}"; do
            [ -f "$c" ] && hits+=("$c")
        done
        if [ "${#hits[@]}" -gt 0 ]; then
            newest="$(printf '%s\n' "${hits[@]}" | sort -Vr | head -n1)"
            [ -n "$newest" ] || newest="${hits[0]}"  # sort -V 缺席時退化為 glob 第一筆
            printf '%s\n' "$newest"
            return 0
        fi
    done

    # 優先序 4（fallback）：cache 根下遞迴搜同一相對路徑，同樣取版本最新。
    local -a fhits=()
    while IFS= read -r c; do
        [ -n "$c" ] && fhits+=("$c")
    done < <(find "$cache_root" -type f -path "*/${rel}" 2>/dev/null)
    if [ "${#fhits[@]}" -gt 0 ]; then
        newest="$(printf '%s\n' "${fhits[@]}" | sort -Vr | head -n1)"
        [ -n "$newest" ] || newest="${fhits[0]}"
        printf '%s\n' "$newest"
        return 0
    fi
    return 1
}

if ! PLUGIN_CREATE_SH="$(resolve_plugin_create_sh)"; then
    echo -e "${RED}ERROR: 找不到 woow-dev plugin 的 k8s-dev-sandbox create.sh。${NC}" >&2
    echo -e "${YELLOW}deploy-stg 已改為委派給 woow-dev plugin 建沙盒（不再自帶 create.sh）。${NC}" >&2
    echo -e "${YELLOW}請安裝 woow-dev plugin，或設 WOOWPRD_PLUGIN_ROOT 指向其根目錄。${NC}" >&2
    echo -e "${YELLOW}  已找：\$WOOWPRD_PLUGIN_ROOT / ~/.claude/plugins-local/woowprd / ~/.claude/plugins/cache/*/{woow-dev,woowprd}/* / cache 遞迴 fallback${NC}" >&2
    exit 1
fi
echo -e "${BLUE}Delegating sandbox creation to woow-dev plugin:${NC} ${PLUGIN_CREATE_SH}"

# web.base.url + freeze 的 seed 已宣告式化：.sandbox.yml 的 config_params_stg，
# 由 plugin create.sh 在部署時 diff-aware 套用（值變才 restart、失敗則 create 非零退出）。

# --- 部署 + 收尾 post-check（兩條路徑共用）---
#
# 抽成函式的理由不只是去重：`--image` 那條路以前是 create.sh 一跑完就 `exit 0`，
# 連 `kubectl get pods` 都沒有，收尾檢查自然也漏掉一整條。**部署的路徑有兩條、
# 驗證的路徑只有一條**正是 #665 這類無聲失效的溫床，所以兩條合流到同一個收尾。
VERIFY_ADDON_SH="${SCRIPT_DIR}/verify-addon-version.sh"

deploy_and_postcheck() {
    local create_args=(--repo "$PROJECT_ROOT" --context "$KUBE_CONTEXT" \
        --db-prefix "$DB_PREFIX" --yes --branch "$SLUG" --image "$FULL_IMAGE" --mode stg)
    if [ "$INGRESS" = true ]; then
        create_args+=(--ingress)
    fi
    bash "$PLUGIN_CREATE_SH" "${create_args[@]}"

    local namespace="dev-sandbox-${SLUG}"
    echo ""
    echo -e "${BLUE}Verifying deployment...${NC}"
    # 顯式 --context：本檔其餘 kubectl/helm 都是委派出去且各自帶 context，這是唯一
    # 一個吃 ambient context 的呼叫點。overlay 已經把它導對了，帶上旗標是第二道保險
    # ——這樣就算 overlay 機制被拿掉，這行也不會靜默改打別的叢集。
    kubectl --context "$KUBE_CONTEXT" get pods -n "$namespace"

    # 🔴 #665 post-check：換 image **不會**升 addon，而漏升時 helm 綠燈／pod
    #    1/1 Running／logs 乾淨，與成功長得一模一樣。這道 gate 讓那條路徑變紅，
    #    且必須讓整支 script 非 0 退出（`set -e` 會接手）——只印 warning 的話，
    #    自動化呼叫端一樣看不見，等於沒做。
    if [ ! -f "$VERIFY_ADDON_SH" ]; then
        echo -e "${RED}ERROR: 找不到 addon 版號 post-check：${VERIFY_ADDON_SH}${NC}" >&2
        echo -e "${YELLOW}沒有 post-check 就無從得知 addon 到底升了沒 —— 視為部署失敗（#665），不 fallback 成「跳過」。${NC}" >&2
        return 1
    fi
    bash "$VERIFY_ADDON_SH" \
        --slug "$SLUG" \
        --context "$KUBE_CONTEXT" \
        --db-prefix "$DB_PREFIX" \
        --pod-image "$FULL_IMAGE" \
        --repo "$PROJECT_ROOT" \
        --timeout "$POST_CHECK_TIMEOUT"

    echo ""
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Staging deployment complete!${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo -e "  Namespace: ${namespace}"
    echo -e "  Image:     ${FULL_IMAGE}"
    echo -e "  Addon Ref: ${ADDON_REF}"
    echo ""
}

# Check tea login (not needed for --image override)
if [ -z "$IMAGE_OVERRIDE" ]; then
    if ! tea login list &>/dev/null; then
        echo -e "${RED}Error: No tea login configured for git-prod.woowtech.io${NC}" >&2
        echo -e "Run: tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>" >&2
        exit 1
    fi
fi

# Agent-runtime enablement: only the NAMESPACE secret must be supplied by the
# deployer — it is Odoo-side only and acts as the "enable agent-runtime" switch.
# create.sh (stg) auto-sources AUTH + WEBHOOK from the operator's canonical
# secret (paas-operator-stg-agent-runtime-auth) and fail-fasts if it cannot, so
# they are NOT re-warned here. Without the namespace secret the in-task pod path
# / state-sync simply stays disabled; legacy AI chat deploys fine either way.
if [ -z "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ]; then
    echo -e "${YELLOW}Note: AGENT_RUNTIME_NAMESPACE_SECRET 未在當下 env 偵測到。${NC}" >&2
    echo -e "${YELLOW}  若 onboard profile 已記錄，create.sh 載入後仍會啟用 agent-runtime；兩處都沒有則保持停用（legacy AI chat 不受影響）。${NC}" >&2
    echo -e "${YELLOW}  持久供值：/woow-dev:k8s-dev-sandbox onboard --repo \"\$(git rev-parse --show-toplevel)\" --set AGENT_RUNTIME_NAMESPACE_SECRET=<value>${NC}" >&2
    echo -e "${YELLOW}  (AUTH + WEBHOOK auto-source from the operator secret; export AGENT_RUNTIME_AUTH_SECRET_CURRENT / AGENT_RUNTIME_WEBHOOK_SECRET to override.)${NC}" >&2
fi

# --- Resolve addon ref ---
cd "$PROJECT_ROOT"

if [ -z "$ADDON_REF" ]; then
    ADDON_REF=$(git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)
fi

# --- Resolve git tag and docker tag ---
if [ -n "$GIT_TAG_OVERRIDE" ]; then
    GIT_TAG="$GIT_TAG_OVERRIDE"
else
    GIT_TAG="stg-$(date -u +'%Y%m%d.%H%M')"
fi

# Docker image tag = git tag with stg- prefix stripped
DOCKER_TAG="${GIT_TAG#stg-}"
FULL_IMAGE="${REGISTRY_HOST}/${IMAGE_PATH}:${DOCKER_TAG}"

# --- Handle --image override (skip everything, go straight to deploy) ---
if [ -n "$IMAGE_OVERRIDE" ]; then
    FULL_IMAGE="$IMAGE_OVERRIDE"
    echo -e "${BLUE}Using provided image: ${FULL_IMAGE}${NC}"
    # Jump to deploy（收尾與正常路徑共用同一個 post-check，見 deploy_and_postcheck）
    deploy_and_postcheck
    exit 0
fi

# --- Display configuration ---
echo ""
echo -e "${BLUE}Deploy Staging Configuration:${NC}"
echo -e "  ${BLUE}Addon Ref:${NC}    $ADDON_REF"
echo -e "  ${BLUE}Git Tag:${NC}      $GIT_TAG"
echo -e "  ${BLUE}Docker Tag:${NC}   $DOCKER_TAG"
echo -e "  ${BLUE}Full Image:${NC}   $FULL_IMAGE"
echo -e "  ${BLUE}Slug:${NC}         $SLUG"
echo -e "  ${BLUE}Namespace:${NC}    dev-sandbox-${SLUG}"
echo -e "  ${BLUE}Trigger:${NC}      Gitea API create tag ${GIT_TAG}"
echo ""

# --- Trigger CI build via Gitea API ---
if [ "$SKIP_BUILD" = false ]; then
    echo -e "${BLUE}Triggering CI build via Gitea API...${NC}"

    # Get main branch latest commit SHA
    if ! BRANCH_RESP=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/branches/main" 2>&1); then
        echo -e "${RED}Error: Failed to get main branch info${NC}" >&2
        echo "$BRANCH_RESP" >&2
        exit 1
    fi
    MAIN_SHA=$(echo "$BRANCH_RESP" | jq -r '.commit.id // empty')

    if [ -z "$MAIN_SHA" ]; then
        echo -e "${RED}Error: Failed to parse main branch SHA${NC}" >&2
        echo "$BRANCH_RESP" | jq . 2>/dev/null || echo "$BRANCH_RESP" >&2
        exit 1
    fi

    echo -e "  Main branch SHA: ${MAIN_SHA:0:12}"

    # Create tag on CI repo
    TAG_RESP=$(tea api -X POST -r "${CI_REPO}" "repos/{owner}/{repo}/tags" \
        -f "tag_name=${GIT_TAG}" -f "target=${MAIN_SHA}" 2>&1) || true
    TAG_NAME=$(echo "$TAG_RESP" | jq -r '.name // empty' 2>/dev/null)

    if [ -z "$TAG_NAME" ]; then
        # Check if tag already exists (409 conflict)
        if echo "$TAG_RESP" | grep -qi "exist"; then
            echo -e "${YELLOW}Tag ${GIT_TAG} already exists, proceeding to poll CI status...${NC}"
        else
            echo -e "${RED}Error: Failed to create tag ${GIT_TAG}${NC}" >&2
            echo "$TAG_RESP" >&2
            exit 1
        fi
    else
        echo -e "${GREEN}CI build triggered: tag ${TAG_NAME} created${NC}"
    fi

    echo -e "  Monitor: ${GITEA_URL}/${CI_REPO}/actions"
else
    echo -e "${YELLOW}Skipping CI build (--skip-build)${NC}"
fi

# --- Poll CI status via Gitea API ---
echo ""
echo -e "${BLUE}Waiting for CI to complete...${NC}"
echo -e "  Git Tag: ${GIT_TAG}"
echo -e "  Timeout: ${POLL_TIMEOUT}s (poll every ${POLL_INTERVAL}s)"
echo ""

MAX_ATTEMPTS=$(( POLL_TIMEOUT / POLL_INTERVAL ))
CI_DONE=false

for i in $(seq 1 "$MAX_ATTEMPTS"); do
    # Query action tasks, filter by head_branch matching our tag
    TASKS_RESP=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/actions/tasks?limit=10" 2>/dev/null || echo "{}")

    # Extract statuses of all tasks matching our tag
    STATUSES=$(echo "$TASKS_RESP" | jq -r \
        ".workflow_runs // [] | map(select(.head_branch == \"${GIT_TAG}\")) | .[].status" 2>/dev/null)

    ELAPSED=$(( i * POLL_INTERVAL ))

    if [ -z "$STATUSES" ]; then
        echo -e "  [${ELAPSED}s/${POLL_TIMEOUT}s] CI run not found yet, waiting ${POLL_INTERVAL}s..."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # Check for failure/cancelled in any task
    if echo "$STATUSES" | grep -qE '^(failure|cancelled)$'; then
        FAILED_STATUS=$(echo "$STATUSES" | grep -E '^(failure|cancelled)$' | head -1)
        echo -e "${RED}CI failed (status: ${FAILED_STATUS})${NC}" >&2
        echo -e "Check: ${GITEA_URL}/${CI_REPO}/actions" >&2
        exit 1
    fi

    # Check if all tasks are successful
    NON_SUCCESS=$(echo "$STATUSES" | grep -v '^success$' || true)
    if [ -z "$NON_SUCCESS" ]; then
        echo -e "${GREEN}CI completed successfully! (attempt ${i}/${MAX_ATTEMPTS})${NC}"
        CI_DONE=true
        break
    fi

    # Still in progress
    CURRENT=$(echo "$STATUSES" | head -1)
    echo -e "  [${ELAPSED}s/${POLL_TIMEOUT}s] CI status: ${CURRENT}, waiting ${POLL_INTERVAL}s..."
    sleep "$POLL_INTERVAL"
done

if [ "$CI_DONE" = false ]; then
    echo -e "${RED}Timeout: CI not completed after ${POLL_TIMEOUT}s${NC}" >&2
    echo -e "Check CI status: ${GITEA_URL}/${CI_REPO}/actions" >&2
    exit 1
fi

# --- Deploy to sandbox ---
echo ""
echo -e "${BLUE}Deploying to sandbox...${NC}"

deploy_and_postcheck
