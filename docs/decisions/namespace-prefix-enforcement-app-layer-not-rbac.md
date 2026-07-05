---
name: decision-namespace-prefix-enforcement-app-layer-not-rbac
status: accepted
created: 2026-07-04
updated: 2026-07-04
phase: 上線前驗證 gate #284（operator ClusterRole 是否在 RBAC 層 enforce ns prefix）
team: repo-leader（woow-paas-platform）
deciders:
  - repo-leader（本 gate 驗證）
  - docs-leader（上線前接受單層 app guard + 明文記錄傾向）
related:
  - extra/paas-operator/README.md
  - extra/paas-operator/src/services/helm.py
  - docs/decisions/patterns/external-resource-env-token-naming.md
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
  - docs/tracking/followups/follow-ups.md
issues:
  - "#284（本 gate：RBAC 是否 enforce prefix）"
  - "#280（W1 network-layer cross-tenant default-deny NetworkPolicy — 另一層補償控制）"
followup: F-ENV-8
---

# Decision：租戶 namespace prefix 隔離由 **application 層** enforce，RBAC 層**不是** prefix 邊界

> 適用：任何要理解 / 主張 paas-operator 的 `paas-ws-*` 租戶 namespace 隔離「靠什麼守」的人；動 operator ClusterRole（cross-repo `woow-paas-charts`）、動 `validate_namespace`、或評估 admission 層補強時。

## TL;DR（結論）

**RBAC 層並未（也無法）enforce `paas-ws-*` 前綴邊界。** `paas-ws-*` 租戶隔離目前**只由 application 層** `validate_namespace()`（`extra/paas-operator/src/services/helm.py:79-84`）單層守住。operator 的 `ClusterRole` 授予的是**對一組 resource 類型的 cluster-wide verbs（無 `resourceNames` 限制）**，屬 operational 授權，不是租戶隔離邊界。

**上線決策**：接受**單層 app guard + 本文明文記錄**上線；**admission 層（Kyverno/OPA ValidatingWebhook）列為上線後強化候選（follow-up F-ENV-8）**，與 network 層的 #280（default-deny NetworkPolicy）互為縱深補償控制。理由見下方「Defense-in-depth 評估」。

## Ground truth（kubectl 實測證據，woow-k3s，2026-07-04）

反查路徑：SA `paas-system/paas-operator` → `ClusterRoleBinding` → `ClusterRole`（**不猜名字**）。

```
$ kubectl --context woow-k3s get clusterrolebinding -o json \
    | jq '.items[] | select(.subjects[]?.name=="paas-operator") | {crb:.metadata.name, role:.roleRef.name, subjects:.subjects}'
```

| 環境 | ClusterRoleBinding | roleRef (ClusterRole) | subject（SA / namespace） |
|---|---|---|---|
| prod | `paas-operator` | `ClusterRole/paas-operator` | `ServiceAccount paas-operator` @ `paas-system` |
| stg | `paas-operator-stg` | `ClusterRole/paas-operator-stg` | `ServiceAccount paas-operator-stg` @ `paas-stg-system` |

兩個 ClusterRole 的 rules **逐字相同**（chart `paas-operator-0.1.16`，Fleet-managed）。關鍵欄位檢查：

```
$ for cr in paas-operator paas-operator-stg; do
    kubectl --context woow-k3s get clusterrole "$cr" -o json \
      | jq '[.rules[] | select(has("resourceNames"))] | length'
  done
# paas-operator     : 0
# paas-operator-stg : 0
```

- **所有 rule 皆無 `resourceNames`**（兩個 ClusterRole 各 0 條帶 resourceNames）。
- 授權的 resource 類型（RWCU = get/list/watch/create/update/patch/delete，除另註）：
  `namespaces`；core `secrets`/`configmaps`/`services`/`serviceaccounts`/`persistentvolumeclaims`/`pods`(+`pods/log`,`pods/exec`)/`events`；`resourcequotas`；apps `deployments`/`statefulsets`/`replicasets`/`daemonsets`(+`*/scale` get/patch/update)；networking `ingresses`/`networkpolicies`；`policy/poddisruptionbudgets`；`autoscaling/horizontalpodautoscalers`；`coordination/leases`；`batch/jobs`/`cronjobs`；cluster-scoped `persistentvolumes`(get/list/patch/delete)、`storageclasses`(get/list)；**`rbac.authorization.k8s.io/roles`+`rolebindings`（RWCU）**。
