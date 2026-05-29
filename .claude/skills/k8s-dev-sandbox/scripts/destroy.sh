#!/usr/bin/env bash
#
# Destroy a K8s Dev Sandbox
# 銷毀 Kubernetes 中的 Odoo 開發沙盒環境
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   --force             Skip confirmation prompt
#   -h, --help          Show usage
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh epic-smarthome
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh epic-smarthome --force

set -euo pipefail

# --- 顏色輸出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 取得專案根目錄 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# --- 預設值 ---
SLUG=""
FORCE=false
# issue #85 fix B: sandbox is ephemeral — by default remove postgres data so a
# re-deploy starts clean (otherwise Retain-policy PV / kept PVC leftovers rebind
# stale DB and the create.sh init gate gets confused). --keep-data preserves it.
KEEP_DATA=false
# KUBE_CONTEXT must be set explicitly (--context flag or KUBE_CONTEXT env).
# destroy.sh runs DESTRUCTIVE, cluster-scoped operations (helm uninstall,
# delete namespace, and — issue #85 fix B — delete pv). Defaulting to the
# ambient current-context risks wiping the wrong cluster's data if it is left
# on e.g. woow-k3s while the operator means to clean a local sandbox, and a
# same-named namespace exists on both. Mirrors create.sh hardening (a62b75b).
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        --keep-data)
            # issue #85 fix B: preserve postgres data (PVC + Retain/Released PV)
            KEEP_DATA=true
            shift
            ;;
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Destroy a K8s dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --force             Skip confirmation prompt"
            echo "  --keep-data         Preserve postgres data (PVC + Retain/Released PV)."
            echo "                      Default removes data so re-deploy starts clean (issue #85)."
            echo "  --context <name>    Required: kubectl context (e.g. woow-k3s, docker-desktop)."
            echo "                      Can also be set via KUBE_CONTEXT env var."
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0 epic-smarthome --context docker-desktop"
            echo "  $0 epic-smarthome --force --context woow-k3s"
            echo "  $0 epic-smarthome --keep-data --context woow-k3s"
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
            # Positional argument: sandbox name
            if [ -z "$SLUG" ]; then
                SLUG="$1"
            else
                echo -e "${RED}Unexpected argument: $1${NC}" >&2
                echo "Use --help for usage information" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# --- 如果未提供名稱，從 branch 自動偵測 ---
if [ -z "$SLUG" ]; then
    BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    SLUG=$(echo "$BRANCH" \
        | sed 's/[^a-zA-Z0-9]/-/g' \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/-\{2,\}/-/g' \
        | sed 's/^-//;s/-$//' \
        | cut -c1-40)
    echo -e "${BLUE}Auto-detected sandbox from branch: ${BRANCH}${NC}"
fi

NAMESPACE="dev-sandbox-${SLUG}"

# --- 檢查先決條件 ---
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
    echo -e "${RED}helm not found. Please install Helm 3+ first.${NC}" >&2
    exit 1
fi

# --- 要求顯式 kubectl context（防 cross-cluster 誤刪，mirror create.sh）---
# destroy.sh 會跑 helm uninstall + delete namespace + delete pv，全為破壞性操作。
if [[ -z "$KUBE_CONTEXT" ]]; then
    echo -e "${RED}ERROR: kubectl context is required (use --context <name> or export KUBE_CONTEXT).${NC}" >&2
    echo -e "${YELLOW}This is required to prevent accidental cross-cluster destroys.${NC}" >&2
    echo -e "${YELLOW}Available contexts:${NC}" >&2
    kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || echo "  (none configured)" >&2
    echo -e "${YELLOW}Example: $0 ${SLUG:-<name>} --context woow-k3s${NC}" >&2
    exit 1
fi
if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$KUBE_CONTEXT"; then
    echo -e "${RED}ERROR: kubectl context '${KUBE_CONTEXT}' not found in kubeconfig.${NC}" >&2
    echo -e "${YELLOW}Available contexts:${NC}" >&2
    kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
    exit 1
fi
# Switch to the requested context; subsequent kubectl/helm calls inherit it.
# Visible echo so the operator sees exactly which cluster they're destroying.
echo -e "${BLUE}Using kubectl context:${NC} ${GREEN}${KUBE_CONTEXT}${NC}"
kubectl config use-context "$KUBE_CONTEXT" >/dev/null
export KUBE_CONTEXT

# Confirm connectivity before deciding "namespace not found" — otherwise an
# unreachable cluster would be misread as "nothing to destroy" (exit 0).
if ! kubectl --request-timeout=30s cluster-info >/dev/null 2>&1; then
    echo -e "${RED}Cannot connect to Kubernetes cluster '${KUBE_CONTEXT}'. Check your kubeconfig.${NC}" >&2
    exit 1
