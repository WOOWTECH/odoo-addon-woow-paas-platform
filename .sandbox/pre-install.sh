#!/usr/bin/env bash
#
# woow_paas_platform — sandbox pre-install hook (install-time helm values).
#
# 由 woow-dev plugin 的通用 create.sh 在 `helm install` 之前執行，傳入：
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

# --- SANDBOX_NEUTRALIZE_LOCKDOWN（#1205；#1162 裁決 D7）------------------------------
# prod clone 彩排沙盒專用：由 #1206 的 seed-from-prod.sh 在 create 前 export。只收 1／0／未設——
# 其他值（true、yes、打錯字）fail-loud：「以為鎖了其實沒鎖」正是這個旗標要防的事。
# 與 woow-dev plugin（cc/woowprd#5 platform mode、#6 stg mode）讀同一個 env、同一個值域。
case "${SANDBOX_NEUTRALIZE_LOCKDOWN:-}" in
  1) LOCKDOWN=1 ;;
  ""|0) LOCKDOWN=0 ;;
  *)
    echo "ERROR pre-install: SANDBOX_NEUTRALIZE_LOCKDOWN='${SANDBOX_NEUTRALIZE_LOCKDOWN}' 不合法（只收 1／0／未設）。" >&2
    exit 1 ;;
esac
if [ "$LOCKDOWN" = 1 ]; then
  # prod clone 只進 stg mode（plugin 內建 chart）或 platform mode（woow-odoo chart；不跑本 hook，
  # lockdown 由 plugin 自己套）。local／ci-local 沒有對應的中和流程 ⇒ 拒絕。
  if [ "$MODE" != "stg" ]; then
    echo "ERROR pre-install: SANDBOX_NEUTRALIZE_LOCKDOWN=1 只支援 stg mode（收到 MODE=${MODE}）；平台 Odoo 彩排請用 --mode platform。" >&2
    exit 1
  fi
  # agent-runtime 會開 paas-managed 雙向 NP 並注入 operator 共享 secret，與「egress 只留 DNS＋同 ns」
  # 語意衝突（#1162 D7）⇒ 同時設就 fail-loud，不默默忽略其中一個。
  if [ -n "${AGENT_RUNTIME_NAMESPACE_SECRET:-}" ]; then
    echo "ERROR pre-install: SANDBOX_NEUTRALIZE_LOCKDOWN=1 不可同時設 AGENT_RUNTIME_NAMESPACE_SECRET（語意衝突，#1162 D7）。" >&2
    exit 1
  fi
fi

# 只有 stg 模式需要這些平台 values；local/ci-local 直接結束（no-op）。
[ "$MODE" = "stg" ] || exit 0

# single-quoted YAML scalar emitter（robust to " \ $ backtick；只有 ' 需 double）
# 3.2 注意：${v//\'/\'\'} 在 bash<4.3 會把反斜線留進輸出 —— 用變數形式跨版本一致。
yaml_sq() { local v="${1:-}" q="'"; v="${v//$q/$q$q}"; printf "'%s'" "$v"; }

# kubectl 沿用 create.sh 已切換的 context（require_kube_context 做過 use-context）。

# --- stg networkPolicy / storageClass / devMode（cloudflare tunnel ingress + operator egress + agent-runtime pods）---
{
  echo "networkPolicy:"
  if [ "$LOCKDOWN" = 1 ]; then
    # lockdown：egress 只剩 DNS（kube-system:53）＋同 ns——關 internet（cc/woowprd#5）、關 agent-runtime
    # pod、兩份 namespace 清單清空（含 ingress 鏡射到 egress 的那段；Helm 對 list 整段取代）。
    # 不能在檔尾再寫一次 networkPolicy 覆蓋：同一份 YAML 重複的頂層鍵會讓 helm 解析失敗。
    # adminPassword 隨機／list_db=False／拒絕 --pgadmin 與 --ttl never 由 plugin 在 lockdown 下處理
    # （cc/woowprd#6）——那些是 create.sh 旗標與 chart 值，hook 看不到。
    echo "  enabled: true"
    echo "  allowInternetEgress: false"
    echo "  allowIngressFromNamespaces: []"
    echo "  allowEgressToNamespaces: []"
    echo "  allowAgentRuntimePods: false"
  else
    # 平時 stg 無條件放行 agent-runtime pod（paas-managed）的問題另見 #1202。
    echo "  allowIngressFromNamespaces: [paas-stg-cloudflare-system, paas-stg-system]"
    echo "  allowEgressToNamespaces: [paas-stg-system]"
    echo "  allowAgentRuntimePods: true"
  fi
  echo "odoo:"
  # stg 是類正式驗證環境：釘 devMode=false。plugin chart 的 default devMode=true 只對本機
  # local sandbox（hostPath 掛 code、改檔即時生效）合理；stg 是打包 image、code 不變，開
  # devMode 只剩副作用且與 prod（devMode off）行為偏離：--dev xml 會從 XML 檔讀 view 而非
  # DB（遮蔽升級後的真實 view 狀態，正是 stg 要驗的東西）、qweb 不快取每次重讀 template、
  # reload 起多餘 file watcher。對齊舊 in-repo values-ci.yaml 原本的 devMode: false。
  echo "  devMode: false"
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
      # 注意：不可寫成 `[ -n ... ] && echo`——PREVIOUS 未設（常態）時該行短路回 1，
      # 作為 group 最後一行會讓整個 script 以非零退出 → create.sh 誤報 hook 失敗
      # （values 其實已寫完；2026-06-12 stg 部署實證的假性失敗）。
      if [ -n "${AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS:-}" ]; then
        echo "  webhookSecretPrevious: $(yaml_sq "$AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS")"
      fi
    } >> "$EXTRA_VALUES_FILE"
  fi
fi