- 綁定方式為 `ClusterRoleBinding`（非某 namespace 的 `RoleBinding`）→ 這些 verbs 在**所有** namespace 生效。
- 無任何綁到 operator SA 的 namespaced `RoleBinding`（`kubectl get rolebinding -A` 過濾 subject `paas-operator` 為空）。

**判定：RBAC 層 is NOT enforcing the prefix。** operator SA 在 RBAC 層對 `kube-system`、`paas-system`、`default`、任何他租戶 ns 的上述 resource 都被授權；`paas-ws-` 只是 app 層自我約束。

## 為什麼 K8s RBAC 結構上做不到 prefix 邊界

三條獨立成立的理由（任一條就足以否證「RBAC 能守 `paas-ws-*`」）：

1. **`resourceNames` 是「物件名字」軸，不是「namespace」軸。** `resourceNames` 限制的是被操作**資源物件的 name**（例如某個叫 `foo` 的 pod），跟它落在哪個 namespace 無關。要按 namespace 收斂，RBAC 的唯一手段是把一個 **namespaced `Role`** 用 `RoleBinding` 綁到**特定 namespace**——這是完全不同的機制，跟 `resourceNames` 無關。
2. **`resourceNames` 不支援 wildcard / prefix，且對 `list`/`watch`/`create` 無效。** 就算把它當名字白名單用，也只能列**完整名稱**精確比對（無 `paas-ws-*` 通配），而且對 `create`（物件還沒名字）、`list`/`watch`（跨物件）根本不生效。
3. **operator 必須動態 `create` namespace，per-namespace RoleBinding 不可行。** 租戶 ns 是 runtime 才產生的 `paas-ws-{hash}`（CS = `paas-ws-{ws_hash}`、agent-runtime = `paas-ws-pa-{hash}` / `paas-ws-stg-pa-{hash}`，皆由 `settings.namespace_prefix` 衍生）。名字無法事先列舉去預先在每個 ns 建 `RoleBinding`；而且 `namespaces` 本身是 cluster-scoped resource，「建 namespace」這個權限無法被 namespace-scope。

→ 結論：**namespace-prefix 這種 multi-tenant 邊界，天生只能在 application 層（或 admission 層）表達，RBAC 表達不了。** README `extra/paas-operator/README.md` 原「enforced at both the application and RBAC levels」為**不正確**的安全主張，已於本 gate 校正（見該檔 §Namespace Enforcement / §RBAC Permissions）。

## 現況：app 層單守的實際強度

- **choke-point 設計（正面，2026-07-05 W0 覆核校正）**：`validate_namespace(namespace)` 集中在單一 service 層（`helm.py`，實測 **32** 個 call site，非原記 ~28），API router（`releases.py` / `agent_runtimes.py` / `init.py` / …）都委派這些 service method → 前綴檢查不散落各 endpoint。⚠️ 但「**每個**會 shell-out 的 method 入口都呼叫」原敘述**不精確**：W0 覆核完整 audit 發現 `apply_manifest`（ns 埋在 manifest dict）與 `ensure_registry_secret` 兩個 mutate 入口**原未自守**、`n8n_init._find_secret` 以 raw `_run_command` 繞過 choke-point，當時安全靠 call-site 順序/衍生名而非結構強制。**已於本批 TDD 補齊**（見下方「已修」）。
- **derived-name 路徑（safe-by-construction，但依賴 call-site 紀律）**：agent-runtime 的 `_ensure_namespace` 走 `k8s_service.apply_manifest({...Namespace...})`，namespace 名由 `_names()` 從 `settings.namespace_prefix` **衍生**（`paas-ws-pa-{hash}`），故結構上保證落在前綴內、非 attacker-supplied。安全成立**靠的是每個 call site 都正確衍生名字**，而非一道無條件的結構性強制。
- **單層的風險面**：因 ClusterRole 是 cluster-wide，一旦（a）出現任何繞過 `validate_namespace` 的 mutation 路徑、（b）operator API key 外洩、或（c）injection/SSRF 類 bug 讓 attacker 控到 namespace 參數，**blast radius = 整個 cluster**（含 `kube-system`、他租戶 ns），沒有第二道 backstop。這正是「單層 app guard」與「app guard + admission 縱深」的差別所在。

## Defense-in-depth 評估：admission 層（Kyverno / OPA）

