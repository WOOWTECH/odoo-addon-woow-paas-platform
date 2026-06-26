---
title: Prod Agent-Runtime 啟用 Implementation Plan
category: plans
status: draft
created: 2026-06-26
related:
  - docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md
  - docs/guides/deployment/prod-release-sop.md
---

# Prod Agent-Runtime 啟用 Implementation Plan

> **For agentic workers:** 這是 prod 基礎設施變更 plan。每個 Task 結束都有獨立可驗證的 deliverable + rollback。**勿跳過前置 Gate**——其中 G1/G3/G5 需人類提供 secret 值或決策，缺了會 fail-closed。

**Goal:** 在 prod（`paas-system`，Bitnami odoo chart + 獨立 paas-operator chart）啟用 agent-runtime pod 路徑，達到與 stg 等價的端到端能力（spawn pod、skill bundle 載入、WS 串流、§5.5 state-sync webhook）。

**Architecture:** agent-runtime 是多層 fail-closed 系統（見 gap-chain pitfall）。**stg 走 dev-sandbox chart + `create.sh` 自動注入；prod 落點完全不同**——operator 與 odoo 是兩個獨立 Fleet bundle，所有 wiring 走 `paas-app-bundle-cd` repo 的 values + `kubectl` 建 secret。operator 端 **13 個 `AGENT_RUNTIME_*` env 由 operator chart 0.1.16 從 `config.agentRuntime` 自動渲染**（只要加 values 區塊 + 建 2 個 secret）；odoo 端 **5 個 env 走 Bitnami `extraEnvVars` + `secretKeyRef`**（敏感值不進 git）。

**Tech Stack:** Rancher Fleet GitOps、Helm（Bitnami odoo `28.2.10` + paas-operator `0.1.16`）、Kubernetes NetworkPolicy、`paas-app-bundle-cd` repo（git-prod.woowtech.io/woow-paas）。

## Global Constraints

- **operator 由 Fleet 管**：所有變更**必走 CD repo values**，`kubectl patch`/`set env` 會被 Fleet 還原（只能當暫時驗證 bootstrap）。
- **AUTH/WEBHOOK secret 兩端 byte-identical**：operator 的 `current`/`webhook-current` 必須等於 odoo 的 `AUTH_SECRET_CURRENT`/`WEBHOOK_SECRET`，否則 pod 抓 skill-bundle / webhook 回 **401**。
- **無回歸超集 gate**（prod-release-sop §9 坑 #11）：不可把 prod 推回更舊的 operator image/chart。目前 prod operator `0.1.16` / image `20260624.1022` 已與 stg 同版。
- **prod 目前 0 project / 0 agent_session**：平台 wiring 就位後，端到端驗證**需先建第一個 prod project**（`use_agent_runtime=True` + 有 `agent_template`）。
- **NP 改動謹慎**：stg 曾因 `allow-internal` NP 有非-helm field manager → helm conflict → `create.sh` 失敗即 `helm uninstall`+`namespace delete` → **資料全失**（見 gap-chain pitfall HAZARD 段）。prod 非 create.sh，但改 NP 前仍須確認無非-helm field manager 佔用。

---

## 前置 Gate（開工前必須確認，部分需人類提供）

| Gate | 內容 | 取得方式 | 卡關後果 |
|------|------|---------|---------|
| **G1** | `AGENT_RUNTIME_NAMESPACE_SECRET` prod canonical 值 | **生成 prod 專屬**（≠stg，避免同 project 算出撞名 ns）：`openssl rand -hex 32`。Eugene 保管，odoo 端固定注入、cluster 不存 | 缺 → `agent_session._get_namespace_secret()` fail-closed，spawn 前擋 |
| **G2** | agent-runtime image/chart 版本 | 對齊 stg：`imageTag 1.4.2` / `chartVersion 0.4.3` / `probeStyle httpGet`（stg values-stg.yaml 藍本） | 版本 lockstep 錯 → sidecar crashloop |
| **G3** | LLM API key | agent pod **真回應**需要（否則 echo fallback「收到你的訊息」）；同 AI provider 待辦（`ai.config` 3 個皆無 key）。decide 本階段是否一起設 | 缺 → pod 只回 echo，非真 LLM |
| **G4** | 啟用範圍 | 平台 wiring ready 後：`default_enable_agent_runtime=True`（新 project 自動開）vs 逐 project 手動。prod 0 project → 先 ready，第一個 project 建立時驗證 | 設 True 但平台沒 ready → 新 project 落 spawn-fail 態（auto-enable readiness gate 會擋，見 default-agent-template pitfall） |
| **G5** | agent-runtime chart registry 認證 | `paas-operator-agent-runtime-repo` secret 的 `username`/`password`（operator helm install agent-runtime chart 從 paas-charts 拉用）。對照 stg `paas-operator-stg-agent-runtime-repo` 來源 | 缺 → operator spawn 時拉不到 agent-runtime chart |

