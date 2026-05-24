#!/usr/bin/env bash
# TDD red phase — Phase D WS1 第 10 條: charts agent-runtime Secret env mount.
#
# Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS2 charts row
#        (落點修正: Odoo deployment chart 在本 repo charts/odoo-dev-sandbox,
#         非 woow-paas-charts repo).
#
# 斷言: `helm template` render 出的 odoo-deployment 必須注入凍結的 5 個
#       agent-runtime K8s Secret env (OQ#1 ruling (b) — K8s Secret env
#       injection). 目前 odoo-deployment.yaml 只 mount postgres-* env, 缺這 5
#       個 → RED. impl 階段照 :62-75 的 secretKeyRef pattern 補上 → GREEN.
#
# 凍結 env 名 (見 plan §"軟契約 env 名" 表, 名稱凍結):
#   - AGENT_RUNTIME_AUTH_SECRET_CURRENT
#   - AGENT_RUNTIME_AUTH_SECRET_PREVIOUS
#   - AGENT_RUNTIME_WEBHOOK_SECRET
#   - AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS
#   - AGENT_RUNTIME_NAMESPACE_SECRET
#
# 機制: 純 bash + `helm template` 斷言 (無 helm-unittest 依賴).
# 執行 (host, 非 docker):
#   HELM_BINARY=$(which helm) bash charts/odoo-dev-sandbox/tests/test_agent_runtime_env.sh
#
# Exit 0 = 全部 5 env present (green); Exit 1 = 任一缺 (red) 或 render 失敗.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HELM="${HELM_BINARY:-helm}"

REQUIRED_ENVS=(
  AGENT_RUNTIME_AUTH_SECRET_CURRENT
  AGENT_RUNTIME_AUTH_SECRET_PREVIOUS
  AGENT_RUNTIME_WEBHOOK_SECRET
  AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS
  AGENT_RUNTIME_NAMESPACE_SECRET
)

if ! command -v "$HELM" >/dev/null 2>&1; then
  echo "FIXTURE-ERROR: helm binary not found (set HELM_BINARY). This is a"
  echo "harness problem, not the feature red." >&2
  exit 2
fi

# Render ONLY the odoo deployment template so the assertion is scoped to the
# Odoo pod's env block (the 5 secrets belong on the Odoo container).
VALUES_ARG=()
if [[ -f "$CHART_DIR/values-local.yaml" ]]; then
  VALUES_ARG=(-f "$CHART_DIR/values-local.yaml")
fi

RENDER="$("$HELM" template phase-d-test "$CHART_DIR" \
  -s templates/odoo-deployment.yaml "${VALUES_ARG[@]}" 2>/tmp/helm_render_err)"
RC=$?
if [[ $RC -ne 0 || -z "$RENDER" ]]; then
  echo "FIXTURE-ERROR: 'helm template -s templates/odoo-deployment.yaml' failed" >&2
  echo "(rc=$RC). This is a chart-render harness problem, not the feature red:" >&2
  cat /tmp/helm_render_err >&2
  exit 2
fi

MISSING=()
for env_name in "${REQUIRED_ENVS[@]}"; do
  # match the rendered "- name: <ENV>" env entry
  if ! grep -Eq "name:[[:space:]]+${env_name}([[:space:]]|$)" <<<"$RENDER"; then
    MISSING+=("$env_name")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "RED (F-PC charts): odoo-deployment is missing the frozen agent-runtime"
  echo "Secret env(s):"
  for m in "${MISSING[@]}"; do echo "  - $m"; done
  echo
  echo "Fix: add these as secretKeyRef env entries on the Odoo container in"
  echo "charts/odoo-dev-sandbox/templates/odoo-deployment.yaml, mirroring the"
  echo "postgres-* secretKeyRef pattern at lines ~62-75."
  exit 1
fi

echo "OK: all 5 frozen agent-runtime Secret envs present in odoo-deployment."
exit 0
