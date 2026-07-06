---
title: W1 收尾 — NS/Quota 治理（#277 孤兒 ns + #278 quota/LimitRange）設計
date: 2026-07-07
issues: [277, 278]
status: design-approved-pending-review
---

# W1 NS/Quota 治理設計（#277 + #278）

## 背景與範圍

W1（資料/資安止血 P0）最後兩項，同組 NS/Quota 治理（scout `docs/scout/2026-07-02-ns-per-workspace-decision-matrix.md` §7 第 0 段）。#279/#280（NP 隔離）、#268（MCP 規格）已 merge。scout 已裁定 **維持混合粒度**（workspace-scoped CS 共用 workspace ns、agent per-project ns，不合併）→ #278 決策 B（agent quota 就地補 per-project ns）為既定路線。

**兩者非硬 blocker**：#277 有手動清理 stopgap；#278 為條件式（開放/不受信任租戶上線才算 blocker）。但同波收乾淨。

## 決策（2026-07-07 定案）

- **#277 策略 = dry-run reconciler 先行**：ns 建立時 stamp owner/TTL label + 帳號級 reconciler 只**報告**孤兒（不自動刪，符 repo 的 L4 / cf-ns-orphan「dry-run 慢 graduate」慣例）+ 一次性手動清既有空 ns。**不**建 eager DELETE 端點（#278 已修 quota drift → 孤兒 ns 的 quota 帳面害處已消，eager 刪的急迫性降低）。
- **#278 quota 值 = 平台預設**（硬編碼）：CS 沿用現行 `max(3×sum, 8/8/100)`、agent `3CPU/3Gi`、LimitRange defaultRequest/default。訂閱規格單一來源留 #273/#274 follow-up。

## 現況（2026-07-07 confirmed，post #279/#280）

- `helm.py:851 create_namespace(name, cpu_limit, memory_limit, storage_limit, workspace_id)`：建 ns（`build_managed_ns_labels`）+ ResourceQuota（`{ns}-quota`，hard: requests/limits.cpu·memory + pvc=10 + requests.storage）+ registry secret + `_apply_baseline_netpol`（#279）。**無 LimitRange**。quota 值由 Odoo caller 算好傳入。
- `helm.py:1411 delete_namespace(namespace)`：**存在**，但唯一生產 caller = agent teardown（`agent_runtimes.py`）。
- `namespaces.py`：只有 POST create（:25）+ POST backfill_baseline_netpol（:68）。**無 DELETE 端點**。
- `agent_runtimes.py:82 _ensure_namespace`：只 `apply_manifest` 貼 label。**無 quota、無 LimitRange**。
- reconcile 範式已有：`helm.py:1515 reconcile_ns_reach_labels`（#279）、`cloudflare.py:535 reconcile_tunnel_dns`（L4，dry-run `reconcilable` 恆 False）。
- CS quota 值算於 Odoo `cloud_service.py`（sum of allocated），deploy 路徑重算+apply（冪等只增），**刪除路徑不碰**（drift 根因）。

## 設計

### #278 A1 — CS ns quota drift 修（權威覆寫）

**問題**：deploy `kubectl apply` 冪等只往上棘輪；所有刪除路徑（`cloud_service.py` `_do_cleanup`/`_cleanup_helm`/`unlink`、`paas.py` `_delete_service`、`subscription_lifecycle_cron.py`）不重算 → per-workspace 共用 ns 的 quota 被 sum 撐大後永不縮。

**修法**：抽出「以現存非-deleting service 加總算 quota」的權威函式，**刪除路徑也呼叫**（重算 → 經 operator 覆寫該 ns 的 ResourceQuota）。operator 側加輕量 `PATCH/PUT /api/namespaces/{ns}/quota`（或複用冪等 apply-quota），避免刪 service 要跑整個 create_namespace（含 NP）。權威覆寫語意：quota = f(現存 service)，非 delta。

### #278 A2 — CS ns LimitRange（兜底）

`create_namespace` 在 quota 之後 apply 一個 LimitRange（`{ns}-limits`，type: Container，default/defaultRequest = 平台預設如 cpu 500m/mem 512Mi）。讓缺 limits 宣告的第三方 chart 吃安全預設而非被 ns quota 的 `limits.cpu/memory` hard 宣告 admission 拒建。

### #278 B1 — agent ns per-project ResourceQuota

`_ensure_namespace` 在貼 label 後 apply ResourceQuota（`{ns}-quota`）。值 = 平台預設 **3CPU/3Gi**（> 單 pod sidecar 啟用時的 2CPU/2Gi，留 headroom；agent 一 project 一 pod，非 sum）+ pvc 上限（如 3）+ requests.storage。可被 dormant `agent_session.quota_override_*` 覆寫（若日後啟用）。⚠️ 值必須 > 單 pod 需求否則同 ns 容不下（capacity reject）。

### #278 B2 — agent ns LimitRange

同 A2，`_ensure_namespace` 一併 apply LimitRange 兜底（守 `agentRuntime.resources` 被 --set 成缺 limits 的邊界）。

### #277 — 孤兒 ns：owner/TTL label + dry-run reconciler

1. **stamp label**（`create_namespace` + `_ensure_namespace` 經 `build_managed_ns_labels`）：加 `woow.tw/owner`（workspace/account 識別）+ `woow.tw/created-at`（或 TTL annotation）。純 additive、不影響現有 NP label 選擇。
2. **dry-run reconciler**：帳號級對帳——list 所有 `paas-ws-*` ns（cluster）對 Odoo stored `helm_namespace` 取差集 → 「Odoo 已無 record 但 cluster 仍有 ns」納入孤兒盤點。**只報告、不自動刪**（比照 L4 `reconcile_tunnel_dns` dry-run）。落點：Odoo 端 cron 呼叫 operator 列 ns（或 operator 端點吃 Odoo live-ns 集）——取 L4 reconciler 同構落點。
3. **一次性清存量**：手動清既有零負載空 ns（`paas-ws-stg-ad023482`）——非 code，運維動作。

## 測試策略

- **operator 單元**：`cd extra/paas-operator && pytest`（**原生跑、無 Odoo/amd64 模擬問題**）——驗 quota/LimitRange manifest 生成、agent quota 值、reconciler 差集邏輯（mock k8s list）。
- **operator integration**（真 k8s，docker-desktop）：`HELM_BINARY=$(which helm) pytest -m integration` 驗 quota/LimitRange 真 apply + admission 行為（缺 limits 容器吃 LimitRange 預設不被拒）。
- **Odoo 側**（A1 drift）：drift 重算的單元測試（mock operator client）——走既有 Odoo 測試環境（sandbox，避 amd64 模擬）。

## 範圍/排序

一份 spec，實作拆 **兩個 PR**（對兩個 issue）：
1. **PR-278**（先）：quota/LimitRange（A1+A2+B1+B2）——最硬的 agent 零-quota 逃逸口 + drift。
2. **PR-277**（後）：owner/TTL label + dry-run reconciler + 存量清理。
（label stamp 在 create_namespace 是共用觸點，PR-277 疊在 PR-278 之上。）

## 非目標（follow-up）

- 訂閱規格單一來源 quota（#273/#274，W3）。
- #277 reconciler graduate 成 auto-delete（F-ENV-2 graduation，另開）。
- eager workspace teardown 端點（本輪不建）。
