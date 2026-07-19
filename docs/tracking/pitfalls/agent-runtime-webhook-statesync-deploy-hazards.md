# Pitfall: agent-runtime §5.5 webhook state-sync 在 stg 接通的 3 個部署層陷阱（含一次 namespace 資料全失）

> 來源：stg session 2026-06-01。in-repo 的 §5.5 webhook code（Tasks 1-5，`feat/agent-runtime-state-sync-webhook` → develop #118）落地後，把它**實際在 stg 接通**（plan Tasks 6-8：重建 operator + 兩端 secret + e2e）時踩到三層部署陷阱。其中第 2 條一次刪光 `dev-sandbox-stg` 的 DB。
>
> 這是 [[agent-runtime-stg-enablement-gap-chain]] 的**第 6 層**（前 5 層讓 *pod round-trip* 通；本篇讓 *operator→Odoo 狀態回寫* 通）。webhook 的 code/設計與 6 條延後項見 `docs/tracking/followups/follow-ups.md` 的 `F-ASW-1~6`、plan `docs/archived/superpowers/plans/2026-06-01-agent-runtime-state-sync-webhook.md`、架構 `docs/guides/development/agent-runtime-chat-architecture.md`。本篇只記**部署/環境層**踩雷，不重複 code 細節。

## Symptom

- in-repo webhook code 已 merge（operator `notify_state_update` 從 `spawn_or_attach`/`reap` 經 `BackgroundTasks` 發；Odoo receiver `agent_runtime_webhook.py` 早在），但 stg 上 `agent_session.state` **仍卡 `absent`**。
- 一次「重新部署既有 stg」直接讓 **整個 `dev-sandbox-stg` namespace 消失**（pod / DB / PVC 全無），postgres 資料遺失。
- 恢復後 spawn 觸發：operator log 有 `httpx POST .../webhook/state-update`，但 Odoo log 對同一 POST 回 **401**（不是 200）→ state 還是不動。

## Why — 3 個部署層陷阱

### 陷阱 1（gap-chain 第 6 層）：operator→Odoo 的 NetworkPolicy 是單向的

前 5 層只開了 **Odoo ↔ agent-pod**（`networkPolicy.allowAgentRuntimePods=true`，用 `paas-managed=true` namespace selector 開雙向）。但 §5.5 webhook 是 **operator（`paas-stg-system`）→ Odoo（`dev-sandbox-stg`）** 的 POST，這條從沒開過：

- Odoo 的 `allow-internal` ingress 只放 `kube-system` / `ingress-nginx` / `paas-stg-cloudflare-system` / `paas-managed=true`，**不含 `paas-stg-system`**。
- Odoo egress 早有開到 `paas-stg-system`（`allowEgressToNamespaces`，給 Odoo→operator 的 spawn 呼叫用），但**反向 ingress 沒有** → operator 的 webhook POST 被 K3s NP 回 **RST（curl exit 7）**。
- 同 [[agent-runtime-stg-enablement-gap-chain]] 的「refused ≠ timeout」陷阱：K3s NP enforcer 回 RST 不是 drop，**別用「refused 所以不是 NP」排除嫌疑**。

**修**：`create.sh` stg mode 把 `paas-stg-system` 加進 `networkPolicy.allowIngressFromNamespaces`（chart `network-policy.yaml` 的 `range` 會自動鏡像 egress）。operator 知道 Odoo URL 靠 operator 端 env `ODOO_BASE_URL`（stg 指 internal svc:8069）。

**驗**：從 operator pod `curl -X POST .../api/agent-runtimes/webhook/state-update` → 應回 `401`（空簽名被拒 = 連得到），不是 `exit 7`（被 NP 擋）。

### 陷阱 2（HAZARD — 資料全失）：create.sh 失敗的 helm **upgrade** 會刪 namespace + 資料

重新部署既有 stg（`deploy-stg.sh --image` 走 `create.sh` 的 upgrade 路徑）時：

1. `allow-internal` NP 上殘留一個 **`kubectl-patch` field manager**（先前手動 `kubectl patch`/`set`/`edit` 那條 helm 管的 NP 留下的）佔住 `.spec.egress`/`.spec.ingress`。
2. helm upgrade 的 server-side apply 與該 field manager **衝突** → `UPGRADE FAILED: conflict ...`。
3. `create.sh` 的失敗清理把它當成失敗的安裝處理 → `helm uninstall` + **`namespace delete`**。
4. PV 是 `longhorn-delete` reclaim policy → namespace 一刪，postgres + filestore volume 連同資料**一起刪掉**（`helm.sh/resource-policy: keep` 的 filestore PVC 也擋不住 namespace cascade delete）。

**為什麼難預期**：觸發點是「**很久以前**對 helm 管的資源做過一次手動 `kubectl patch`」——那次 patch 當下無害，但它在資源上留下 `kubectl-patch` field manager，**下一次 helm upgrade 才引爆**。而 create.sh「失敗即刪 namespace」對 *upgrade* 來說過度暴力（對 *fresh install* 失敗清理才合理）。

