---
title: 租戶網路隔離 operator 實作規格（#280 baseline default-deny + #279 agent→CS 雙腿 wiring）
status: draft
date: 2026-07-04
owner: woow-paas-platform (repo leader)
reviewers: docs-leader, woow-paas-charts
related: ["#280", "#279", "#284", "PR #288"]
supersedes_assumption: 「kube-router 全毀、只能 ipBlock/換 CNI」（已由 PR #288 更正）
---

# 租戶網路隔離 operator 實作規格（#280 + #279）

> **前提（已鎖死，見 PR #288）**：kube-router netpol 在 woow-k3s **正常運作**——`default-deny` / `ipBlock` / `namespaceSelector` / `podSelector` / egress 全對 **established pod** 生效。唯一 caveat = 新建 pod 啟動空窗（數秒）。故本 spec 走**標準 native selector**，非 ipBlock 全包、非換 CNI。
>
> **範圍**：跨租戶洞目前只在 **stg**（prod 尚空）→ 上線前 W1 強化，非緊急事件。
>
> **⚠️ 部署硬前置**：ns 全域 default-deny 上線前，must-allow 清單（§6）**必須用 established-pod 方法（PR #288「如何正確驗證 NP」）在 stg 實測驗過**，不憑推論。default-deny 打錯會讓 kubelet probe / cloudflared→app / DNS 全斷。

## 1. 目標

1. **#280 止血**：租戶 ns 從「fail-open」變「fail-closed baseline」——operator 建每個租戶 ns 即 stamp ingress `default-deny` baseline（chart-independent，兜住 n8n 這類**零 NP** 的 app pod），並收緊 CS 服務的 from-less allow。
2. **#279 接通**：agent→CS-MCP 單一路徑的兩腿都 wire 上（腿1 agent egress workspace-id、腿2 CS ingress agent-ns），且**跨 workspace 仍 deny**。
3. **兩者 lock-step**（`two-layer-acl-lockstep`）：#280 收緊腿2、#279 wire 腿1，必須一起 land，否則腿2 維持全開或路不通。

## 2. 雙腿單路徑模型（詳見 PR #288）

| 腿 | 方向 | chart 機制 | wiring 需求 |
| --- | --- | --- | --- |
| 腿1 = agent egress | agent pod → CS ns | agent-runtime `egressWorkspaceIds` → `egress namespaceSelector{woow.tw/workspace-id:<id>}`（整段 `{{- if egressWorkspaceIds }}` gated）| (a) operator 送**非空** `egressWorkspaceIds` **且** (b) **CS ns 帶 `woow.tw/workspace-id:<id>` label** |
| 腿2 = CS ingress | agent ns → mcp-server pod | mcp-server `ingressNamespaces` → `ingress namespaceSelector{kubernetes.io/metadata.name:<agent-ns>}` | 平台在 CS deploy values 填 **agent ns 名** |

## 3. Operator 端改動（`extra/paas-operator`，本 repo）

| # | patch 點 | 現況 | 改動 |
| --- | --- | --- | --- |
| O1 | `services/helm.py:108 build_managed_ns_labels()` | 不吃參數，只印 `paas-managed=true` + `paas.woowtech.io/env` | 加 `workspace_id: Optional[int] = None`；非 None 時加 `woow.tw/workspace-id=<id>`（經 `validate_label_value` 驗）。None → 不加（向後相容）|
| O2 | `services/helm.py:737 create_namespace(...)` | 建 CS ns + quota + registry secret；label 用 `build_managed_ns_labels()`；**無 default-deny** | ① 加 `workspace_id` 參數傳給 O1 ② ns apply 後呼叫新 helper `_apply_baseline_netpol(name)`（O4）|
| O3 | `api/agent_runtimes.py:81 _ensure_namespace(namespace)` | apply 帶 label 的 agent ns；label 用 `build_managed_ns_labels()`；**無 default-deny** | ① 加 `workspace_id` 參數（來源見 Q2）傳給 O1 ② apply 後呼叫 `_apply_baseline_netpol(namespace)` |
| O4 | **新** `_apply_baseline_netpol(name)`（HelmService/KubernetesService）| — | idempotent `kubectl apply` 一組 baseline NP：見 §4 |
| O5 | `models/schemas.py:95 NamespaceCreateRequest` | 只有 name/cpu/mem/storage | 加 `workspace_id: Optional[int] = Field(None, ...)` |
| O6 | `api/namespaces.py:22 create_namespace` endpoint | 呼叫 `k8s_service.create_namespace(name, cpu, mem, storage)` | 多傳 `workspace_id=request.workspace_id` |