**選項**：加一條 ValidatingWebhook（Kyverno `ClusterPolicy` 或 OPA Gatekeeper `Constraint`），對 subject = operator SA 的請求，deny：
- 對 namespaced resource 的 `create`/`update`/`delete`，當 `request.namespace !~ "^paas-ws-"`；
- `namespaces` 的 `create`/`update`/`delete`，當 `name !~ "^paas-ws-"`；
- **allowlist 例外**：operator 自身系統 ns（`paas-system` / `paas-stg-system`）——因為 operator 對自己 ns 有合法操作（`coordination/leases` leader-election、讀自己的 secret）。

**成本評估（誠實列出，未實測部署）**：
- **allowlist 收斂容易**：所有租戶 ns 共用單一 `paas-ws-` 前綴（CS、agent-runtime 皆衍生自它），故 policy 的 match 條件單純；只需額外放行 operator 系統 ns。此點成本**低**（已由本 gate 查證 ns 前綴一致，見上）。
- **engine 依賴 + cross-repo 落點**：需叢集有 Kyverno/Gatekeeper（若未裝要先裝），且 ClusterRole 與這類 cluster policy 都住在 cross-repo **`woow-paas-charts`** + `paas-app-bundle-cd` Fleet，屬**跨 repo 變更 + stg→prod gate**，非本 repo 單獨可完成。此點成本**中**。
- **回歸風險**：policy 若漏放行 operator 系統 ns 的 leases/secret，會**打斷 operator 自身運作**（leader-election / 啟動）；需在 stg 先驗「`paas-ws-*` 正常放行 + 系統 ns 自我操作放行 + 非前綴 ns 被 deny」三向。

> **需 recruit `woow-paas-charts` 確認**：admission policy 的實際落點、chart 結構、是否已有 Kyverno/Gatekeeper、以及 policy 與現有 `paas-operator` chart 的組裝方式，屬 cross-repo chart 細節——**本文不臆測 chart 內容**，若決定推進 F-ENV-8，先請 `woow-paas-charts` agent 確認上述前提。

## 建議

**上線前：接受單層 app guard**（與 docs-leader 傾向一致），依據：
1. app guard 是**集中式 choke-point**（單一 service 層 ~28 call site），非散落，drift 面小；
2. admission 層是**跨 repo chart 變更 + engine 依賴 + 系統 ns allowlist 驗證**，非零成本，不宜作上線硬阻擋項；
3. 已有**兩條旁路補償控制**降低單層風險：#280（W1 network-layer cross-tenant **default-deny NetworkPolicy**，網路層阻斷跨租戶橫向流量）+ operator API key 認證（`X-API-Key`，非公開端點）。

**上線前的低成本硬前置（✅ 已完成，2026-07-05 W0 覆核 TDD）**：對 operator 全部 mutation 路徑做一次 `validate_namespace` 覆蓋 audit——**完整** audit（非初步）發現三處單層破口並補齊（RED→GREEN，operator unit suite 357 綠零回歸）：① `apply_manifest` 入口按 manifest kind 取 `metadata.namespace`／Namespace-kind 取 `metadata.name` 並 `validate_namespace`；② `ensure_registry_secret` 入口補 `validate_namespace`；③ `validate_namespace` 對空 `namespace_prefix`（`startswith('')` 恆真）fail-closed + `config.assert_env_guard` 開機對空 prefix abort。買回「每個 mutate 入口結構性自守」而非依賴 call-site 紀律——因為單層一旦有洞就無 backstop。

**上線後：F-ENV-8 追蹤 admission 層強化**。若後續實測（recruit `woow-paas-charts` 後）發現 policy 成本確實低（叢集已有 Kyverno、chart 掛載點清楚），可提前落地：它把「隔離正確性」從**依賴每個 call-site 正確衍生/檢查名字**升級為**結構性、對整個 SA 無條件強制**，是 cluster-wide ClusterRole 之下最直接的第二道邊界。

## 關聯

- **#284**：本 gate（結論即本文 TL;DR）。
- **#280**：W1 network-layer default-deny NetworkPolicy — 另一層（網路層）補償控制，與本文（RBAC/app 層）互補構成縱深。
- **F-ENV-8**（`docs/tracking/followups/follow-ups.md`）：admission 層強化追蹤條目。
- 同「共用資源 / 租戶隔離治理」家族：[`external-resource-env-token-naming`](patterns/external-resource-env-token-naming.md)（CF 命名 env token）、pitfall [`cf-ns-orphan-no-account-gc`](../tracking/pitfalls/cf-ns-orphan-no-account-gc.md)（無帳號級反向 GC 的孤兒累積）。
