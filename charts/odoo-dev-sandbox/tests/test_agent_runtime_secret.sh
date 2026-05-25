#!/usr/bin/env bash
# TDD — odoo (i): charts/odoo-dev-sandbox secret PRODUCTION for the frozen
# agent-runtime keys.
#
# The env CONSUMPTION side (odoo-deployment secretKeyRef) already exists and is
# covered by test_agent_runtime_env.sh. This asserts the matching secrets.yaml
# *data keys* are POPULATED from agentRuntime.* values, so the optional
# secretKeyRef actually resolves at deploy (otherwise the env stays unset and
# the agent-runtime path fails closed).
#
# Assert (set):   helm template -s templates/secrets.yaml --set agentRuntime.*
#                 -> the 5 data keys present.
# Assert (unset): no agentRuntime.* values -> keys absent (fail-closed via unset;
#                 unconfigured sandboxes unaffected — current postgres/admin
#                 keys must be untouched either way).
#
# Mechanism: pure bash + `helm template` (no helm-unittest dependency).
# Run (host): HELM_BINARY=$(which helm) bash charts/odoo-dev-sandbox/tests/test_agent_runtime_secret.sh
#
# Exit 0 = green; 1 = red (missing-when-set or leaked-when-unset); 2 = fixture.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HELM="${HELM_BINARY:-helm}"

# secret data keys — MUST match odoo-deployment.yaml secretKeyRef keys.
REQUIRED_KEYS=(
  agent-runtime-auth-secret-current
  agent-runtime-auth-secret-previous
  agent-runtime-webhook-secret
  agent-runtime-webhook-secret-previous
  agent-runtime-namespace-secret
)

if ! command -v "$HELM" >/dev/null 2>&1; then
  echo "FIXTURE-ERROR: helm binary not found (set HELM_BINARY). Harness problem," >&2
  echo "not the feature red." >&2
  exit 2
fi

render() {
  "$HELM" template phase-d-secret-test "$CHART_DIR" \
    -s templates/secrets.yaml "$@" 2>/tmp/helm_secret_err
}

# --- 1. keys PRESENT when agentRuntime values are set ---
SET_RENDER="$(render \
  --set agentRuntime.authSecretCurrent=k-current \
  --set agentRuntime.authSecretPrevious=k-previous \
  --set agentRuntime.webhookSecret=k-webhook \
  --set agentRuntime.webhookSecretPrevious=k-webhook-prev \
  --set agentRuntime.namespaceSecret=k-namespace)"
RC=$?
if [[ $RC -ne 0 || -z "$SET_RENDER" ]]; then
  echo "FIXTURE-ERROR: 'helm template -s templates/secrets.yaml' failed (rc=$RC)." >&2
  cat /tmp/helm_secret_err >&2
  exit 2
fi

MISSING=()
for key in "${REQUIRED_KEYS[@]}"; do
  if ! grep -Eq "^[[:space:]]+${key}:" <<<"$SET_RENDER"; then
    MISSING+=("$key")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "RED (odoo (i)): secrets.yaml does not populate agent-runtime data key(s):"
  for m in "${MISSING[@]}"; do echo "  - $m"; done
  echo
  echo "Fix: add these keys to charts/odoo-dev-sandbox/templates/secrets.yaml"
  echo "data block, populated from .Values.agentRuntime.* (conditional per value)."
  exit 1
fi

# --- 2. keys ABSENT when agentRuntime values are unset (fail-closed) ---
UNSET_RENDER="$(render)"
LEAKED=()
for key in "${REQUIRED_KEYS[@]}"; do
  if grep -Eq "^[[:space:]]+${key}:" <<<"$UNSET_RENDER"; then
    LEAKED+=("$key")
  fi
done
if [[ ${#LEAKED[@]} -gt 0 ]]; then
  echo "RED (odoo (i)): secrets.yaml emits agent-runtime key(s) even when unset"
  echo "(must be conditional on .Values.agentRuntime.*):"
  for l in "${LEAKED[@]}"; do echo "  - $l"; done
  exit 1
fi

echo "OK: 5 agent-runtime secret keys present when set, absent when unset."
exit 0