> **CS deploy values（leg-2）不在 operator hardcode**：`api/releases.py:39 create_release` 把 `request.values` passthrough（只 setdefault imagePullSecrets）→ mcp-server `ingressNamespaces` 是**平台端**塞進 values 的（現在沒塞 → chart 預設 `[]` → from-less）。故 leg-2 主要是平台端修（P2），operator 端零改動（頂多加個防呆：CS chart 若無 ingressNamespaces 就 log warn，不強制）。

## 4. Baseline NetworkPolicy 設計（O4）

K8s NP 是**跨 NP allow-union**：baseline default-deny（`podSelector:{}`）選中 ns 內**所有** pod → 全 ns ingress fail-closed；chart 自己的 allow NP（app↔pg podSelector、app↔mcp）**疊加放行**其流量，故 **不會弄斷 chart-managed 服務**（對 established pod）。真正被 baseline 擋住的是**零 chart-NP 的 pod（n8n）**的外來 ingress。

因此 baseline 需**配套 allow**（否則 n8n 的 cloudflared→app 也被擋）：

```yaml
# 1) 全 ns ingress default-deny（fail-closed 底）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: paas-baseline-default-deny, namespace: <ns>}
spec: {podSelector: {}, policyTypes: [Ingress]}
---
# 2) 同 ns 內部互通（app↔pg↔mcp，兜住無 chart-NP 的 app）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: paas-baseline-allow-samens, namespace: <ns>}
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress: [{from: [{podSelector: {}}]}]   # 同 ns 全放行（established 生效、cross-ns 仍 deny）
---
# 3) 公開入口：shared tunnel → app（per-tenant cloudflared sidecar 走 rule#2 same-ns；此條僅為
#    用 shared 環境 tunnel 的 CS。stg=paas-stg-cloudflare-system / prod=paas-cloudflare-system，§6 實測確認）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: paas-baseline-allow-ingress-gateway, namespace: <ns>}
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
  - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-stg-cloudflare-system}}}]
```

> **設計選擇**：baseline 只做 **Ingress**（不做 ns 全域 egress default-deny）——egress SSRF 收斂交給 **per-chart** egress 規則（agent-runtime / CS 各自 chart 的 egress + ipBlock except），避免一刀切 egress 打斷 DNS/LLM/外部 API。若要 ns 全域 egress default-deny，須額外把 DNS→kube-system:53 + 各 app 的外呼 except 全列進 baseline（§6 未涵蓋、暫不做）。
>
> **啟動空窗（實測 ≈ 個位數秒）**：baseline 對**新建** app pod 有短暫空窗——實測 ingress-allow ≈ 1s、egress-deny ≈ 2s（PR #288「啟動空窗實測」）。屬 hardening note，非 blocker；關鍵隔離別假設「pod 剛啟動就已 fail-closed」。must-allow 驗證的 `sleep 45` 是相對此窗口的保守裕度。

## 5. 平台端改動（Odoo `src/`，本 repo）

| # | 位置 | 改動 |
| --- | --- | --- |
| P1 | CloudService 呼叫 operator `POST /api/namespaces` 處 | request 帶 `workspace_id`（該 CS 所屬 workspace）|
| P2 | CloudService deploy 建 CS chart values 處（送 `POST /api/releases`）| ① mcp-server 類：`values["mcp-server"]["ingressNamespaces"] = [<該 workspace 的 agent ns 名>]`（leg-2）② Bitnami odoo/pg 類：`allowExternal: false`（原生 podSelector）|
| P3 | agent spawn 呼叫（`SpawnOrAttachRequest`）已帶 `project_workspace_ids`（schemas.py:399）| 確認 operator `_ensure_namespace` 能拿到**該 agent 自己 project 的 workspace_id**（Q2）|

## 6. must-allow 清單 —— 部署 baseline 前**必用 established-pod 方法實測驗過**

