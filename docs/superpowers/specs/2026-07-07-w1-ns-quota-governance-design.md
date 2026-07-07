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
- reconcile 範式已有：`helm.py:1515 reconcile_ns_reach_labels`（#279，主動 label reconcile）、`cloudflare.py:535 reconcile_tunnel_dns`（**主動** create/update/delete DNS，**非** dry-run）；真正的 **dry-run「零自動刪」框架**在 operator `schemas.py:655+`（inventory，`reconcilable`/`dry_run` 首版恆 False/True）+ Odoo `src/services/reconciler.py`/`reconcile_report.py`（#277 延伸落點）。
- CS quota 值算於 Odoo `cloud_service.py`（sum of allocated），deploy 路徑重算+apply（冪等只增），**刪除路徑不碰**（drift 根因）。

## 設計

### #278 A1 — CS ns quota drift 修（權威覆寫）

**問題**：deploy `kubectl apply` 冪等只往上棘輪；所有刪除路徑（`cloud_service.py` `_do_cleanup`/`_cleanup_helm`/`unlink`、`paas.py` `_delete_service`、`subscription_lifecycle_cron.py`）不重算 → per-workspace 共用 ns 的 quota 被 sum 撐大後永不縮。

**修法**：抽出權威函式 `_compute_ns_quota(workspace)` = `max(3×Σ allocated(現存 service), floor 8/8/100)`，**刪除路徑也呼叫並覆寫** ns 的 ResourceQuota（非 delta、非 apply 冪等只增）。

**⚠️ timing（reviewer Major-1）**：重算的加總必須反映「刪除**之後**」的存活集，否則被刪 service 仍在 sum 內 → 覆寫成錯值。兩條刪除語意各別處理：
- `unlink`（ORM 硬刪）：重算/覆寫**排在 `super().unlink()` 之後**（record 已離場），或以 `services - self` 顯式排除本批。
- soft cleanup（`_do_cleanup`/`_cleanup_helm`/`_delete_service`/cron）：service 仍在但狀態轉 terminating → 加總 domain 明確 `state not in ('deleting','terminated')`，不靠隱含。

**operator 端點（reviewer Major-2）**：新增 `PUT /api/namespaces/{ns}/quota`，body = `{cpu_limit, memory_limit, storage_limit}`（與 `create_namespace` 同三值，Odoo 端算好傳入——沿用現行「值 Odoo 算、operator apply」分工，operator **不**自 cluster 重推），語意 = **整組 hard overwrite**（`kubectl apply` 該 ResourceQuota，覆蓋全部 hard 鍵含 pvc=10），不動 ns / NP / secret。避免刪 service 要跑整個 `create_namespace`。

### #278 A2 — CS ns LimitRange（兜底）

`create_namespace` 在 quota 之後 apply 一個 LimitRange（`{ns}-limits`，type: Container，default/defaultRequest = 平台預設如 cpu 500m/mem 512Mi）。

**這是 correctness requirement，非 best-practice**：ns ResourceQuota 一旦 hard 宣告了 `limits.cpu`/`limits.memory`（現行 `create_namespace` 就有），k8s admission **強制該 ns 內每個 container（含 init/sidecar）都必須宣告 limits**，否則直接拒建。現在 CS 能跑純屬現役 chart 剛好都帶 limits（無安全網）——任何缺 limits 的第三方 chart / Bitnami preset 改動會**靜默 deploy 失敗且訊息隱晦**。LimitRange 的 `default` 補上缺漏的 limits，讓容器吃安全預設而非被拒。

### #278 B1 — agent ns per-project ResourceQuota

`_ensure_namespace` 在貼 label 後 apply ResourceQuota（`{ns}-quota`）。值 = 新增的明確常數（避免實作自行編數）：

```python
AGENT_QUOTA_CPU = "3"       # > 單 pod sidecar 啟用 2CPU，留 headroom
AGENT_QUOTA_MEMORY = "3Gi"  # > 單 pod 2Gi
AGENT_QUOTA_PVC = "3"
AGENT_QUOTA_STORAGE = "10Gi"
```

requests/limits.cpu·memory 皆用上面（agent 一 project 一 pod、非 sum）。可被 dormant `agent_session.quota_override_*` 覆寫（若日後啟用）。⚠️ 值必須 > 單 pod 需求否則同 ns 容不下（capacity reject）。

### #278 B2 — agent ns LimitRange

同 A2，`_ensure_namespace` 一併 apply LimitRange 兜底（守 `agentRuntime.resources` 被 --set 成缺 limits 的邊界）。

### #277 — 孤兒 ns：owner/TTL label + dry-run reconciler

1. **stamp label**（`create_namespace` + `_ensure_namespace`）：加 `woow.tw/owner`（workspace/account 識別）+ `woow.tw/created-at`（RFC3339）。純 additive、不影響現有 NP label 選擇。**實作註記（reviewer Rec-2）**：`build_managed_ns_labels(workspace_id, reach_workspace_ids)` 現簽章要**新增 `owner`/`created_at` 參數**（或另拆 builder）——此 refactor 要刻意、兩 caller lock-step。

2. **延伸既有 L4 dry-run reconciler 框架**（reviewer Major-3 校正——**原引 `reconcile_tunnel_dns` 是錯的**：那支 `cloudflare.py:535` 其實主動 create/update/**delete** DNS，非 dry-run）。真正的 dry-run 框架已存在，#277 是**把 k8s ns 加進它的 fact 集**（scout：L4 現只盤 CF、不含 ns）：
   - operator 端 `schemas.py:655+` 的「首版零自動刪除」dry-run inventory（`reconcilable` 恆 False、`dry_run` 恆 True）——operator 只回**事實**（list `paas-ws-*` ns 的 name/labels），不做差集、不做 reconcilable 判定。
   - Odoo 端 `src/services/reconciler.py` + `src/models/reconcile_report.py` 做差集/classify/delete-gate（既有 test：`test_reconciler_diff`/`test_reconcile_delete_gate`/`test_reconcile_report_endpoint`）。#277 在此加一類 fact：**k8s ns**。
   - **孤兒定義**（reviewer Major-4，操作性明確）：cluster 上帶 `paas-managed=true` label 的 `paas-ws-*` ns，且**無**對應的 Odoo `cloud.service.helm_namespace` live record → 列為疑似孤兒。**dry-run 保證非破壞**（`reconcilable=False`、`dry_run=True`，只入報表供人工審）→ 誤判成本 = 一列多餘報表，非誤刪。auto-delete graduate 留 follow-up（F-ENV-2）。

3. **一次性清存量**：手動清既有孤兒空 ns（`paas-ws-stg-ad023482`）。「孤兒空」判準 = 無 live workload（`kubectl get all` 空）**且**無 Odoo helm_namespace record——非 code、運維動作，且是上條 dry-run 報表的第一批人工處置對象。

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

<!-- peer-reviewed: 2026-07-06T22:59:33Z rounds=2 verdict=approved by=miniclaude -->