---

## Task 1: operator 端 agentRuntime wiring

**Files:**
- Modify: `paas-app-bundle-cd/paas-operator/values-prod.yaml`（`config:` 區塊加 `agentRuntime`）
- Create（cluster）: secret `paas-operator-agent-runtime-auth` + `paas-operator-agent-runtime-repo`（`paas-system`）

**Interfaces:**
- Produces: operator pod 13 個 `AGENT_RUNTIME_*` env（chart 0.1.16 渲染）；`current`/`webhook-current` 供 Task 2 odoo 端對齊。

- [ ] **Step 1: 建 auth secret（current + webhook-current）**

值來自 G1 流程同批生成（兩個獨立 32-byte hex）；`current` 給 skill-bundle token 簽章、`webhook-current` 給 state-sync webhook 驗章。

```bash
AUTH=$(openssl rand -hex 32)
WEBHOOK=$(openssl rand -hex 32)
kubectl --context woow-k3s -n paas-system create secret generic paas-operator-agent-runtime-auth \
  --from-literal=current="$AUTH" \
  --from-literal=webhook-current="$WEBHOOK"
# 記下 $AUTH / $WEBHOOK：Task 2 odoo 端 AUTH_SECRET_CURRENT / WEBHOOK_SECRET 要用同值
```

- [ ] **Step 2: 建 repo secret（agent-runtime chart 拉取認證，G5）**

```bash
kubectl --context woow-k3s -n paas-system create secret generic paas-operator-agent-runtime-repo \
  --from-literal=username="<G5 username>" \
  --from-literal=password="<G5 password>"
```

- [ ] **Step 3: values-prod.yaml `config:` 加 `agentRuntime` 區塊**

對照 stg `paas-operator-stg/values-stg.yaml`，**secret 名改 prod**（去 `-stg`）。加在現有 `config:` 區塊內（與 `namespacePrefix`/`paasEnv`/`pvcGracePeriodDays` 同層）：

```yaml
config:
  namespacePrefix: "paas-ws-"
  paasEnv: "prod"
  pvcGracePeriodDays: 90
  agentRuntime:
    imageTag: "1.4.2"          # G2，對齊 stg
    chartVersion: "0.4.3"      # G2，lockstep imageTag
    probeStyle: "httpGet"      # 需 image ≥1.2.0（1.4.2 滿足）
    repoSecret:
      existingSecret: "paas-operator-agent-runtime-repo"   # Step 2 建
    authSecret:
      existingSecret: "paas-operator-agent-runtime-auth"   # Step 1 建
    externalEgress:
      enabled: true            # 開 agent pod NP egress → LLM endpoint（minimax/openai）
    skillRunner:
      enabled: true            # bash sidecar；須與 imageTag 1.4.x + chartVersion 0.4.x 成對
```

> ⚠️ `externalEgress`/`skillRunner` 的精確子鍵以 stg values-stg.yaml 第 37–50 行註解為準（chart 0.1.11/0.1.14 wiring）。複製 stg 區塊後逐鍵核對，勿憑記憶改鍵名。

- [ ] **Step 4: PR → merge main → Fleet reconcile**

```bash
cd /tmp/paas-app-bundle-cd && git checkout -b feat/operator-prod-agent-runtime-enable
# 套用 Step 3 編輯後：
git add paas-operator/values-prod.yaml
git commit -m "feat: prod operator 啟用 agentRuntime（對照 stg 0.4.3/1.4.2）"
git push -u origin feat/operator-prod-agent-runtime-enable
tea pr create --base main --title "feat: prod operator 啟用 agentRuntime" --description "..."
# merge 後等 Fleet reconcile（bundle paas-app-bundle-paas-operator）
```