> 方法：`kubectl run probe --image=curlimages/curl --command -- sleep 3600` → `wait Ready` → `sleep 45`（跨空窗）→ `kubectl exec probe -- curl ...`。**禁用「起 pod 即 curl」臨時 pod**（假訊號）。

**✅ stg 實測結果**（2026-07-04，established-pod 方法，test ns `mustallow` 套 baseline default-deny + same-ns allow）：

| 路徑 | 結果 |
| --- | --- |
| **kubelet probe（node→pod）** | ✅ app 帶 readinessProbe 在 default-deny 下 **Ready=True** → kube-router 對 node/LOCAL 來源放行、**default-deny 不擋 kubelet probe、無需 carve-out**（最 critical，過了）|
| **same-ns app↔pg / app↔mcp** | ✅ established `sns→pg=200`、`sns→mcp=200`（baseline + same-ns allow 疊加放行）|
| **cross-ns negative** | ✅ 無關 ns established probe→app = **blocked（000）** → baseline 確實擋跨租戶 |
| **cloudflared → app** | ⚠️ 部分解：cloudflared **多為 per-tenant 同 ns sidecar**（如 `bnidistrict-cloudflared` 在 `bnidistrict-odoo` ns）→ **rule#2 same-ns 已覆蓋**；**shared 環境 tunnel ns = `paas-stg-cloudflare-system`**（prod `paas-cloudflare-system`）→ rule#3 目標。**須逐 CS 確認用 per-tenant sidecar 還是 shared tunnel** |
| DNS → kube-system:53 | ➖ N/A（本 spec 不做 egress baseline）|
| Odoo ↔ agent WS:8000 | ➖ chart 自管（F-PILOT-5 `odooNamespace`）|

## 7. Open design questions（誠實標出，需 docs-leader / 平台決策，勿讓 impl 硬猜）

