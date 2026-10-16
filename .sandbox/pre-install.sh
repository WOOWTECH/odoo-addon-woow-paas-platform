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
  # AI 模型分頁（LLM-01b；DESIGN §2.9、§2.11，〔REV〕18）：prod clone 彩排不得注入任何 LLM env——
  # 開了就等於讓一份帶著 prod 密文的 DB 有機會接上 new-api-stg。照上面 AGENT_RUNTIME 的先例 fail-loud。
  if [ -n "${WOOW_LLM_SANDBOX:-}${WOOW_LLM_ROTATE_SECRET_KEYS:-}${WOOW_NEWAPI_PROXY_AUTH_WEB:-}${WOOW_NEWAPI_PROXY_AUTH_CRON:-}${WOOW_LLM_SECRET_KEYS:-}" ]; then
    echo "ERROR pre-install: SANDBOX_NEUTRALIZE_LOCKDOWN=1 不可同時設 WOOW_LLM_SANDBOX 或任何 LLM env（DESIGN §2.9）。" >&2
    exit 1
  fi
fi

# --- AI 模型分頁（LLM-01b）：opt-in WOOW_LLM_SANDBOX=1 ----------------------------------------
# 只收 1／0／未設（同 SANDBOX_NEUTRALIZE_LOCKDOWN：「以為開了其實沒開」與反過來都要 fail-loud）。
# 不寫進 .sandbox.yml 的 config_params_stg：那會對本 repo 所有 stg 沙盒生效（〔REV〕47）。
case "${WOOW_LLM_SANDBOX:-}" in
  1) LLM_SANDBOX=1 ;;
  ""|0) LLM_SANDBOX=0 ;;
  *)
    echo "ERROR pre-install: WOOW_LLM_SANDBOX='${WOOW_LLM_SANDBOX}' 不合法（只收 1／0／未設）。" >&2
    exit 1 ;;
esac
# 加密金鑰輪替（只對既有的 LLM 沙盒有意義）：同一個值域。
case "${WOOW_LLM_ROTATE_SECRET_KEYS:-}" in
  1) LLM_ROTATE=1 ;;
  ""|0) LLM_ROTATE=0 ;;
  *)
    echo "ERROR pre-install: WOOW_LLM_ROTATE_SECRET_KEYS='${WOOW_LLM_ROTATE_SECRET_KEYS}' 不合法（只收 1／0／未設）。" >&2
    exit 1 ;;
esac
if [ "$LLM_SANDBOX" = 1 ] && [ "$MODE" != "stg" ]; then
  echo "WARN pre-install: WOOW_LLM_SANDBOX=1 只在 stg mode 生效（new-api-stg 在 woow-k3s；收到 MODE=${MODE}），略過。" >&2
  LLM_SANDBOX=0
fi

# 只有 stg 模式需要這些平台 values；local/ci-local 直接結束（no-op）。
[ "$MODE" = "stg" ] || exit 0

# single-quoted YAML scalar emitter（robust to " \ $ backtick；只有 ' 需 double）
# 3.2 注意：${v//\'/\'\'} 在 bash<4.3 會把反斜線留進輸出 —— 用變數形式跨版本一致。
yaml_sq() { local v="${1:-}" q="'"; v="${v//$q/$q$q}"; printf "'%s'" "$v"; }

# kubectl 沿用 create.sh 已切換的 context（require_kube_context 做過 use-context）。