- [ ] **Step 5: 驗 operator env 渲染 + pod 健康**

```bash
kubectl --context woow-k3s -n paas-system get deploy paas-operator \
  -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}{"\n"}{end}' | grep -i agent
# 期望出現 13 個 AGENT_RUNTIME_*（對照 stg：IMAGE_TAG/CHART/REPO_URL/CHART_VERSION/
#   REPO_USERNAME/REPO_PASSWORD/AUTH_SECRET_CURRENT/AUTH_SECRET_PREVIOUS/
#   WEBHOOK_SECRET/WEBHOOK_SECRET_PREVIOUS/EXTERNAL_EGRESS_ENABLED/SKILL_RUNNER_ENABLED/PROBE_STYLE）
kubectl --context woow-k3s -n paas-system rollout status deploy/paas-operator
kubectl --context woow-k3s -n paas-system exec deploy/paas-operator -- curl -s localhost:8000/health
```

**Deliverable:** prod operator 帶 agentRuntime env、健康；尚未有 odoo 端對齊與 NP，spawn 還不會通（預期）。

---

## Task 2: odoo 端 env 注入（5 env，secretKeyRef）

**Files:**
- Create（cluster）: secret `paas-odoo-agent-runtime`（`paas-system`，5 key）
- Modify: `paas-app-bundle-cd/paas-odoo/values-prod.yaml`（`extraEnvVars` 加 5 條 `secretKeyRef`）

**Interfaces:**
- Consumes: Task 1 Step 1 的 `$AUTH`/`$WEBHOOK`、G1 的 `$NAMESPACE`。
- Produces: odoo pod 5 個 `AGENT_RUNTIME_*` env，供 `agent_session.py` / `agent_runtime_skill.py` / `agent_runtime_webhook.py` 讀。

- [ ] **Step 1: 建 odoo 端 secret（值與 operator 對齊）**

`AUTH_SECRET_CURRENT` 必須 = operator `current`；`WEBHOOK_SECRET` 必須 = operator `webhook-current`（byte-identical，否則 401）。`*_PREVIOUS` 首次留空（rotation 才用）。

```bash
NAMESPACE="<G1 openssl rand -hex 32>"   # prod canonical，Eugene 保管
kubectl --context woow-k3s -n paas-system create secret generic paas-odoo-agent-runtime \
  --from-literal=namespace-secret="$NAMESPACE" \
  --from-literal=auth-secret-current="$AUTH" \
  --from-literal=auth-secret-previous="" \
  --from-literal=webhook-secret="$WEBHOOK" \
  --from-literal=webhook-secret-previous=""
```

- [ ] **Step 2: values-prod.yaml `extraEnvVars` 加 5 條 secretKeyRef**

現有 `extraEnvVars` 只有 `ODOO_SKIP_MODULES_UPDATE`，append：

```yaml
extraEnvVars:
  - name: ODOO_SKIP_MODULES_UPDATE
    value: "yes"
  - name: AGENT_RUNTIME_NAMESPACE_SECRET
    valueFrom: { secretKeyRef: { name: paas-odoo-agent-runtime, key: namespace-secret } }
  - name: AGENT_RUNTIME_AUTH_SECRET_CURRENT
    valueFrom: { secretKeyRef: { name: paas-odoo-agent-runtime, key: auth-secret-current } }
  - name: AGENT_RUNTIME_AUTH_SECRET_PREVIOUS
    valueFrom: { secretKeyRef: { name: paas-odoo-agent-runtime, key: auth-secret-previous } }
  - name: AGENT_RUNTIME_WEBHOOK_SECRET
    valueFrom: { secretKeyRef: { name: paas-odoo-agent-runtime, key: webhook-secret } }
  - name: AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS
    valueFrom: { secretKeyRef: { name: paas-odoo-agent-runtime, key: webhook-secret-previous } }
```

> 為何用 `secretKeyRef` 而非明文 `value`：Bitnami `extraEnvVars` 進 git，明文敏感值會外洩；`secretKeyRef` 只把 secret 名/key 名進 git。

