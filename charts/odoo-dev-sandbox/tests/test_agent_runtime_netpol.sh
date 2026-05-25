#!/usr/bin/env bash
# TDD — odoo NP: let the agent-runtime pod namespace (dynamic paas-ws-pa-{hash},
# labelled paas-managed=true by the operator) talk to Odoo through the
# allow-internal NetworkPolicy.
#
# Both directions are blocked by default and both are needed for e2e:
#   - INGRESS  pod -> odoo : skill-bundle fetch (pod ns not in static allowlist)
#   - EGRESS   odoo -> pod : WS /v1/chat (existing egress excludes RFC1918, and
#                            the K3s pod CIDR 10.42.0.0/16 ⊂ 10.0.0.0/8)
# So when networkPolicy.allowAgentRuntimePods=true the chart must add a
# label-based (paas-managed=true) ingress rule AND an egress rule (the egress
# also serves as the K3s reply-traffic mirror for the ingress, per the existing
# allowIngressFromNamespaces pattern).
#
# Assert (set):   helm template -s templates/network-policy.yaml
#                 --set networkPolicy.allowAgentRuntimePods=true
#                 -> paas-managed appears twice (1 ingress from + 1 egress to).
# Assert (unset): default -> paas-managed absent (existing sandboxes unaffected).
#
# Run (host): HELM_BINARY=$(which helm) bash charts/odoo-dev-sandbox/tests/test_agent_runtime_netpol.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HELM="${HELM_BINARY:-helm}"

if ! command -v "$HELM" >/dev/null 2>&1; then
  echo "FIXTURE-ERROR: helm binary not found (set HELM_BINARY)." >&2
  exit 2
fi

render() {
  "$HELM" template phase-d-netpol-test "$CHART_DIR" \
    -s templates/network-policy.yaml "$@" 2>/tmp/helm_netpol_err
}

# --- 1. label-based rules PRESENT when allowAgentRuntimePods=true ---
SET_RENDER="$(render --set networkPolicy.allowAgentRuntimePods=true)"
RC=$?
if [[ $RC -ne 0 || -z "$SET_RENDER" ]]; then
  echo "FIXTURE-ERROR: 'helm template -s templates/network-policy.yaml' failed (rc=$RC)." >&2
  cat /tmp/helm_netpol_err >&2
  exit 2
fi

COUNT="$(grep -c "paas-managed" <<<"$SET_RENDER")"
if [[ "$COUNT" -lt 2 ]]; then
  echo "RED (odoo NP): allowAgentRuntimePods=true must add a paas-managed=true"
  echo "namespaceSelector on BOTH ingress (pod->odoo) and egress (odoo->pod);"
  echo "found $COUNT 'paas-managed' line(s), expected >= 2."
  echo
  echo "Fix: in charts/odoo-dev-sandbox/templates/network-policy.yaml allow-internal,"
  echo "gate on .Values.networkPolicy.allowAgentRuntimePods an ingress 'from' and an"
  echo "egress 'to' with namespaceSelector matchLabels paas-managed: \"true\"."
  exit 1
fi

# --- 2. ABSENT by default (backward-compat) ---
UNSET_RENDER="$(render)"
if grep -q "paas-managed" <<<"$UNSET_RENDER"; then
  echo "RED (odoo NP): paas-managed rule present even when allowAgentRuntimePods"
  echo "is unset (must be opt-in so existing sandboxes are unaffected)."
  exit 1
fi

echo "OK: paas-managed ingress+egress present when allowAgentRuntimePods=true, absent by default."
exit 0