# --- AI 模型分頁的 env（LLM-01b；SBX-01 的 odoo.extraSecretEnv）---------------------------------
# 照下面 agent-runtime 段的形狀：從 paas-stg-system 的 Odoo 端簽章金鑰複本讀 web／cron（LLM-01a
# 建立；值只經 stdin／變數、不印），每個沙盒另外有自己的 WOOW_LLM_SECRET_KEYS（不共用）。
# 簽章金鑰任一拿不到或格式不合 ⇒ 整段不寫（fail closed）：env 不存在時 Odoo 端 fail closed、
# post-create 也不會開分頁。值只進 $EXTRA_VALUES_FILE（create.sh 的 0700 暫存目錄，結束即刪）
# → chart Secret → secretKeyRef，不進 ConfigMap 或 annotation。
#
# 🔴 加密金鑰的壽命＝這個沙盒 namespace 的壽命（LLM-01b finding 1）。create.sh 在既有 slug 上走
# helm upgrade（--yes／deploy-stg 換 image）時也會跑本 hook，而 chart Secret 的
# extra-env-woow-llm-secret-keys 是整個覆寫、不是累加：每次重產新金鑰 ⇒ 下一次 pod 重啟後所有
# newapi_password_enc／up_token_enc 都解不開（llm_secret_undecryptable），開戶中的帳戶變 error、
# active 的帳戶 keys/create 與 usage 都 llm_unavailable；new-api 的使用者刪不掉、密碼又沒了，那些
# 名字永遠拿不回來。所以：
#   * 先讀 chart Secret 的現值（跟 create.sh 沿用隨機 admin 密碼同一個作法）；**只有沒有時才產生**。
#   * 輪替：WOOW_LLM_ROTATE_SECRET_KEYS=1 ⇒ 在前面加一把新的、舊的全部保留（MultiFernet：第一把
#     加密、全部可解密；DESIGN §2.9）。
#   * 已是 LLM 沙盒（Secret 裡有這個 key）卻沒帶 WOOW_LLM_SANDBOX=1：照樣沿用（sticky）＋WARN——
#     不帶的話 helm upgrade 會把 extraSecretEnv 整段拿掉，金鑰就此消失。明確帶 WOOW_LLM_SANDBOX=0
#     想關掉：拒絕（照樣沿用）並以非 0 結束；要關請 destroy 沙盒。
#   * 讀不到現值（kubectl 失敗、Secret 不只一份、值格式不合）：有 opt-in ⇒ 不產生新的（可能蓋掉
#     舊的），整段不寫並以非 0 結束；沒 opt-in ⇒ 只 WARN（不讓一般沙盒因為 kubectl 一時失敗就報
#     hook 失敗），訊息提醒 LLM 沙盒的主人帶 WOOW_LLM_SANDBOX=1 重跑。
#   * 現值在、但這次讀不到簽章金鑰：只保留加密金鑰（不放行 new-api-stg；Odoo 端簽章 fail closed、
#     不外呼），密文不會因此變成解不開。
_llm_key_list_re='^[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{32,256}(,[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{32,256})*$'
# Fernet key＝32 bytes 的 urlsafe base64（44 字元、含一個 =）；清單以逗號分隔、第一把加密。
_llm_fernet_list_re='^[A-Za-z0-9_-]{43}=(,[A-Za-z0-9_-]{43}=)*$'
_llm_fernet_re='^[A-Za-z0-9_-]{43}=$'
_llm_new_fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '[:space:]' || true; }
LLM_AUTH_WEB=""
LLM_AUTH_CRON=""
LLM_SECRET_KEYS=""
LLM_WRITE_AUTH=0
LLM_WRITE_KEYS=0
_llm_rc=0

# 現值：chart Secret（odoo-dev-sandbox.extraSecretEnvKey：extra-env-<env 名小寫、_ 換 ->）。
# 用 release label 找（跟 create.sh 的 sandbox_admin_secret_name 同一個找法，不必算 fullname）；
# 只印這一個 key 的 base64（jsonpath），不 dump 整個 Secret。四態：absent／present／error／
# skipped（沒有 NAMESPACE／SLUG 可查；create.sh 一定會給）。
_llm_lookup=skipped
LLM_EXISTING_KEYS=""
if [ "$LOCKDOWN" != 1 ] && [ -n "${NAMESPACE:-}" ] && [ -n "${SLUG:-}" ]; then
  _llm_lookup=error
  if _llm_raw="$(kubectl get secret -n "$NAMESPACE" -l "app.kubernetes.io/instance=${SLUG}" \
        -o jsonpath='{range .items[*]}{.data.extra-env-woow-llm-secret-keys}{"\n"}{end}' 2>/dev/null)"; then
    _llm_found=0
    _llm_b64=""
    while IFS= read -r _llm_line; do
      [ -n "$_llm_line" ] || continue
      _llm_found=$((_llm_found + 1))
      _llm_b64="$_llm_line"
    done <<<"$_llm_raw"
    if [ "$_llm_found" = 0 ]; then
      _llm_lookup=absent
    elif [ "$_llm_found" = 1 ]; then
      LLM_EXISTING_KEYS="$(printf '%s' "$_llm_b64" | base64 -d 2>/dev/null | tr -d '[:space:]' || true)"
      if [[ "$LLM_EXISTING_KEYS" =~ $_llm_fernet_list_re ]]; then
        _llm_lookup=present
      else
        LLM_EXISTING_KEYS=""
      fi
    fi
    unset _llm_raw _llm_b64 _llm_line
  fi
