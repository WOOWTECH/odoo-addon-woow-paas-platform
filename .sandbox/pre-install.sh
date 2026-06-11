#!/usr/bin/env bash
#
# woow_paas_platform — sandbox pre-install hook (install-time helm values).
#
# 由 woowprd plugin 的通用 create.sh 在 `helm install` 之前執行，傳入：
#   EXTRA_VALUES_FILE  要把追加 helm values 寫進的 YAML 檔（create.sh 會 -f 它）
#   REPO_ROOT / NAMESPACE / SLUG / MODE / DB_NAME / KUBE_CONTEXT / PRIMARY_MODULE
#
# 為什麼需要 install-time hook（不能只用 post-create.sh）：
#   agent-runtime 共享 secret 必須在 helm install 當下寫進 chart Secret（pod env），
#   而那些值要從 operator 的 k8s secret 動態撈 —— 是 install-time、非 post-install。
#   stg networkPolicy / storageClass / registry creds 同屬 install-time chart values。
#
# 這些原本寫死在舊 in-repo create.sh:230-326；外置至此讓通用工具不帶任何 woow 分支。
set -euo pipefail

: "${EXTRA_VALUES_FILE:?pre-install hook 需要 EXTRA_VALUES_FILE}"
MODE="${MODE:-local}"

# 只有 stg 模式需要這些平台 values；local/ci-local 直接結束（no-op）。
[ "$MODE" = "stg" ] || exit 0

# single-quoted YAML scalar emitter（robust to " \ $ backtick；只有 ' 需 double）
yaml_sq() { local v="${1:-}"; v="${v//\'/\'\'}"; printf "'%s'" "$v"; }

# kubectl 沿用 create.sh 已切換的 context（require_kube_context 做過 use-context）。

# --- stg networkPolicy / storageClass（cloudflare tunnel ingress + operator egress + agent-runtime pods）---
{
  echo "networkPolicy:"
  echo "  allowIngressFromNamespaces: [paas-stg-cloudflare-system, paas-stg-system]"
  echo "  allowEgressToNamespaces: [paas-stg-system]"
  echo "  allowAgentRuntimePods: true"
  echo "odoo:"
  echo "  filestore:"
  echo "    persistence:"
  echo "      storageClass: longhorn-delete"
  echo "postgres:"
  echo "  storage:"
  echo "    storageClass: longhorn-delete"
} >> "$EXTRA_VALUES_FILE"

# --- JCR registry creds（避免落 shell history；經 env 傳入）---
if [ -n "${JCR_USER:-}" ] && [ -n "${JCR_PASS:-}" ]; then
  {
    echo "registry:"
    echo "  secretName: jfrog-registry"
    echo "  server: jcr-prod.woowtech.io"
    echo "  username: $(yaml_sq "$JCR_USER")"
    echo "  password: $(yaml_sq "$JCR_PASS")"
  } >> "$EXTRA_VALUES_FILE"
fi

# --- agent-runtime 共享 secret ---
# AUTH(current) + WEBHOOK(webhook-current) 必須 byte-match operator 的 secret，否則
# skill-bundle / state-update webhook 401 → agent_session.state 卡 'absent'。
# NAMESPACE secret 是 Odoo-side only、兼作「啟用 agent-runtime」訊號。
# 未設 AGENT_RUNTIME_NAMESPACE_SECRET → 整段不寫（agent-runtime 不啟用）。
if [ -n "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ]; then
  _ar_op_ns="${PAAS_OPERATOR_NAMESPACE:-paas-stg-system}"
  _ar_op_secret="paas-operator-stg-agent-runtime-auth"
  if [ -z "${AGENT_RUNTIME_AUTH_SECRET_CURRENT:-}" ]; then
    AGENT_RUNTIME_AUTH_SECRET_CURRENT="$(kubectl get secret -n "$_ar_op_ns" "$_ar_op_secret" -o jsonpath='{.data.current}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  fi
  if [ -z "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ]; then
    AGENT_RUNTIME_WEBHOOK_SECRET="$(kubectl get secret -n "$_ar_op_ns" "$_ar_op_secret" -o jsonpath='{.data.webhook-current}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  fi
  if [ -z "${AGENT_RUNTIME_AUTH_SECRET_CURRENT:-}" ] || [ -z "${AGENT_RUNTIME_WEBHOOK_SECRET:-}" ]; then
    # fail-closed：auth 或 webhook 任一解析不到，就整段不寫（含 namespaceSecret）。
    # 半開（只寫 namespaceSecret 啟用 agent-runtime、卻缺 auth/webhook）會讓
    # skill-bundle / state-update webhook 401 → agent_session.state 卡 'absent'。
    # 寧可 agent-runtime 整個關閉（legacy AI chat 不受影響），不要半開漂移。
    # 比照舊 in-repo create.sh stg 段的 fail-fast 語義（不留半套設定）。
    echo "WARN pre-install: 無法解析 agent-runtime auth/webhook secret（operator secret ${_ar_op_ns}/${_ar_op_secret} 缺，或 kubectl context 不對）。" >&2
    echo "WARN pre-install: agent-runtime 維持關閉（fail-closed），不寫任何 agentRuntime values。" >&2
  else
    {
      echo "agentRuntime:"
      echo "  authSecretCurrent: $(yaml_sq "$AGENT_RUNTIME_AUTH_SECRET_CURRENT")"
      echo "  namespaceSecret: $(yaml_sq "$AGENT_RUNTIME_NAMESPACE_SECRET")"
      echo "  webhookSecret: $(yaml_sq "$AGENT_RUNTIME_WEBHOOK_SECRET")"
      [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS:-}" ] && echo "  webhookSecretPrevious: $(yaml_sq "$AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS")"
    } >> "$EXTRA_VALUES_FILE"
  fi
fi
