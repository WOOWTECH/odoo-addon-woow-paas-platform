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
# 3) 公開入口：cloudflared → app（來源 ns 由 §6 實測確認）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: paas-baseline-allow-ingress-gateway, namespace: <ns>}
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
  - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: <cloudflared-ns>}}}]
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

| 路徑 | 為何必驗 | 驗法 | 狀態 |
| --- | --- | --- | --- |
| kubelet probe（node→pod）| ingress default-deny 若擋 kubelet health probe → pod 永遠 NotReady | 建帶 readinessProbe 的 pod + baseline default-deny，看 Ready | ⬜ 待 stg 實測 |
| cloudflared → app | 公開入口，擋掉 = 服務對外 502 | 確認 cloudflared 實際來源 ns 名，established probe 從該 ns → app | ⬜ 待確認 cloudflared ns |
| same-ns app↔pg / app↔mcp | chart NP 已放行，但需確認 baseline 疊加不破 | established exec app→pg:5432 / app→mcp:8080 | ⬜ 待 stg 實測 |
| DNS → kube-system:53 | 只有做 ns egress default-deny 才需（本 spec §4 不做 egress baseline）→ **N/A** | — | ➖ N/A（不做 egress baseline）|
| Odoo ↔ agent WS:8000 | agent-runtime chart 自管（`odooNamespace`），非 baseline 範圍 | 既有 F-PILOT-5 已處理 | ➖ chart 自管 |

## 7. Open design questions（誠實標出，需 docs-leader / 平台決策，勿讓 impl 硬猜）

- **Q1（leg-2 reconciliation，最關鍵）**：mcp-server `ingressNamespaces` 要填**該 workspace 全部 agent ns 名**，但 agent ns 是 **per-project、lazy 建立**（首次 spawn 才建）→ CS deploy 當下 agent ns 可能還不存在，且之後會增減。**選項**：
  - (A) **每次 agent ns 建立/刪除時，reconcile 同 workspace 全部 CS 的 mcp-server ingressNamespaces**（operator/平台需一個 workspace→CS 列表的反查 + patch values 重裝/patch NP）。複雜、有時序缺口。
  - (B) **改用 workspace-id label 對稱化**：mcp-server chart 加 `ingressWorkspaceIds`（鏡像 agent-runtime，charts agent 評估為低成本），CS ns 與 **agent ns 都 stamp `woow.tw/workspace-id`** → mcp-server ingress 直接 `namespaceSelector{woow.tw/workspace-id}` → **免枚舉、免 reconcile**。**推薦 (B)**，需重啟 charts agent 那個「現在不做」的小 PR + O3 確保 agent ns 也 stamp workspace-id label。
- **Q2（agent ns 自身 workspace-id 來源）**：`_build_install_values` 有 `project_workspace_ids`（agent 可 egress 到的 workspace 集合，schemas.py:399），但 agent ns **自己 label 該用哪個 workspace-id**？一個 project 屬單一 workspace，但 `project_workspace_ids` 是**可達集合**、未必等於自身。→ 需平台端明確傳 agent 自身 project 的 workspace_id（新欄位 or 複用）。**未定，需平台決策。**
- **Q3（baseline 對既有 stg ns 的回填）**：現存 stg 租戶 ns 無 baseline → 要不要一次性 backfill（對現存 ns apply baseline NP + 補 workspace-id label）？backfill 若漏 must-allow 會打斷現有服務 → 須先 §6 驗過再 backfill。

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