fi

if [ "$LOCKDOWN" != 1 ]; then
  case "$_llm_lookup" in
    present)
      if [ "$LLM_SANDBOX" != 1 ]; then
        if [ "${WOOW_LLM_SANDBOX:-}" = 0 ]; then
          echo "ERROR pre-install: ${NAMESPACE} 已是 AI 模型沙盒（chart Secret 有 WOOW_LLM_SECRET_KEYS）；WOOW_LLM_SANDBOX=0 關不掉它——拿掉 extraSecretEnv 會讓 DB 裡的密文永遠解不開。這次照樣沿用既有設定；要關請 destroy 沙盒。" >&2
          _llm_rc=1
        else
          echo "WARN pre-install: ${NAMESPACE} 已是 AI 模型沙盒：沿用既有的 AI 模型設定與加密金鑰（重跑時請一律帶 WOOW_LLM_SANDBOX=1）。" >&2
        fi
        LLM_SANDBOX=1
      fi
      ;;
    error|skipped)
      if [ "$LLM_SANDBOX" = 1 ]; then
        echo "ERROR pre-install: 讀不到 ${NAMESPACE:-<NAMESPACE 未設>} 既有的 AI 模型加密金鑰（kubectl 失敗、Secret 不只一份、值格式不合或沒有 NAMESPACE／SLUG）——不產生新金鑰（可能蓋掉舊的），AI 模型段整段不寫。修好後帶 WOOW_LLM_SANDBOX=1 重跑。" >&2
        LLM_SANDBOX=0
        _llm_rc=1
      elif [ "$_llm_lookup" = error ]; then
        echo "WARN pre-install: 無法確認 ${NAMESPACE} 是不是 AI 模型沙盒（讀 chart Secret 失敗）；這次不寫 AI 模型段。若它是，修好 kubectl 後帶 WOOW_LLM_SANDBOX=1 重跑，否則加密金鑰會被這次的 helm upgrade 拿掉。" >&2
      fi
      ;;
  esac
fi

