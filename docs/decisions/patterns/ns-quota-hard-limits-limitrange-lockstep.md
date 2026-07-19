# Pattern: ns ResourceQuota hard 宣告 limits ⇒ LimitRange 必須 lock-step 同落

> 適用：任何**新 namespace 類型**要加 ResourceQuota、或修改既有 ns quota 的 hard map 時。只要 hard 宣告了 `limits.cpu` / `limits.memory`，k8s admission 就強制該 ns 內**每個 container（含 init / sidecar）**必須自帶 limits 宣告，缺者**直接拒建且錯誤訊息隱晦**。因此 LimitRange default（本平台定值 cpu `500m` / memory `512Mi`）是 **correctness requirement 而非 best-practice**——ResourceQuota 與 LimitRange 必須**同 PR、同落點（lock-step）**，不可拆開「先上 quota、LimitRange 之後補」。出自 #278（PR #312）。

## 機制（為什麼是 correctness 而非 best-practice）

k8s 的 admission 規則：ns 的 ResourceQuota 一旦在 `spec.hard` 出現 `limits.cpu` / `limits.memory`，該 ns 內任何**未宣告 limits 的 container** 在建立時會被 quota admission 直接拒絕（`must specify limits.cpu, limits.memory` 之類的 event，藏在 ReplicaSet / Job event 裡，pod 根本不會出現，**表面症狀是 workload 靜默起不來**）。這個強制涵蓋 init container 與 sidecar，不只主容器。

平台現役 chart 全部能跑，**純屬「剛好都帶 limits」**——沒有任何安全網。任何一個缺 limits 的第三方 chart、Bitnami preset 改動、或使用者 `--set` 把 `resources` 清掉，都會觸發上述靜默拒建。

LimitRange 的 `default` / `defaultRequest` 正是為此存在：對**未宣告** limits 的 container 注入預設值（declared 的容器不受影響），讓 admission 放行。所以：

- **有 quota（hard limits.*）而無 LimitRange** = 埋雷：現在綠是運氣，下一個缺 limits 的容器就炸，而且錯誤難查。
- 兩者必須**同 PR 同落點**——同一個建 ns 的 code path 裡，quota apply 之後緊接 LimitRange apply。

## 現行實作（兩個 ns 類型，各自 lock-step）

**CS 租戶 ns（`paas-ws-*`）** — operator `create_namespace`：

- ResourceQuota hard 含 `limits.cpu` / `limits.memory`：`extra/paas-operator/src/services/helm.py:986-996`
- 緊接 apply LimitRange（#278 A2，「quota 之後、registry secret 之前」）：`helm.py:1018-1026`
- LimitRange manifest（`{ns}-limits`，type Container，default/defaultRequest = `500m`/`512Mi`）：`helm.py:1047-1059`（docstring 明寫「ns quota hard-宣告 limits.cpu/memory 後，admission 拒收未設 limits 的容器；default 補上安全預設讓其可建」）
- quota 覆寫端點 `update_namespace_quota`（#278 A1）維持完整 6-key hard map：`helm.py:1061-1087`

**agent per-project ns** — `_ensure_namespace`：

- ResourceQuota（#278 B1）：`extra/paas-operator/src/api/agent_runtimes.py:122-134`
- LimitRange（#278 B2，註解自述「**必要配套**：quota 逼出 limits → LimitRange 補未宣告 limits 的容器」）：`agent_runtimes.py:135-144`

兩處 LimitRange 值刻意同值（cpu `500m` / mem `512Mi`），單一平台預設，避免 ns 類型間漂移。

## 守則（新增 / 修改時）

1. **新 ns 類型要加 ResourceQuota** → 同一個建 ns 函式內、quota apply 之後**立即** apply LimitRange，同 PR 交付。不存在「LimitRange 是 follow-up」這個選項。
2. **hard map 移除 `limits.*`** 才能免 LimitRange——但現行兩處 hard map 都同時鎖 requests 與 limits（防 burst 超賣），不要為了省 LimitRange 而拆掉 limits 鎖。
3. **LimitRange 值沿用平台預設 `500m`/`512Mi`**（CS 與 agent 同值）；要改就兩處一起改，並確認 default ≤ quota hard（否則單一容器就吃爆 quota）。
4. **quota 覆寫路徑不碰 LimitRange**（`update_namespace_quota` 只 apply quota）——LimitRange 是 create-time 一次性兜底，語意上不隨 quota 值伸縮；若未來 LimitRange 值要可調，需另開對稱的覆寫路徑，不可只調 quota。

## 偵測 / 測試建議

- **integration 測試（真 admission）**：在真 cluster 對新建 ns 內丟一個**不帶 resources 的 pod** → assert 建得起來且 limits 被 LimitRange 注入為 `500m`/`512Mi`（缺 LimitRange 時此測試會紅=直接鎖死本 pattern）。跑法見 CLAUDE.md「PaaS Operator Integration Tests」段。
- **review checklist 一句話**：diff 裡出現新的 `kind: ResourceQuota` 且 hard 含 `limits.*` → 找同 PR 內對應的 `kind: LimitRange`；找不到就是 blocker。
- **除錯指紋**：ns 內 Deployment/Job「pod 一直是 0、無 pod event」→ `kubectl describe replicaset/job` 看 `FailedCreate` event 是否含 `must specify limits`——這就是 quota 有、LimitRange 缺（或第三方容器繞過 default 的其他 admission 拒建）。

## See also

- 同 family（兩個機制互為必要配套、必須 lock-step 落地否則比沒改更糟）：[[two-layer-acl-lockstep]]。
- 設計依據：`docs/plans/2026-Q3/2026-07-07-w1-ns-quota-governance-design.md`（#278 A2 段——「correctness requirement，非 best-practice」原始論證）＋ `docs/plans/2026-Q3/2026-07-07-pr278-ns-quota-limitrange.md`（Global Constraints 段——「B2 是 B1 的必要配套、兩者同 PR 同落點」＋ 500m/512Mi 定值）。
- Landing 記錄：#278 / PR #312（W1 ns/quota 治理）。