- [ ] **Step 3: PR → merge → Fleet reconcile → odoo pod 滾動**

注意 `updateStrategy: Recreate` + RWO PVC → 舊 pod 須先終止才起新 pod（會短暫 downtime）。

- [ ] **Step 4: 驗 odoo env 注入 + 兩端對齊**

```bash
POD=$(kubectl --context woow-k3s -n paas-system get pod -l app.kubernetes.io/name=odoo -o name | head -1)
kubectl --context woow-k3s -n paas-system exec $POD -- sh -c 'echo NS=${#AGENT_RUNTIME_NAMESPACE_SECRET} AUTH=${#AGENT_RUNTIME_AUTH_SECRET_CURRENT} WH=${#AGENT_RUNTIME_WEBHOOK_SECRET}'
# 三者長度皆 >0
# 兩端 byte-match（不洩明文，比 sha256）：
kubectl --context woow-k3s -n paas-system get secret paas-odoo-agent-runtime -o jsonpath='{.data.auth-secret-current}' | base64 -d | sha256sum
kubectl --context woow-k3s -n paas-system get secret paas-operator-agent-runtime-auth -o jsonpath='{.data.current}' | base64 -d | sha256sum
# 兩個 sha256 必須相同
```

**Deliverable:** odoo 帶 5 env、與 operator auth/webhook byte-match。

---

## Task 3: NetworkPolicy + operator→odoo webhook

**Files:**
- Modify: `paas-app-bundle-cd/paas-odoo/values-prod.yaml`（`networkPolicy.extraIngress`）
- Modify: `paas-app-bundle-cd/paas-operator/values-prod.yaml`（operator `ODOO_BASE_URL` 若未設）

**Interfaces:**
- Consumes: Task 1/2 完成。
- Produces: odoo↔agent pod 雙向、operator→odoo webhook 通。

- [ ] **Step 1: 確認 odoo→agent pod egress（allowExternalEgress）**

Bitnami chart 28.2.10 `networkPolicy.allowExternalEgress` 預設 `true`。先驗 prod 現狀：

```bash
kubectl --context woow-k3s -n paas-system get networkpolicy paas-odoo -o jsonpath='{.spec.egress}' | python3 -m json.tool
# 若為單條「allow all egress」(無 to/ports 限制) → odoo→agent pod:8000 不受限，本步無需改
# 若 egress 被收窄 → values 設 networkPolicy.allowExternalEgress: true
```

- [ ] **Step 2: odoo NP 加 operator→odoo webhook ingress**

operator(`paas-system`)→odoo `:8069` webhook（`/api/agent-runtimes/webhook/state-update`）。用 `networkPolicy.extraIngress`：

```yaml
networkPolicy:
  enabled: true
  extraIngress:
    - from:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: paas-operator
      ports:
        - protocol: TCP
          port: 8069
```

> operator 與 odoo 同 ns（`paas-system`）→ 用 `podSelector` 即可，無需 `namespaceSelector`。對照 stg：stg operator 在 `paas-stg-system`、odoo 在 `dev-sandbox-stg`（跨 ns），prod 同 ns 更簡單。

- [ ] **Step 3: 確認 operator `ODOO_BASE_URL`（webhook 目標）**

```bash
kubectl --context woow-k3s -n paas-system get deploy paas-operator -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep -i odoo_base
# 期望 http://paas-odoo.paas-system.svc.cluster.local:8069（或 http://paas-odoo:8069）
# 若無 → operator values-prod.yaml config 補（對照 stg operator ODOO_BASE_URL）
```

- [ ] **Step 4: 驗 NP 雙向**

```bash
# odoo→agent svc（先有 agent pod 才測，可留到 Task 4）：odoo pod curl agent svc → 404（非 refused/timeout）
# operator→odoo：spawn 後看 operator log httpx POST .../webhook/state-update → 200、odoo log 該 POST → 200（非 401）
```

**Deliverable:** 三方向（odoo→agent、operator→odoo webhook）NP 放行。

---

## Task 4: 端到端驗證（建第一個 prod project）

**Files:** 無（cluster 操作 + odoo shell）