- **Q1（leg-2 機制 + 跨 workspace 語意，最關鍵）**：mcp-server 該放行哪些 agent？自然身分是 **project↔CS 綁定**（`project.cloud_service_ids`），非「agent 的 workspace」。且 data model（`project_project.py:141-149`）顯示 **project↔workspace 從 CS 綁定推導、技術上多值**：`project.workspace_ids = cloud_service_ids.mapped('workspace_id')`（= leg-1 `egressWorkspaceIds` 來源、可跨 workspace）；`workspace_id` primary = 第一個綁定 CS（id ASC）的 workspace、arbitrary。**無 constraint 強制「一 project 的 CS 同 workspace」**（multi-bind 明確支援）。CS 側則單值（`cloud_service.workspace_id`，cloud_service.py:62）。選項：
  - **(A) ns-name 枚舉**：CS mcp-server `ingressNamespaces` 列出綁定 project 的 agent ns 名。**invariant-independent、永遠正確**，但綁定變動要 reconcile（agent ns lazy 建、且綁定增減）。
  - **(B) 單一 workspace-id label 對稱**：mcp-server 加 `ingressWorkspaceIds`（charts 評估低成本）、CS ns 與 agent ns 各 stamp 自身 `woow.tw/workspace-id`、吃 label → **免枚舉**。**但只在「project 綁的 CS 全同 workspace」才成立**——agent ns 單一 label 表達不了跨 workspace（multi-bind 會 silently 漏放行 non-primary workspace 的合法 agent）。現實多半成立（project 通常單租戶），但**無 enforced invariant 兜底**。
  - **✅ Eugene 決策：Model B（保留跨-workspace 綁定）** → **不加 same-workspace constraint**；leg-2 走 **(B') per-reachable-workspace label**：agent ns 標多個 `woow.tw/reach-ws-<W>`、mcp-server 選自身 workspace 那把 key（免枚舉、空綁定=deny-all）。final shape 由 docs-leader ultracode workflow validated 後定案，再落 O1/O3 + charts leg-2。
- **Q2（agent ns label 來源）→ Model B 定案：per-reachable-workspace 多 label**：agent ns 標**每個可達 workspace 一把** `woow.tw/reach-ws-<W>`，來源 = `project.workspace_ids`（可達集合，= egressWorkspaceIds 源，`agent_runtime_client.py:126`）。非單一 workspace-id（lossy 已排除）。O3 `_ensure_namespace` 須拿到 `project.workspace_ids` 逐一 stamp（empty=不標任何 reach-ws label=deny-all，符合 opt-in）。
- **Q3（baseline 對既有 stg ns 的回填）**：現存 stg 租戶 ns 無 baseline → 要不要一次性 backfill（對現存 ns apply baseline NP + 補 workspace-id label）？backfill 若漏 must-allow 會打斷現有服務 → 須先 §6 驗過再 backfill。

> **stg 實測定調（2026-07-04，leader `multi_ws=0/12` + docs-leader 雙查收斂）**：stg 12 個 agent project **全部 `cloud_service_ids=[]`/`workspace_ids=[]`、0 跨-workspace**（5 CS 有 ws 但 0 綁 project）→ 跨-ws 綁定**目前零使用**，但 **Eugene 拍板 Model B**（保留「一 project 可綁跨 workspace CS」產品功能，agent 須同時達 ws-A 與 ws-B 的 CS）→ **不加 same-workspace constraint**。**leg-2 必須處理『可達集合』**：候選 shape = agent ns 標 `woow.tw/reach-ws-<W>` per reachable W、mcp-server 選自身 workspace 那把 key（免枚舉、空綁定=deny-all）；docs-leader 正以 ultracode 對抗式 workflow 驗證，validated shape + spec-delta 落地後才動 O1/O3。**Q2 隨之**：agent ns 標**多個** `woow.tw/reach-ws-<W>`（每個可達 workspace 一把），非單一 workspace-id。
>
> **binding = agent→CS reachability 的 opt-in 前置**（重要含意）：0 綁定 = 所有 agent 的 `egressWorkspaceIds` 空 = leg-1 CS-egress 規則不 render（= 現況「agent→CS 授權幾乎沒生效」真根因＝0 綁定、**非控制器**）。收斂後 agent 要達某 CS **必須先綁 CS 到 project**。確認：
> - **(a) binding flow 存在**：`/api/support/cloud-services/<id>/project`（auth=user，`_check_workspace_access` gated）建 project 時綁 CS。**但 `_update_project` 只改 name/desc、無 rebind UX** → 既有 project 要新增 CS reachability 目前**無使用者路徑**（#280 rollout UX 待補）。
> - **(b) base egress 收斂後仍開（已確認 operator values 獨立）**：`_build_install_values`（agent_runtimes.py:143/160/178）分送 `egressWorkspaceIds`/`odooNamespace`/`externalEgress.enabled` **三獨立 key** → 空 `egressWorkspaceIds` **只**移除 CS-workspace egress，DNS（chart 預設）/Odoo WS/外部 LLM **不受影響**、agent 不全斷。（chart 端請 charts 複核 DNS/external 規則確非 gated 於 egressWorkspaceIds。）

## 8. Rollout 順序（lock-step）

1. **§6 must-allow 在 stg established-pod 實測全綠**（硬 gate）。
2. **決 Q1（推薦 B 對稱化）+ Q2**。
3. #280 O1–O6 + P2 Bitnami `allowExternal:false` + baseline（單環境 stg 先行）。
4. #279 腿1（P1 workspace_id + O1 label）+ 腿2（P2 ingressNamespaces 或 Q1-B 的 workspace-id label）**同批 land**。
5. stg established-pod 驗：跨租戶 curl→擋、同 workspace agent→CS→通、跨 workspace agent→CS→擋。
6. **獨立 replication 鎖死**（docs-leader 提議）後再議 prod。

## 9. 測試

- operator 單元：`build_managed_ns_labels(workspace_id=...)` label 正確 + 非法值 raise；`create_namespace` 產出含 baseline NP manifest。
- integration（real k8s，`-m integration`）：建 ns → 驗 baseline NP 存在 + workspace-id label 存在 + established cross-ns 被擋 / same-ns 通。
- e2e（deploy-stg + established-pod）：§8.5 三條路徑。
- **禁用臨時 pod 斷言**（PR #288 教訓）。

## 10. 不做 / 非目標

- 不做 ns 全域 egress default-deny（egress 收斂留 per-chart）。
- 不動 CNI、不 ipBlock 全包。
- 殘留 bypass（hostNetwork / NodePort / RWX PV / default SA token / 啟動空窗）非本 spec，歸 #284 admission/PSA 線。

## 11. 實作對抗驗證 findings（implementation-grounded workflow，2026-07-04）

獨立 implementation-grounded 對抗 workflow（5 skeptic 讀真實 operator/平台 code 攻破 (B')），與 docs-leader design-level workflow 互補。**結論：(B') 靜態雙腿設計健全，但動態 reconcile 面整條缺失 → 非 implementation-ready，需前置 spec-delta。**

| # | severity | 發現 | spec-delta |
| --- | --- | --- | --- |
| **F1** | 🔴 **BLOCKER** | 綁定變動後**零 reconcile**：`sync_workspaces`(agent_runtime_client.py:180) **零 production caller**（已 grep 驗）、`project`/`cloud_service` 無 `write()` override、attach/resume/reap 皆不重算 NP → unbind 後 agent egress 凍在舊快照、**整個 release 生命週期可達已解綁 workspace 的 CS MCP**。對照 `_push_snapshot_to_running_pods`(agent_session.py:530) 有 skill/mcp hot-push 卻無 workspace 對應 push = 漏建鐵證。 | **O4** `project.write()` hook 活化死碼 `sync_workspaces`（同交易、fail-loud、冪等）；**O5** `cloud_service._do_cleanup`/`unlink` 對 running pod 同步 sync；**O6** sync endpoint 擴成同時撤 leg-2 reach-ws label |
| **F2** | 🟠 | leg-2（reach-ws ingress）**整條未實作**，agent ns label cold-start-only、**無 post-spawn ns-label patch 原語** → 比 leg-1 更 stale | **O1** stamp label；**O2**(charts) selector；**O3** ns-label patch 路徑 |
| **F3** | 🟠 **GATING** | CS ns **現無 workspace-id label** → 若 chart leg-1 egress 依賴該 label，leg-1 現在就 deny-all | 落 O1 前**先勘查 woow-paas-charts NP template 實際比對哪個 label key**（byte-identical 前提）|
| **F4** | 🟠 | 跨 repo label-key 無單一真源 + 無 `validate_label_key`（只造斷路非洩漏；W 恆正整數 fail-closed）| operator 算好完整 key 明文傳 chart + cross-repo 契約測試 + 補 `validate_label_key` |
| **F5** | 🟡 **已修正** | fresh-pod 窗（見下方自我修正）| leg-1 不可省寫進 `two-layer-acl-lockstep`；data-plane MCP retry ≥3s |

**★ 對自身 workflow 的 F5 修正（對抗自驗）**：skeptic 稱「fresh agent egress 空窗 fail-CLOSED → 兩腿不同時 fail-open → 無洩漏」。但 **np-window.sh 實測 egress-deny fresh pod 是 fail-OPEN**（t=0-1s code=200、t=2s 才擋）→ fresh agent egress 空窗**是 fail-open**。故存在**窄跨租戶洩漏 race**：attacker agent FRESH（egress fail-open）+ victim CS FRESH（ingress fail-open）~1s 重疊時，未授權 agent 可達 victim CS。實務可利用性低（需兩 pod 同時 fresh + 命中 CS IP + 猜中重啟時機），但**非「無洩漏」**。窗口為 kube-router 固有、無法消除；此 race **強化** leg-1 load-bearing（leg-1 egress 是唯一擋 established 未授權 agent 的腿）。

**落地順序（lock-step）**：見 §11.2（雙 pass 收斂版）。

### 11.1 雙 pass 收斂 + docs-leader workflow 新增 findings（2026-07-04）

leader（implementation-grounded）+ docs-leader（3 設計 + 9 skeptic）雙 pass **在 F1 收斂**（各自獨立抓到 unbind reconcile gap）。docs-leader 多抓 4 項 + F1 兩個精度，全部併入：

**F1 精度（unbind reconcile）**：
- (a) **`kubectl apply` 只 merge label、不刪 key** → 光 re-apply `_ensure_namespace` 不夠；unbind 收斂**必須顯式移除** `reach-ws-<W>` key（`kubectl label ns <ns> woow.tw/reach-ws-<W>-` 或整份 replace），否則永久 stale grant（比 F1 更毒）。
- (b) reconcile trigger **不能只掛 `project.cloud_service_ids` write**——CS-side 變動（`cloud_service.unlink` / workspace_id 改）是最自然的撤銷路徑、不 fire project hook → **O5 主 anchor 錨在 CloudService 生命週期**（反查 project_ids）。

**🔴 F6 NEW CRITICAL — cross-environment 洩漏（must-fix 最高優先）**：label value = 原始 Odoo workspace int id、**per-DB 非全叢集唯一**；woow-k3s **prod+stg 同一物理叢集**（helm.py:115-116，env label 正為此加）→ prod ws#3 與 stg ws#3 都 `workspace-id=3` → stg agent `egressWorkspaceIds=[3]` match prod CS ns、leg-2 `reach-ws-3` 也對上 → **stg agent 可達 prod CS**。#280 default-deny 落地後 selector 會**允許**此路 = 真跨環境租戶洩漏。**修**：leg-1 + leg-2 的 namespaceSelector matchLabels **都加 `paas.woowtech.io/env: <env>`**（CS/agent ns 已由 `build_managed_ns_labels` 帶此 label，helm.py:108）。

> **F6 enforcement（O4 前置，#290 review note 2）**：#279 label 無條件 stamp、env 只在 `settings.paas_env` 非空時加 → 共用 prod/stg 叢集若 `PAAS_ENV` 漏設，#279 label 變 env-unqualified = **config-level fail-open**（F6 跨環境洩漏重開）。O1（#290）label inert 不擋；**O4 讓 label load-bearing 前**必補：operator lifespan startup 加「非 dev 必設 `PAAS_ENV`」assert（或 stamp #279 label 但 env 空時 loud-warn）。

**🟠 F7 NEW — leg-2 改由 operator baseline 渲染（非 chart）**：旗艦 n8n 的 MCP 是 `patch_sidecar` 注入 n8n deployment（cloud_service.py:442-488）、CS ns **零 chart NP** → leg-2 若只靠 mcp-server chart selector **會漏 sidecar-MCP**。**修**：leg-2 reach-ws ingress allow 由 **operator baseline（O4 `_apply_baseline_netpol`）渲染** → 一機制同蓋 standalone mcp-server + sidecar-MCP、**消掉 leg-2 跨 repo chart 依賴**（leg-1 egress 仍需 chart）。**改寫 §3 O2 分工**：O2 不再是 chart selector，而是 operator baseline 的一條 ingress-allow（含 env-qualifier + MCP 埠）；charts 端 `ingressWorkspaceIds` 增強**不再需要**（docs-leader 知會 charts）。

**🟠 F8 NEW — 既有 CS ns workspace-id label backfill**：`woow.tw/workspace-id` 全 source 不存在（僅 paas-managed+env）→ 除 O1 往後 stamp，**既有 CS ns 需一次性 backfill**（否則 leg-1 對存量 CS 永遠 deny-all）。呼應 §7 Q3、擴到 workspace-id label。

**🟡 F9 minor**：(a) `egressWorkspaceIds` 是 `List[int]` → label value 須 `str()` + `validate_label_value`（int→string 鑄型否則 render 壞）。(b) **workspace-granular reach（知會 Eugene）**：CS ns per-workspace 共用 → 綁 W 內一個 CS = 網路可達 W 內**全部** mcp（比 per-CS 粗）；多半可接受（workspace=租戶邊界）但網路層粒度 ≠ 邏輯綁定粒度。

### 11.2 修正後落地順序（雙 pass 收斂版）

1. CS-ns `workspace-id` label **backfill**（F8）+ F3 chart NP template 勘查（實際比對哪個 key）
2. **env-qualified** label schema（F6，leg-1+leg-2 selector 都加 env）+ F4 key 真源（operator 算完整 key 明文傳、含 str 鑄型）
3. **O1**（CS ns stamp workspace-id+env；agent ns stamp reach-ws-<W>+env；含 backfill、str）/ **O3**（ns-label patch 原語，支援**顯式刪 key**）
4. **leg-2 走 operator baseline O4**（F7，reach-ws+env ingress allow，蓋 sidecar）
5. **O4/O5/O6 reconcile**（關 F1）：撤銷須**顯式刪 label**（F1a）+ 主 anchor 錨 **CloudService 生命週期**（F1b/O5）
6. **leg-1 收斂 + leg-2 撤銷同批 land**（lock-step）