**避免 / 復原**：
- **別**對 helm 管理的資源（NP / deployment / secret …）做臨時 `kubectl patch`/`set env`/`edit`——它會污染 field manager，下次 helm upgrade 衝突。非做不可時用 `kubectl apply --server-side --force-conflicts`，或事後讓 helm 重新接管。
- upgrade 前可先查：`kubectl get netpol <name> -o yaml | grep -A2 managedFields`，看有沒有非 `helm` 的 manager。
- **復原解**：**全新 install**（namespace 不存在 → 沒有殘留 field manager → 無衝突）。本次即靠 fresh install 重建（DB 重新初始化、舊測試資料接受遺失）。
- create.sh 應考慮：upgrade 失敗時**不要**自動刪 namespace（只在 fresh install 失敗才清），或保留 data PVC。（follow-up 候選）

### 陷阱 3：webhook secret 漂移（暫時值 vs canonical）+ create.sh 探索錯 operator

兩個成因疊加讓「兩端都有 secret」卻仍 401：

- **漂移**：接通過程一度用 `kubectl set env` 給 operator 灌一把**暫時** webhook secret，Odoo 也用同把。但持久化的 **canonical** 值是 operator auth secret `paas-stg-system/paas-operator-stg-agent-runtime-auth` 的 `webhook-current` key（由 CD `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` chart 0.1.12 wire）。**Fleet 一 sync 就把 operator 換回 canonical**，Odoo 卻還是暫時值 → HMAC 對不上 → 401。教訓：operator 端的 kubectl bootstrap 是 **Fleet 暫時態**，以 CD / cluster secret 為**單一真實來源**，Odoo 必須對齊它（不是反過來）。
- **探索錯 operator**：`create.sh` 全新安裝的憑證 seeding 自動探索 operator，`OPERATOR_NS` 預設一律 `paas-system`（連 stg 也是）→ stg 被 seed 成**非-stg** operator 的 `operator_url`/`api_key`（沒有 stg 的 webhook secret、namespace prefix 也錯）。

**修**：
- 對齊 secret：把 operator `webhook-current`（base64）直接 copy 到 Odoo secret `stg-odoo-dev-sandbox-secret` 的 `agent-runtime-webhook-secret`（不解密、不落地）+ `rollout restart` odoo。
- 根治：`create.sh` stg mode **自動從 operator 的 canonical secret 讀 auth+webhook 注入**（部署者只需帶 `AGENT_RUNTIME_NAMESPACE_SECRET`，其餘自動對齊、不漂移；取不到則 helm 前 fail-fast）+ `OPERATOR_NS` 預設改 mode-aware（stg → `paas-stg-system`）。見 PR #119（`feat/create-sh-autosource-agentruntime-secrets`）。

## How to avoid / 接通與除錯 checklist

要在 stg 接通 §5.5 webhook（讓 `agent_session.state` 跟 pod 生命週期同步），前 5 層（[[agent-runtime-stg-enablement-gap-chain]]）通之外，這 3 層也要到位：

1. **operator image 含 notify code**：`webhook_dispatcher.notify_state_update` 存在且 `agent_runtimes.py` 有 caller（`spawn_or_attach`/`reap` 經 `BackgroundTasks`）。operator 走 `operator-v*` tag → CI → `paas-app-bundle-cd` Fleet（**不是** deploy-stg）。`kubectl exec deploy/paas-operator-stg -- grep notify_state_update /app/src/api/agent_runtimes.py`。
2. **webhook secret 兩端同值（canonical）**：operator 從 `paas-operator-stg-agent-runtime-auth/webhook-current` 讀；Odoo 端對齊同值（PR #119 後 create.sh 自動對齊）。對不上的症狀：operator log 有 webhook POST、但 Odoo log 該 POST 回 **401**（非 200）。
3. **operator→Odoo NP ingress**：Odoo `allow-internal` ingress 含 `paas-stg-system`。實測：operator pod curl Odoo webhook endpoint → `401`（可達）非 `exit 7`（被擋）。
4. **operator 設定正確**：Odoo 的 `ir.config_parameter` `operator_url` 指 `paas-operator-stg.paas-stg-system...`（非 `paas-system`），`operator_api_key` = `paas-operator-stg-secret/api-key`。

**端到端驗**：建 project（`use_agent_runtime` 可 sudo write）→ `client.spawn_or_attach`（odoo shell）→ 幾秒後 fresh odoo shell 查 `agent_session.state`（web worker 收 webhook commit；跨 transaction 要重讀）→ 應 `absent → spawning`（cold-start）/ `active`（attach）；`reap` → `reaped`。operator log + Odoo log 雙端該 POST 都 `200`。

**動既有 stg 前**：先確認 `allow-internal` NP 沒有非-helm field manager（陷阱 2）；不確定就走 fresh install 而非 upgrade。