- [ ] **Step 1: 建 prod project + 開 flag + 綁 template**

```python
# odoo shell（paas-system odoo pod）
proj = env['project.project'].sudo().create({'name': 'agent-runtime-prod-verify'})
proj.use_agent_runtime = True   # gap #2
# agent_template：default(id=2) 已 seed + post_init 指向，resolver fallback 生效（gap #3 已緩解）
env.cr.commit()
print('PROJECT:', proj.id, 'use_ar:', proj.use_agent_runtime)
```

- [ ] **Step 2: spawn_or_attach + WS 往返**（recipe 見 memory `agent-runtime-stg-verify` §E2E）

```python
from odoo.addons.woow_paas_platform.services.agent_runtime_client import get_agent_runtime_client
client = get_agent_runtime_client(env)
sess = env['woow_paas_platform.agent_session'].sudo().create({'project_id': proj.id})
env.cr.commit()   # 先持久化再 spawn（避免 timeout rollback → 孤兒 pod）
r = client.spawn_or_attach(proj, 999001, env.user.id, 'owner', proj._resolve_agent_template(), mcp_snapshot={}, skill_snapshot=[])
print('SPAWN:', r.get('pod_state'), 'token_len:', len(r.get('auth_token','')))
```

- [ ] **Step 3: 驗 state-sync webhook + LLM**

```python
# 等幾秒 → fresh odoo shell 查 agent_session.state（absent→spawning→active）
# operator log: POST .../webhook/state-update → 200；odoo log 同 → 200（非 401 = 簽章對）
# WS 往返收 text_chunk：若 G3 設 LLM key → 真 LLM 回應；否則 echo fallback「收到你的訊息」= 全鏈路通
```

- [ ] **Step 4: 清理驗證資源**

```python
# reap + 刪 verify project；operator reap 保留空 ns（inert）
```

**Deliverable:** prod agent-runtime 端到端閉環（spawn→WS→webhook state-sync），有/無真 LLM 視 G3。

---

## Rollback

| 層 | rollback |
|----|---------|
| operator | CD revert `paas-operator/values-prod.yaml` 移除 `agentRuntime` → Fleet sync（13 env 消失，operator 回純 cloud-service 模式） |
| odoo | CD revert `paas-odoo/values-prod.yaml` 移除 5 `extraEnvVars` → pod 滾動（`_get_namespace_secret` fail-closed，agent path 自動關，legacy chat 不受影響） |
| NP | revert `extraIngress`；**改前確認 NP 無非-helm field manager**（HAZARD） |
| secret | `kubectl delete secret paas-operator-agent-runtime-{auth,repo} paas-odoo-agent-runtime` |

agent-runtime 全層 fail-closed：移除任一層即停，既有 pod 下次 reap 不再起。**不影響已上線的 /woow、cloud-service、金流**。

## 風險與注意

1. **prod NP 改動 HAZARD**：改前 `kubectl get networkpolicy paas-odoo -o yaml | grep managedFields -A5` 確認只有 helm field manager（無 `kubectl-*` patch 殘留），避免 server-side apply conflict。
2. **兩端 secret 漂移**：每次 rotation 必 lock-step 更新 operator + odoo 兩端，否則 401。建議寫進 SOP。
3. **無回歸超集**：本 plan 不動 operator image/chart 版本（維持 0.1.16），僅加 values 區塊，天然滿足超集 gate。
4. **0 project 限制**：Task 4 須臨時建 project 驗證，驗完清理；正式啟用對象是未來真實租戶。
5. **G3 LLM key 與 AI provider 共用決策**：agent pod 真回應 = legacy AI 助理同一批 LLM 憑證問題，建議一併處理。

## Self-Review 檢核

- [x] 涵蓋 gap-chain 全 6 層在 prod 的對應落點（operator wiring / odoo 5 env / NP / webhook）
- [x] 每個敏感值標明來源/生成方式（非 placeholder，是 infra gate）
- [x] env 清單以實際勘查為準（operator 13 / odoo 5）
- [x] secret 名/key 名一致（operator `current`/`webhook-current`、odoo `auth-secret-current`/`webhook-secret`、repo `username`/`password`）
- [x] Rollback 對每層；fail-closed 不傷既有上線功能