fi

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${YELLOW}Namespace '$NAMESPACE' does not exist. Nothing to destroy.${NC}"
    exit 0
fi

# --- 確認操作 ---
if [ "$FORCE" = false ]; then
    echo -e "${YELLOW}This will destroy sandbox: ${NAMESPACE}${NC}"
    echo -e "${YELLOW}All data will be lost.${NC}"
    echo ""

    # Show current resources
    echo -e "${BLUE}Resources in namespace:${NC}"
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    echo ""

    echo -n -e "${YELLOW}Continue? (yes/no): ${NC}"
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        echo -e "${YELLOW}Aborted.${NC}"
        exit 0
    fi
fi

echo ""

# issue #85 fix B: before tearing down, capture the PV names bound to this
# namespace's PVCs. Done *before* helm uninstall while the PVCs still exist so
# we can later remove Retain-policy PVs that would otherwise leave stale data on
# disk. Skipped entirely when --keep-data is set. Command substitution is wrapped
# (|| true) to stay safe under `set -euo pipefail`.
PV_NAMES=""
if [ "$KEEP_DATA" = false ]; then
    # No 2>/dev/null here: the cluster is already confirmed reachable and the
    # namespace confirmed to exist above, so a `get pvc` error is a real signal
    # (it must not be silently swallowed into an empty list → skipped cleanup).
    # A genuinely empty namespace yields empty stdout + exit 0 (no stderr noise).
    PV_NAMES=$(kubectl get pvc -n "$NAMESPACE" -o jsonpath='{range .items[*]}{.spec.volumeName}{"\n"}{end}' | grep -v '^$' || true)
fi

# --- Helm uninstall ---
echo -e "${BLUE}Uninstalling Helm release '${SLUG}'...${NC}"
if helm uninstall "$SLUG" -n "$NAMESPACE" 2>/dev/null; then
    echo -e "${GREEN}Helm release uninstalled.${NC}"
else
    echo -e "${YELLOW}Helm release '${SLUG}' not found or already removed. Continuing cleanup...${NC}"
fi

# issue #85 fix B: explicitly delete PVCs before the namespace. Helm leaves
# resource-policy:keep PVCs behind, and a bound PVC keeps its (possibly Retain)
# PV alive with stale postgres data. Removing them ensures a clean re-deploy.
if [ "$KEEP_DATA" = false ]; then
    echo -e "${BLUE}Deleting PVCs in namespace '${NAMESPACE}' (ephemeral sandbox, issue #85)...${NC}"
    kubectl delete pvc --all -n "$NAMESPACE" --timeout=60s 2>/dev/null || true
else
    echo -e "${YELLOW}--keep-data set: preserving PVCs and postgres data.${NC}"
fi

# --- 刪除 namespace ---
echo -e "${BLUE}Deleting namespace '${NAMESPACE}'...${NC}"
if kubectl delete namespace "$NAMESPACE" --timeout=60s 2>/dev/null; then
    echo -e "${GREEN}Namespace deleted.${NC}"
else
    echo -e "${YELLOW}Failed to delete namespace within timeout. It may still be terminating.${NC}"
    echo -e "${YELLOW}Check with: kubectl get namespace ${NAMESPACE}${NC}"
fi

# issue #85 fix B: reclaim the PVs bound to THIS sandbox's PVCs. A Retain-policy
# PV survives PVC/namespace deletion and keeps stale data on disk; a Delete-policy
# PV is normally GC'd with its PVC but may linger if the namespace is slow to
# terminate. PV_NAMES was captured strictly from `kubectl get pvc -n $NAMESPACE`,
# so this cluster-scoped delete is confined to this sandbox's own storage. We
# delete each captured PV best-effort and UNCONDITIONALLY rather than gating on a
# policy/status snapshot taken right after an async namespace delete (that snapshot
# races: a Delete-policy PV may still read Bound and get skipped). Already-GC'd PVs
# return not-found (--ignore-not-found → no-op); Retain ones get reclaimed.
if [ "$KEEP_DATA" = false ] && [ -n "$PV_NAMES" ]; then
    echo -e "${BLUE}Reclaiming PVs bound to this sandbox (issue #85)...${NC}"
    while IFS= read -r pv; do
        [ -z "$pv" ] && continue
        echo -e "  Deleting PV '${pv}'"
        kubectl delete pv "$pv" --ignore-not-found --timeout=60s 2>/dev/null || true
    done <<< "$PV_NAMES"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Sandbox destroyed: ${NAMESPACE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