if [ "$LLM_SANDBOX" = 1 ]; then
  _llm_ns="${PAAS_STG_SYSTEM_NAMESPACE:-paas-stg-system}"
  _llm_secret="paas-stg-newapi-proxy-client"
  LLM_AUTH_WEB="$(kubectl get secret -n "$_llm_ns" "$_llm_secret" -o jsonpath='{.data.web}' 2>/dev/null | base64 -d 2>/dev/null | tr -d '[:space:]' || true)"
  LLM_AUTH_CRON="$(kubectl get secret -n "$_llm_ns" "$_llm_secret" -o jsonpath='{.data.cron}' 2>/dev/null | base64 -d 2>/dev/null | tr -d '[:space:]' || true)"
  if [ "$_llm_lookup" = present ]; then
    LLM_SECRET_KEYS="$LLM_EXISTING_KEYS"
    if [ "$LLM_ROTATE" = 1 ]; then
      _llm_new="$(_llm_new_fernet_key)"
      if [[ "$_llm_new" =~ $_llm_fernet_re ]]; then
        LLM_SECRET_KEYS="${_llm_new},${LLM_EXISTING_KEYS}"
        echo "pre-install: 加密金鑰輪替：前面加一把新的，舊的保留（Odoo 端重新加密在 LLM-03；確認全部改寫後才移除舊金鑰）。" >&2
      else
        echo "ERROR pre-install: 產生新的加密金鑰失敗，這次不輪替（沿用現值）。" >&2
        _llm_rc=1
      fi
      unset _llm_new
    fi
  else
    LLM_SECRET_KEYS="$(_llm_new_fernet_key)"
    if [ "$LLM_ROTATE" = 1 ]; then
      echo "WARN pre-install: 這是新的 AI 模型沙盒，WOOW_LLM_ROTATE_SECRET_KEYS=1 沒有東西可輪替（只產生一把）。" >&2
    fi
  fi
  # [[ =~ ]] 而不是 printf | grep -q：值不經過任何外部行程的 argv，也不吃 pipefail＋SIGPIPE 的坑。
  if [[ "$LLM_SECRET_KEYS" =~ $_llm_fernet_list_re ]]; then
    LLM_WRITE_KEYS=1
  fi
  if [ "$LLM_WRITE_KEYS" = 1 ] && [[ "$LLM_AUTH_WEB" =~ $_llm_key_list_re ]] \
     && [[ "$LLM_AUTH_CRON" =~ $_llm_key_list_re ]]; then
    LLM_WRITE_AUTH=1
  else
    echo "WARN pre-install: 無法取得 AI 模型分頁的簽章金鑰（${_llm_ns}/${_llm_secret} 缺或格式不合，或 kubectl context 不對）。" >&2
    LLM_AUTH_WEB=""
    LLM_AUTH_CRON=""
    if [ "$_llm_lookup" = present ] && [ "$LLM_WRITE_KEYS" = 1 ]; then
      # 既有沙盒：簽章金鑰這次讀不到，但加密金鑰一定要留著（不然密文永遠解不開）。
      echo "WARN pre-install: 只保留既有的加密金鑰；這次不放行 new-api-stg、Odoo 端簽章 fail closed（不外呼）。" >&2
      _llm_rc=1
    else
      echo "WARN pre-install: WOOW_LLM_SANDBOX 段整段不寫（fail closed）；沙盒照常建立，但沒有 AI 模型分頁。" >&2
      LLM_SANDBOX=0
      LLM_WRITE_KEYS=0
      LLM_SECRET_KEYS=""
    fi
  fi
fi
unset LLM_EXISTING_KEYS

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
    if [ "$LLM_WRITE_AUTH" = 1 ]; then
      # newapi-proxy（ns new-api-stg）：代理那一側的 NP 只收 dev-sandbox.io/managed 的 Odoo pod。
      echo "  allowEgressToNamespaces: [paas-stg-system, new-api-stg]"
    else
      echo "  allowEgressToNamespaces: [paas-stg-system]"
    fi
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
  if [ "$LLM_WRITE_KEYS" = 1 ]; then
    # 沙盒是單一 pod（workers=0、max_cron_threads=1），WEB／CRON 兩組都給（DESIGN §1.4.2-1）。
    echo "  extraSecretEnv:"
    if [ "$LLM_WRITE_AUTH" = 1 ]; then
      echo "    WOOW_NEWAPI_PROXY_AUTH_WEB: $(yaml_sq "$LLM_AUTH_WEB")"
      echo "    WOOW_NEWAPI_PROXY_AUTH_CRON: $(yaml_sq "$LLM_AUTH_CRON")"
    fi
    echo "    WOOW_LLM_SECRET_KEYS: $(yaml_sq "$LLM_SECRET_KEYS")"
  fi
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

# AI 模型段的「非 0 但已寫好 values」（例：明確要求關掉既有 LLM 沙盒、讀不到現值）：create.sh 在
# 非 lockdown 時會印「hook 失敗（繼續）」並照樣套用已寫好的 values——要的就是這個大聲但安全的結果。
exit "$_llm_rc"
