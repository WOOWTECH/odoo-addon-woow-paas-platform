---
title: "HAOS × KubeVirt Cloud Service v1 實作 Plan（#423）"
category: plans
status: draft
created: 2026-07-28
updated: 2026-07-28
related:
  - docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md
  - docs/decisions/patterns/README.md
  - docs/plans/2026-Q3/2026-07-08-317-chart-owned-pvc-keep.md
  - docs/plans/2026-Q3/2026-07-07-w1-ns-quota-governance-design.md
---

# HAOS × KubeVirt Cloud Service v1 實作 Plan（#423）

> **設計來源**：[`2026-07-27-haos-kubevirt-v1-design.md`](2026-07-27-haos-kubevirt-v1-design.md)（PR [#445](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/445) 已 merge 進 develop，commit `1666360c`）。本檔把該檔 §12 的 W0–W6 骨架細化成可執行任務。
> **上游 issue**：[#423](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/423)
> **本檔是 plan，不含任何 code 實作。** 撰寫期間 kubectl 僅唯讀。

**For agentic workers:** REQUIRED SUB-SKILL: 使用 `superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans` 逐任務執行本 plan。所有步驟用 checkbox（`- [ ]`）語法追蹤。

---

## 0. 拍板前提（本 plan 據此展開，不重議）

PR #445 第二則留言（Eugene，2026-07-28，經 grilling 檢驗後確認）：

| # | 題 | 定案 | 對本 plan 的影響 |
|---|---|---|---|
| **Q1** | HAOS OS 版本 | **實作當下官方 stable**，版本編進 image tag 與 chart `appVersion` | W2 的第一個動作＝查當下 stable 並記錄；W3 的 `appVersion` 隨之 |
| **Q2** | golden PVC clone | **v1 不做**，等 R8 開通耗時實測後再議 | W1-R8 的判定門檻直接對應這題；W2 只做 registry source |
| **Q3** | VM 型判定 | **`workload_type` 顯式欄位**（不嗅探；fail-loud 慣例） | W5a 新增欄位；W4 的 status API 以 query param 收 |
| **Q4** | `stopped` state | **v1 不預留**（design §7.2 方案 C）；欠費停權沿既有 `suspended` 路徑映射 `runStrategy` Halted/Always | W5a 不動 `state` Selection；W5a 測試須釘住「不新增 state」 |
| **Q6** | chart 名 | **`haos`** | W3a 目錄名 |
| **Q7** | 副本數 | **維持 2**（Longhorn 2 副本＋HAOS Supervisor backup 兩層保護） | W3a 不建新 SC；容量帳固定 128 GiB/租戶 |
| **Q5** | 定價／限量 | **延至 W5 前拍板**，不擋 W0–W4 | W5b 的**前置閘門**；成本模型以 128 GiB/租戶、天花板 15–18 台、CF 無 per-tenant 成本、golden image 無儲存折扣為準 |

---

## 1. Global Constraints

- **版號 bump**：`src/__manifest__.py` 現值 **`18.0.1.0.79`**。W5a → `.80`，W5b → `.81`（每個動 `src/` 的 PR bump 一次，沿既有慣例）。W4 只動 `extra/paas-operator/` 不需 bump。
- **縮排**：Python 4 space、XML/YAML/OWL 2 space。
- **Odoo 測試**：`./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init`，或沙盒內 `/woow-dev:k8s-dev-sandbox test`。
- **operator 測試**：`cd extra/paas-operator && .venv/bin/pytest tests/ -v`（**必須用 repo 內 `.venv`（3.11）**，系統 python 3.9 在 collection 期就會炸 PEP604）。整合測試 `HELM_BINARY=$(which helm) .venv/bin/pytest -m integration tests/integration/ -v`。
- **kubectl scope**：本 plan 全程只碰 `dev-sandbox-*`（PoC）與唯讀盤點。`kubevirt` / `cdi` namespace 屬**叢集運維**，本 repo 不碰。
- **PR 描述**：一律 zh-tw，含 `Refs #423` 與 design 檔連結。

---

## 2. Wave 總表與依賴

```
W0 叢集運維：裝 KubeVirt v1.8 + CDI v1.64（叢集級）
 │   ⚠ 硬阻塞：不裝就沒有 CRD，W1 無法開始
 ├──────────────────────────────┐
 ▼                              │
W1 PoC（dev-sandbox-423-poc）   │  W2 golden image（woow-paas-ci）
 │  回答 R5/R6/R7/R8/R9/R11     │   ← 與 W1 併行；W1 的 R8 需要 W2 的產物
 │                              │
 ├──── R7-A 失敗 → 回 design review（阻塞 W3）
 ▼                              ▼
W3a charts/haos ◀───────────────┘（image pin）
W3b charts/paas-operator RBAC（可先合，不依賴 W1）
 ▼
W4 operator（本 repo extra/paas-operator/）
W4b pvc-cleanup cronjob 擴充（charts repo；依賴 W1-R7）
 ▼
W5a 平台側程式（欄位／poll 分支／values／注入／測試）
 ▼
W5b seed（template + plan + M2M + repo-lint baseline）  ← Q5 拍板為前置閘門
 ▼
W6 stg e2e
```

**可併行**：W2 ∥ W1；W3b ∥ 任何階段（純加法 RBAC，建議最早合以免 W4 開發撞 Forbidden）。

### 2.1 PR 切分總表

| PR | Repo | 內容 | 依賴 | 可獨立 merge |
|---|---|---|---|---|
| — | — | **W0** 無 PR：叢集運維操作 ＋ #423 留言驗收 | — | — |
| — | — | **W1** 無 code PR：產出＝PoC 報告留言到 #423 | W0 | — |
| **PR-W1b** | 本 repo | （**條件式**）design 回寫：僅當 R5/R7/R8 結果推翻設計時才開 | W1 | ✅ |
| **PR-W2** | `woow-paas-ci` | HAOS golden OCI build workflow ＋ 首次 build ＋ digest 記錄 | — | ✅ |
| **PR-W3a** | `woow-paas-charts` | `charts/haos` v0.1.0 ＋ 5 支 render 契約測試 | W1、W2（image pin） | ✅ |
| **PR-W3b** | `woow-paas-charts` | `charts/paas-operator` ClusterRole 補 kubevirt.io / cdi.kubevirt.io | — | ✅ **建議最早合** |
| **PR-W4** | 本 repo | operator：`get_vmis` / `get_datavolumes` / status 擴充 / `scale_release` VM 分支 / `label_datavolumes` | W3a | ✅ |
| **PR-W4b** | `woow-paas-charts` | pvc-cleanup cronjob 擴充（刪 DataVolume 而非 PVC） | W1-R7 | ✅ |
| **PR-W5a** | 本 repo | `workload_type` 欄位、poll VM 分支（含 fail-open 回歸測試）、values `runStrategy` 同源、`_inject_storage_class` | W4 | ✅ |
| **PR-W5b** | 本 repo | CloudAppTemplate seed ＋ plan seed ＋ M2M link ＋ `repo-lint --update-baseline` | W5a ＋ **Q5 拍板** | ✅ |
| — | — | **W6** 無 PR：stg e2e 報告留言到 #423 | W5b | — |

---

## W0 — 叢集運維委託（KubeVirt + CDI 安裝）

**負責**：`woow-machines-operator` agent／叢集運維。**本 repo 不執行、不代跑。**
**阻塞性**：🔴 硬阻塞 W1。

### W0.1 委託單內容（原文可直接轉給叢集運維）

- [ ] **安裝 KubeVirt v1.8**（官方 operator YAML；**無官方 Helm chart**）
  - `kubectl apply -f https://github.com/kubevirt/kubevirt/releases/download/v1.8.x/kubevirt-operator.yaml`
  - 再建 `KubeVirt` CR（`kubevirt-cr.yaml`）
  - 落 namespace：`kubevirt`
- [ ] **安裝 CDI v1.64**（同樣是 operator YAML + CR）
  - `cdi-operator.yaml` + `cdi-cr.yaml`；落 namespace：`cdi`
- [ ] **確認 apiserver `--allow-privileged=true`**（virt-handler 是 privileged DaemonSet）
- [ ] **確認 containerd runtime**（k3s 內建，預期無事）

### W0.2 決策依據（給叢集運維的背景，不需重查）

| 項 | 值 | 依據 |
|---|---|---|
| 為何 v1.8 | 叢集是 **k8s v1.34.5+k3s1**；KubeVirt v1.8 支援 1.34/1.35 | design §0.1（實測）；KubeVirt 支援矩陣 |
| 為何 CDI v1.64 | Elmo 實際跑通的組合就是 KubeVirt v1.8.0 + CDI v1.64.0 + virtctl 1.8.0（**virtctl 必須與 server 版本完全一致**） | design §0.1（實測） |
| pod 額度夠不夠 | ✅ 夠：#422 已把 maxPods 調到 200；實查 280/800，最熱節點 104/200 | design §0.3（本檔實查） |
| 節點支援度 | ✅ 四台全裸機、`svm`+`npt` 完整、`/dev/kvm` 存在 → **不需 `useEmulation`** | #423 §2（實測） |

### W0.3 驗收信號（機械可判定）

```bash
# 1. CRD 全到齊
kubectl --context woow-k3s get crd | grep -E 'virtualmachines\.kubevirt\.io|virtualmachineinstances\.kubevirt\.io|datavolumes\.cdi\.kubevirt\.io'
#    → 三行皆存在

# 2. KubeVirt CR 就緒
kubectl --context woow-k3s get kubevirt -n kubevirt -o jsonpath='{.items[0].status.phase}'
#    → Deployed

# 3. virt-handler DaemonSet 四節點全上
kubectl --context woow-k3s get ds -n kubevirt virt-handler -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}'
#    → 4/4

# 4. CDI CR 就緒
kubectl --context woow-k3s get cdi -o jsonpath='{.items[0].status.phase}'
#    → Deployed
```

### W0.4 中止／回滾條件

- **中止**：virt-handler 在任一節點反覆 CrashLoop、或安裝後既有 workload 出現異常 → 立即停止、不進 W1。
- **回滾**：`kubectl delete -f kubevirt-cr.yaml`（先刪 CR 讓 operator 收斂）→ 再刪 operator YAML；CDI 同理。**由叢集運維執行。**
- ⚠ 節點層變更的外溢教訓（`reference_killmode_process_ha_stack_self_restart`）：安裝前先盤點跨節點健檢型 workload；若安裝過程需要重啟 kubelet，須預期鄰節點的 HA 堆疊可能自我重啟。

---

## W1 — PoC（重中之重）

**負責**：本 repo。**環境**：`dev-sandbox-423-poc` namespace（CLAUDE.md 白名單內）。
**依賴**：W0 完成。
**產出**：PoC 報告（留言到 #423）＋（條件式）PR-W1b design 回寫。

### W1.0 環境邊界（必讀，否則會踩錯 scope）

| 元件 | 裝在哪 | 誰負責 |
|---|---|---|
| KubeVirt CRD / virt-operator / virt-api / virt-controller / **virt-handler DaemonSet** | `kubevirt` ns ＋ 叢集級 CRD | **叢集運維（W0）** |
| CDI operator / apiserver / uploadproxy | `cdi` ns | **叢集運維（W0）** |
| **被測的 VM / DataVolume / Service / NetworkPolicy / ResourceQuota / LimitRange** | `dev-sandbox-423-poc` ns | **本 repo（W1）** |

**KubeVirt 不可能「只裝在一個 namespace」**——它有叢集級 CRD 與每節點 DaemonSet。所以 W1 只在自己的 sandbox ns 內建 workload，**絕不碰 `kubevirt` / `cdi` ns**。

### W1.1 前置：建立與 CS ns 同構的 quota 環境

沒有這步就複現不出 R5 的 quota 洞（dev sandbox 預設不建 ResourceQuota／LimitRange）。

- [ ] 建 ns：`kubectl --context woow-k3s create ns dev-sandbox-423-poc`
- [ ] 套用**與 CS ns 同構**的 LimitRange（複製 `extra/paas-operator/src/services/helm.py:1047-1059` 的形狀）
  - `default` / `defaultRequest` 皆 `cpu: 500m` / `memory: 512Mi`，`type: Container`，**不設 `max`**
- [ ] 套用**與 CS ns 同構**的 ResourceQuota（複製 `helm.py:1061-1087` 的 6-key hard）
  - 值取「單台 HAOS」的預期 quota：`requests/limits.cpu: 9`、`requests/limits.memory: 18Gi`、`persistentvolumeclaims: 10`、`requests.storage: 192Gi`
  - （＝design §6.3 的 `min_*` × 3；用設計推算值起跑，R5 若推翻再回填）
- [ ] 套用 chart 端與 operator baseline 的 NetworkPolicy 等價物（R6 用，見 W1.4）

### W1.2 PoC 用的 image 來源（刻意的臨時手段）

W1 與 W2 併行，W2 的 OCI image 可能還沒好。切法：

- **R5 / R6 / R7 / R9 / R11**：**可以用 CDI `http` source** 直接抓 HAOS 官方 `.qcow2.xz` URL。這些項目驗的是「VM 起來之後的行為」，與 image 從哪來無關。
  - ⚠ **這是 PoC 專用的臨時手段，不是 v1 設計**（v1 是 registry source，見 design §5.3）。PoC 報告必須註明，避免日後被當成設計依據。
- **R8（開通耗時）**：**必須等 W2 的 OCI image**——來源不同、耗時不同，用 http source 量出來的數字對 Q2 的判定沒有意義。

### W1.3 R5 — virt-launcher 容器數與 KubeVirt overhead 🔴 高阻塞

**要回答什麼**：design §6.3 的 `min_*`（3 vCPU / 6.0 GB / 64 GB）是**設計推算**，容器數（假設 3）與 KubeVirt memory overhead 的實值未知。這兩個數字直接決定 seed 值與 quota 是否夠用。

- [ ] **步驟 1**：在 W1.1 的 ns 內，用 design §3.3／§3.4 的 manifest 草案建一台 HAOS VM（DataVolume + VM，**先不建 Service**）
- [ ] **步驟 2**：列容器名單
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l kubevirt.io/domain=<vm-name> \
    -o jsonpath='{.items[0].spec.containers[*].name}'
  ```
- [ ] **步驟 3**：列每個容器的**實際生效** resources（含 LimitRange 補上的）
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l kubevirt.io/domain=<vm-name> -o json \
    | python3 -c "import sys,json;p=json.load(sys.stdin)['items'][0];[print(c['name'],c.get('resources')) for c in p['spec']['containers']]"
  ```
- [ ] **步驟 4**：讀 KubeVirt 的 memory overhead annotation
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l kubevirt.io/domain=<vm-name> -o jsonpath='{.items[0].metadata.annotations}'
  ```
- [ ] **步驟 5**：讀 ns quota 實際消耗
  ```bash
  kubectl --context woow-k3s get resourcequota -n dev-sandbox-423-poc -o yaml
  ```

**判定標準**

| 觀察 | 判定 | 動作 |
|---|---|---|
| 容器數 = 3 且 Σrequests.memory ≈ 5.3 GiB、Σlimits.cpu ≈ 3.0 | 設計推算成立 | `min_*` 維持 3 / 6.0 / 64 |
| 容器數 ≠ 3（例如 DataVolume 路徑只有 2 個：`compute` + `guest-console-log`） | 推算需修正 | **回寫 design §6.3**（`min_*` 下調），同步 W5b seed 值與 W1.1 的 quota |
| Σ 值高於推算 | 額度不足風險 | **回寫 design §6.3**（`min_*` 上調），本 wave 內重跑驗證 |
| VM 建不起來且錯誤是 `must specify limits.*` 或 `exceeded quota` | 洞 1 在「chart 有設 limits」下仍存在 | 🔴 **回寫 design §6.2**，可能須改動 LimitRange 策略 → 升級為設計級問題，回 design review |

**回寫落點**：design §6.3 表格、§8.2「RAM（pod requests）」「CPU（limits 帳面）」兩列。

### W1.4 R6 — NetworkPolicy 對 virt-launcher 的實際行為 🔴 高阻塞

**要回答什麼**：design §3.6 判定「virt-launcher 是普通 pod，NP 零改動」是**架構推論**，未在真 KubeVirt 上驗過。若 default-deny egress 擋掉 virt-launcher 需要的東西，VM 會起不來。

- [ ] **步驟 1**：套用 operator baseline NP 四條的等價物（形狀複製 `helm.py:1089-1151`，`podSelector: {}`）
  - `default-deny-ingress` / `allow-same-ns` / `allow-cloudflared` / `allow-mcp-reach`
- [ ] **步驟 2**：套用 chart NP（design §3.6：default-deny Ingress+Egress ＋ allow DNS ＋ `allowAllEgress: true`）
- [ ] **步驟 3**：把 VM 從 `Halted` 切到 `Always`，觀察能否進入 `Running`
  ```bash
  kubectl --context woow-k3s patch vm <vm> -n dev-sandbox-423-poc --type=merge -p '{"spec":{"runStrategy":"Always"}}'
  kubectl --context woow-k3s get vmi <vm> -n dev-sandbox-423-poc -w
  ```
- [ ] **步驟 4**：guest 內測對外（`virtctl console` 進 guest）
  - `curl -I https://version.home-assistant.io/stable.json`（HAOS 的更新檢查端點）
- [ ] **步驟 5**：建 Service，從**標了 cloudflared ns label 的來源**打 `http://<svc>:8123`
- [ ] **步驟 6（反面測試）**：從**未標 label 的第三方 ns** 打 8123 → **應被擋**

> ⚠ **測試方法紀律**（`reference_woow_k3s_netpol_fresh_pod_race`）：woow-k3s 的 NP 對新建 pod 有**啟動編程空窗**。臨時 pod 打完就走會系統性誤判「全毀」。測試端**必須用 long-lived pod、`sleep 20–30s` 後才發第一個請求**。
> ⚠ **TCP 探測紀律**（`reference_dash_devtcp_false_negative`）：不要用 `sh` 的 `/dev/tcp`（dash 下靜默假失敗）。用 `curl` 或 python socket。

**判定標準**

| 觀察 | 判定 |
|---|---|
| 步驟 3 進 Running ＋ 步驟 4 通 ＋ 步驟 5 通 ＋ 步驟 6 被擋 | ✅ PASS，design §3.6 成立、零改動 |
| 步驟 3 卡住（VMI 進不了 Running） | 🔴 virt-launcher 需要額外 egress → **回寫 design §3.6**，chart NP 須加 allow（先用 `kubectl describe vmi` + virt-handler log 定位缺哪條） |
| 步驟 4 不通但 3 通 | guest 對外被擋 → 檢查 `allowAllEgress` 是否生效、DNS 規則是否足夠 → **回寫 design §3.6** |
| 步驟 6 沒被擋 | 🔴 隔離破口 → **回寫 design §3.6**，這是 security 級問題，須在 W3a 前解 |

**回寫落點**：design §3.6 全節；若需新增 egress 規則，同步 W3a 的 `network-policy.yaml` 任務清單。

### W1.5 R7 — DataVolume 刪除語意 🔴 高阻塞（最危險）

**要回答什麼**：design §4.3 的三個失效點全是**設計論述**。若路徑 A 失敗，`helm uninstall` 會連租戶資料一起刪 —— 這是資料滅失級風險，必須在 chart 定案前實證。

> **用隔離 fixture**（另建一顆 VM/DV，**不要用 R5/R6 那台**），形制對齊 #281 的 PVC orphan-label e2e recipe：測的是「平台的刪除行為不碰不該碰的東西」，污染會讓結論不可信。

- [ ] **前置**：以 `helm install` 方式（而非裸 `kubectl apply`）安裝一份最小 chart，DataVolume 帶 `helm.sh/resource-policy: keep`。這樣才驗得到 helm 的 stored-manifest 行為（`reference_helm_resource_policy_stored_manifest`：helm 讀 stored manifest、不讀 live 物件）。

- [ ] **路徑 A — `helm uninstall`**
  ```bash
  helm uninstall <release> -n dev-sandbox-423-poc
  kubectl --context woow-k3s get dv,pvc -n dev-sandbox-423-poc
  ```
  | 觀察 | 判定 | 動作 |
  |---|---|---|
  | DataVolume 存活 ＋ PVC 存活 ＋ VM 已刪 | ✅ 設計成立 | W3a 照 design §3.4 實作 |
  | DataVolume 被刪 → PVC cascade 消失 | 🔴 **設計錯誤** | **阻塞 W3a**：回 design review，改用「DV 不進 helm release、由 operator 另建」等替代方案 |

- [ ] **路徑 B — 直接刪 PVC（模擬 cleanup cron 現行行為）**
  ```bash
  kubectl --context woow-k3s delete pvc <pvc> -n dev-sandbox-423-poc
  sleep 30
  kubectl --context woow-k3s get pvc,dv -n dev-sandbox-423-poc
  ```
  | 觀察 | 判定 | 動作 |
  |---|---|---|
  | PVC 被 CDI 重建（並重跑 import） | 設計論述成立 | **W4b 必做**：cleanup cron 改刪 DataVolume |
  | PVC 沒被重建 | 現行 cron 行為其實安全 | **簡化 design §4.3**，W4b 可降級為「加 DV 清理」而非「改行為」 |

- [ ] **路徑 C — 刪 DataVolume**
  ```bash
  kubectl --context woow-k3s delete datavolume <dv> -n dev-sandbox-423-poc
  kubectl --context woow-k3s get pvc -n dev-sandbox-423-poc
  ```
  預期：PVC 隨 ownerReference cascade 消失 → 確認「刪 DV」是正確的清理動作。

- [ ] **R11 附帶（同一 fixture 一起量）** — CDI 有沒有把 DataVolume 的 labels 複製到它建的 PVC
  ```bash
  kubectl --context woow-k3s get pvc <pvc> -n dev-sandbox-423-poc -o jsonpath='{.metadata.labels}'
  kubectl --context woow-k3s get pvc <pvc> -n dev-sandbox-423-poc -o jsonpath='{.metadata.ownerReferences}'
  ```
  | 觀察 | 判定 | 動作 |
  |---|---|---|
  | PVC 帶 `app.kubernetes.io/instance` | `delete_release` 的 `list_pvcs(label_selector=…)`（`releases.py:246-248`）撈得到 | W4 的 orphan 標記可沿用現行選擇器 |
  | PVC 不帶該 label | 🔴 orphan 標記會漏 | **W4 的 `label_datavolumes` 必須以 DataVolume 為標的**，且 orphan 撈取邏輯要改；**回寫 design §4.3 失效點 3** |

**回寫落點**：design §4.3 全節、§9 的 R7/R11 兩列。

### W1.6 R8 — 開通耗時分段（決定 Q2 是否重議）

**依賴 W2 的 OCI image**（用 http source 量的數字對這題無意義）。

- [ ] 從 `kubectl apply` 起計時，記四個時間點：
  1. DataVolume `Pending → ImportInProgress`（開始拉 image）
  2. DataVolume `Succeeded`（import 完成）—— 同時記錄 `status.progress` 的變化曲線
  3. VMI `Running` ＋ `Ready` condition = True
  4. guest 內 8123 可回應（in-cluster `curl`）
  ```bash
  # 進度輪詢（每 15s）
  kubectl --context woow-k3s get dv <dv> -n dev-sandbox-423-poc \
    -o jsonpath='{.status.phase}{"\t"}{.status.progress}{"\n"}'
  ```

**判定標準**

| 總耗時（步驟 1 → 4） | 判定 | 動作 |
|---|---|---|
| **< 15 分鐘** | 可接受 | Q2 維持「v1 不做 golden PVC clone」 |
| **15–30 分鐘** | 邊際 | 記錄數字，Q2 交 Eugene 定；v1 仍可上，UI 進度條（design §7.4）成為必要而非加分 |
| **> 30 分鐘** | 不可接受 | 🔴 **觸發 Q2 重議**，golden PVC clone 升級為 v1 必做 → 回寫 design §5.4 |

**回寫落點**：design §5.4 的判定門檻、§7.4 的進度可視需求、§9 的 R8 列。

### W1.7 R9 — qemu-guest-agent（低阻塞）

- [ ] ```bash
  kubectl --context woow-k3s get vmi <vm> -n dev-sandbox-423-poc \
    -o jsonpath='{.status.conditions}' | grep -o AgentConnected
  ```

| 觀察 | 判定 | 動作 |
|---|---|---|
| 有 `AgentConnected` | HAOS 內建 guest agent | 記進 v1.1 backlog（可做更準的就緒判定）；**v1 行為不改** |
| 無 | 符合設計假設 | 維持 design §4.1 第 5 點「就緒 ＝ VMI Ready」 |

**回寫落點**：design §4.1 第 5 點的邊界說明（僅補註）。

### W1.8 R3 — 64 GiB volume 行為（順帶量，無額外成本）

R5 的 VM 本來就是 64 GiB，import 過程順帶記錄：Longhorn volume 建立耗時、是否出現 rebuild／degraded、`kubectl get volumes.longhorn.io` 的 replica 狀態。

- [ ] 確認 replica 數 = **2**（Q7 拍板值；同時複驗 `longhorn-delete` SC 生效）

### W1.9 R1 / R2 — IO 實測 ⚠ 需 Eugene 事前同意

**紀律沿用 #423 的既有約定**（2026-07-26 留言）：Longhorn 是與 production 共用的儲存層，IO 壓測會對 prod volume 造成競爭。**必須**：(a) 控制強度與時長、(b) 挑離峰、(c) **取得 Eugene 同意後再跑，不可自行啟動**。

- [ ] **R1（VM 層 IO 開銷）**：在 guest 內跑與 2026-07-26 **完全相同**的測試（SQLite `journal_mode=WAL` + `synchronous=FULL`，500 次小交易；4KB raw fsync 300 次），與容器直寫的上界（p50 3.443 ms / p99 4.880 ms / 284 commits/sec）比對
  - 判定：VM 層若 ≥ 100 commits/sec 且 p99 < 20 ms → 對 HA recorder 仍充裕，設計不變
  - 判定：若 < 50 commits/sec → 回寫 design §9 R1，需重新評估（但**不阻塞 v1 上線**，屬 SLA 論述）
- [ ] **R2（多租戶競爭）**：2–3 台 HAOS 同時跑 recorder 壓測
  - 判定：單台數字劣化 < 2× → 可接受

**R4（recorder 定期 purge）**：需長時間 soak，**v1 不做**，列為上線後觀察項（W6 的 follow-up）。

### W1.10 PoC 收尾

- [ ] 產出 PoC 報告，留言到 #423：逐項列 R5/R6/R7/R8/R9/R11（＋R1/R2/R3 若已跑）的**實際輸出**與判定
  - **紀律**：報告必須沿用 design §0 的三級標註（實測／設計論述／實查），不可把推論寫成實測
  - 明確標註「PoC 用 http source 是臨時手段，非 v1 設計」
- [ ] **清理**：`kubectl --context woow-k3s delete ns dev-sandbox-423-poc`
  - ⚠ **Longhorn Retain 陷阱**（#319 殘留來源）：若 PoC 用到 `longhorn` SC（reclaimPolicy=Retain），**先把 PV patch 成 `Delete` 再刪 PVC**，讓 CSI 連後端 volume 一起回收。用 `longhorn-delete`（平台預設）則無此問題。
  - 刪除後**同命令複驗**：`kubectl --context woow-k3s get ns | grep 423-poc` 應無輸出；`kubectl get pv | grep Released` 確認無新增殘留
- [ ] （條件式）開 **PR-W1b** 回寫 design 檔被推翻的章節

### W1 驗收信號

```bash
# PoC 報告已留言
tea issues 423 --repo odoo-addons/woow_paas_platform --comments | grep -c "W1 PoC"   # ≥ 1
# 環境已清理
kubectl --context woow-k3s get ns dev-sandbox-423-poc   # → NotFound
```

### W1 中止條件

- **R7 路徑 A 失敗** → 🔴 **阻塞 W3a**，回 design review（資料滅失級）
- **R6 步驟 6 沒被擋** → 🔴 隔離破口，須在 W3a 前解
- **R5 出現無解的 admission 拒收** → 升級為設計級問題

---

## W2 — Golden image（`woow-paas-ci`）

**負責**：`woow-paas-ci` repo。**可與 W1 併行。**
**依賴**：無（Q1 已拍板＝實作當下官方 stable）。

- [ ] **W2.1**：查 HAOS 官方**當下 stable** 版本並記錄（Q1 拍板值；注意 #423 §3.2 的教訓——「2026.7.2」是 **HA Core** 版號，不是 HAOS OS 版號，兩者別搞混）
- [ ] **W2.2**：新增 build workflow —— 下載該版本 `.qcow2.xz` → 打包成 **OCI containerDisk** → 推 `jcr-prod.woowtech.io/woow-paas-docker-local/haos`
  - tag 規則：`{haos-os-version}-{UTC 時間戳}`（例 `18.1-20260801.0930`），對齊平台既有 timestamp tag 慣例
  - v1 內容 ＝ **stock HAOS qcow2 原樣重新封裝**，不改內容（rebrand 是 out-of-scope）
- [ ] **W2.3**：首次 build ＋ **從 registry 直驗 digest**（`docker manifest inspect` 或 registry API 兩來源交叉確認）
  - ⚠ 教訓（`project_prod_rollout_20260708`）：digest 必自驗，不可從 CI log 抄
- [ ] **W2.4**：把 `tag@sha256:…` 記錄下來，交給 W3a（chart `values.yaml` 預設）與 W5b（seed pin）

### W2 驗收信號

```bash
# image 存在且 digest 可直驗
docker manifest inspect jcr-prod.woowtech.io/woow-paas-docker-local/haos:<tag>
# → 回 manifest；digest 與 CI 產出一致
```

### W2 中止／回滾條件

- HAOS 官方沒有提供該版本的 qcow2（只有 img/vmdk）→ 回報並改用官方提供的格式，CDI 支援 raw 與 qcow2（含 gz/xz 壓縮）
- image 過大導致推送失敗（CF 上傳限制的既有教訓）→ 走 CI 內網推送，不從本機推
- 回滾：image 未被任何 seed pin 之前，推錯版本無外部影響；直接推新 tag 即可

---

## W3 — Chart（`woow-paas-charts`）

### W3b — operator ClusterRole RBAC（**建議最早合**）

**依賴**：無。**理由**：純加法、不影響現有行為；早合可避免 W4 開發時撞 Forbidden（#315 原樣重演）。

- [ ] 在 `charts/paas-operator/templates/clusterrole.yaml`（現 94 行，實查**無**任何 kubevirt/cdi apiGroup）新增：
  ```yaml
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines", "virtualmachineinstances"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  ```
- [ ] 註解寫明來源（`#423 v1`）與「為何不需要 `datavolumes/source`」（跨 ns clone 是 v1.1 的事，Q2 已定不做）

**驗收信號**
```bash
helm template t charts/paas-operator | yq 'select(.kind=="ClusterRole") | .rules[] | select(.apiGroups[0]=="kubevirt.io")'
# → 非空
```

### W3a — `charts/haos` v0.1.0

**依賴**：W1（用實測值定 resources）＋ W2（image pin）。

- [ ] **W3a.1** 建 chart 骨架（`Chart.yaml` / `values.yaml` / `_helpers.tpl` / `NOTES.txt`）
  - `name: haos`（Q6 拍板）、`version: 0.1.0`、`appVersion: <HAOS OS 版本>`（Q1）
  - `Chart.yaml` description 沿用 `charts/home-assistant/Chart.yaml` 的形制：寫明 OPERATOR COUPLING（chart pin 與 `helm_default_values.image.*` 必須同一次變更一起動）
- [ ] **W3a.2** `templates/datavolume.yaml`（design §3.4）
  - `source.registry`（Q2：不做 PVC clone）、`accessModes: [ReadWriteOnce]`、`volumeMode: Block`
  - **`storageClassName` 顯式**（values 鍵名固定 `persistence.storageClass`，才能吃到平台 `_inject_storage_class` 的預設 fallback）
  - **`helm.sh/resource-policy: keep` annotation**（W1-R7 路徑 A 驗證過的機制）
  - labels 含 `app.kubernetes.io/instance`（R11 相關）
- [ ] **W3a.3** `templates/virtualmachine.yaml`（design §3.3）
  - `runStrategy` 由 values 驅動（預設 `Always`）—— **這是防 upgrade 喚醒的關鍵**
  - `firmware.bootloader.efi.secureBoot: false`
  - `domain.resources` 顯式設 `limits == requests`，值取 W1-R5 回填後的規格
  - `masquerade` ＋ **ports 顯式只列 8123**
  - **`spec.template.metadata.labels` 必須含 chart selectorLabels**（含 `app.kubernetes.io/instance`）—— 同時解決 fail-open 與 NP podSelector 兩件事
- [ ] **W3a.4** `templates/service.yaml`：selector `kubevirt.io/domain`、`targetPort` 用數字、名稱沿用 release fullname
- [ ] **W3a.5** `templates/network-policy.yaml`：對齊 `charts/home-assistant` 形制；`ingressNamespaces` 空則不發 ingress allow；`allowAllEgress: true` 預設；**加上 W1-R6 實測後發現需要的額外規則**
- [ ] **W3a.6** 5 支 render 契約測試（形制對齊 `charts/home-assistant/tests/*.sh`：`helm template` + `yq` + `fail()`）

| 測試 | 釘住什麼 |
|---|---|
| `test-vm-firmware.sh` | `firmware.bootloader.efi.secureBoot == false` 必存在（缺則 HAOS 開不了機） |
| `test-masquerade-ports.sh` | `interfaces[0].ports` 長度 == 1 且 port == 8123（**防有人為了 MQTT 偷開全埠**） |
| `test-service-selector.sh` | Service selector == `kubevirt.io/domain: <fullname>` |
| `test-dv-keep-annotation.sh` | DataVolume 帶 `helm.sh/resource-policy: keep`（**防資料滅失**） |
| `test-labels-passthrough.sh` | VM 的 `spec.template.metadata.labels` 含 `app.kubernetes.io/instance`（**防 fail-open 復發**） |

- [ ] **W3a.7** 空 image tag 必須讓 render 失敗（沿用 home-assistant chart 的做法：**不設 `appVersion` fallback**，因為 registry 不會有 HAOS 版本號的 tag）

### W3 驗收信號

```bash
cd woow-paas-charts
helm lint charts/haos
for t in charts/haos/tests/*.sh; do bash "$t" || exit 1; done   # → 全 PASS
helm template t charts/haos --set image.tag="" 2>&1 | grep -q "required"   # → 空 tag 必失敗
```

### W3 中止／回滾條件

- W1-R7 路徑 A 失敗 → **W3a 不得開始**（DataVolume 的保護機制要重新設計）
- chart 未發佈到 OCI registry 前，對線上零影響；已發佈但無 CloudAppTemplate pin 亦不生效 → 回滾成本低

---

## W4 — Operator（本 repo `extra/paas-operator/`）

**依賴**：W3a（chart 定案後才知道 label／欄位長相）。**不需 bump `__manifest__.py`。**

### W4.1 新增 CR 讀取（`src/services/helm.py`，`KubectlService` 內）

- [ ] `get_vmis(namespace, label_selector=None) -> List[VmiInfo]`
  - `kubectl get vmi -o json` → `phase` ＋ conditions 中的 `Ready` ＋ `nodeName`
  - **CRD 不存在時 raise `KubectlException`**（不可回空 list——那會把「叢集沒裝 KubeVirt」偽裝成「這個 release 沒有 VM」，正是 fail-loud 慣例要防的）
  - 沿用 `validate_namespace()` 入口守衛（與所有 shell-out 方法一致）
- [ ] `get_datavolumes(namespace, label_selector=None) -> List[DvInfo]`
  - `phase`（Pending / ImportInProgress / Succeeded / Failed）＋ `status.progress`
- [ ] 對應的 Pydantic model（`src/models/`）

### W4.2 status API 擴充（`src/api/releases.py:390-440`）

- [ ] `GET /releases/{ns}/{name}/status` 新增 optional query `include_vm: bool = False`
- [ ] `include_vm=true` 時回應多兩欄：`vmis: []`、`datavolumes: []`
- [ ] **預設 false → 既有呼叫端零影響**（向後相容）
- [ ] VMI 撈取失敗時比照現行 `pod_retrieval_error` 的形制回 `vmi_retrieval_error`，**不吞錯**

### W4.3 `scale_release` VM 分支（`src/api/releases.py:443-514`）

- [ ] 現行「撈 Deployment/StatefulSet」路徑**一行不動**
- [ ] 兩者皆空時，撈 `kubectl get vm -l app.kubernetes.io/instance={name}`
  - 有 → `kubectl patch vm {vm} --type=merge -p '{"spec":{"runStrategy":"Halted"}}'`（`replicas == 0`）／`"Always"`（`replicas >= 1`）
  - 三者皆無才維持 404
- [ ] 回應 payload 新增 `virtualmachines` 欄位（與現行 `deployments` / `statefulsets` 對稱）
- [ ] 新增 `patch_vm_run_strategy()` 到 `KubectlService`

> **這條修的是真實失效**：不做的話，VM 型服務走欠費停權會撞 `releases.py:477-481` 的 404 → `_do_suspend` 捕捉例外 → 服務被寫成 `error`（`cloud_service.py:1437`）。

### W4.4 delete 語意（`src/api/releases.py:215-311`）

- [ ] 新增 `label_datavolumes(namespace, dv_names, labels)`（與 `label_pvcs`（`helm.py:1646-1674`）對稱，含 `validate_label_value` 前置檢查——單一壞值會靜默丟掉整批的教訓）
- [ ] `delete_release` 在標 PVC 之外，**也標 DataVolume**
- [ ] 標記依據 **W1-R11 的結論**：
  - PVC 有繼承 label → 沿用現行 `app.kubernetes.io/instance` 選擇器
  - PVC 沒繼承 → orphan 撈取改以 DataVolume 為準
- [ ] （條件式，依 W1-R7 路徑 B）**PR-W4b**：`charts/paas-operator/templates/pvc-cleanup-cronjob.yaml:72` 的 `kubectl delete pvc` 改為「對帶 orphan label 且過期的 DataVolume 執行 `delete datavolume`」
  - ⚠ **不可**對 DV-owned 的 PVC 直接 `delete pvc`（會被 CDI 重建並重跑一次完整 import）
  - ⚠ 該 cronjob 的 image 必須有 shell（`reference_distroless_kubectl_no_shell_cronjob`：官方 kubectl image 無 shell → `/bin/sh` 的 cronjob exit 127 CrashLoop）

### W4.5 測試

- [ ] `extra/paas-operator/tests/` 單元測試（mock kubectl）：`get_vmis` 解析、`scale_release` 三分支（Deployment／VM／404）、`label_datavolumes`
- [ ] `extra/paas-operator/tests/integration/` 整合測試（真 K8s）：**這類 contract drift 正是 mock 藏不住的 BUG-7/8/9 型**
  - ⚠ **無 CI runner**，必須在 merge 前本機跑：
    ```bash
    cd extra/paas-operator
    kubectl config use-context docker-desktop   # 或 woow-k3s
    HELM_BINARY=$(which helm) .venv/bin/pytest -m integration tests/integration/ -v
    ```
  - ⚠ 若本機 k8s 沒裝 KubeVirt，VM 相關的整合測試會 skip → **必須在有 KubeVirt 的 context 跑一次**（W0 完成後的 woow-k3s，用 `dev-sandbox-*` ns）

### W4 驗收信號

```bash
cd extra/paas-operator
.venv/bin/pytest tests/ -v                          # → 全綠
HELM_BINARY=$(which helm) .venv/bin/pytest -m integration tests/integration/ -v   # → 全綠（含新增的 VM 契約測試）
```

### W4 中止／回滾條件

- 改動全是加法（新方法 ＋ 既有方法加分支），既有路徑零改動 → 回滾 ＝ revert PR ＋ 重建 operator image（`/woow:build-operator`）
- ⚠ operator image 重建後需重新部署；stg 走 `values-stg.yaml` pin，prod 走 Fleet

---

## W5 — 平台側（本 repo `src/`）

### W5a — 程式（PR-W5a，版號 → `.80`）

- [ ] **W5a.1** `src/models/cloud_app_template.py` 新增欄位（接在既有 `min_storage_gb`（:122-126）之後）
  ```python
  workload_type = fields.Selection(
      selection=[('container', 'Container'), ('vm', 'Virtual Machine')],
      string='Workload Type', default='container', required=True,
      help='VM 型 workload 的就緒／啟停判定與容器型不同（見 #423 v1 設計）',
  )
  ```
  - 既有 template 全部落在預設 `container` → 零行為改變
  - ⚠ `required=True` ＋ 既有資料：Odoo 對既有 row 會套 default，不需 migration（但要在 PR 說明並以測試釘住）
- [ ] **W5a.2** `src/models/cloud_service.py` — `_poll_service_status`（:1219-1303）新增 VM 分支
  - `template.workload_type == 'vm'`：呼叫 status 時帶 `include_vm=true`；ready ⟺ **至少一個 VMI** 且 `phase == 'Running'` 且 `Ready` condition == True
  - **fail-closed**：`vmis` 為空 → **不翻 running**，維持 `deploying`
  - `container` 分支**一行不動**（見下方「為何不順手修既有 fail-open」）
- [ ] **W5a.3** `src/services/paas_operator.py` — `get_status`（:705）支援 `include_vm` 參數
- [ ] **W5a.4** values 組裝：`state == 'suspended'` → `vm.runStrategy = 'Halted'`；否則 `'Always'`
  - 落點在 `cloud_service.py` 的 helm values 組裝路徑（`_inject_storage_class` 等注入器所在的同一段，:703-741）
  - **這是防「`helm upgrade` 把停權中的 VM 喚醒」的關鍵**（design §4.2 第 4 點）
- [ ] **W5a.5** `_inject_storage_class`（:874-926）`path_map` 新增顯式 entry
  ```python
  'haos': [('persistence', 'storageClass')],
  ```
  - 預設 fallback 本來就是這條（:918），加顯式 entry ＝ 對齊三型 MCP 的既有做法（:904-915 註解：「document intent and guard against default drift」）
- [ ] **W5a.6** `src/models/cloud_service.py` — `_do_suspend`（:1409-1444）／`_do_resume`（:1446-1484）**不需改**（走同一個 `scale_release`，由 W4.3 在 operator 端分流）；但要以測試釘住這個假設

#### ⚠ 為何不順手修既有 container 分支的 fail-open

design 抓到 `cloud_service.py:1261` 的 `all_ready = all(...) if active_pods else (not pods)` 在 `pods` 為空時回 True。**v1 只在 VM 分支堵，不改 container 分支**：

- 既有容器型服務有一個合法的短暫狀態是「helm release 已 deployed 但 pod 還沒被 controller 建出來」，此時 `pods` 為空。改成 fail-closed 會讓這些服務停在 `deploying` 直到下一輪 poll —— 影響面是**所有現存租戶服務**，風險大於收益。
- v1 的做法：**用 characterization test 把現行 container 行為釘住**（確保 VM 分支的改動沒有波及），並在 `docs/tracking/followups/follow-ups.md` 記一筆「container 分支 fail-open 待評估」。

### W5a 測試策略

| 測試檔 | 涵蓋 |
|---|---|
| `src/tests/test_haos_workload_type.py` | ① `workload_type` 預設 `container`；② 既有 template（n8n/odoo/ha-paas/opendesign…）全部是 `container`（**零行為改變的證明**）；③ `required=True` 生效；④ **`state` Selection 仍是 8 態**（Q4 拍板「不預留 `stopped`」的守線） |
| `src/tests/test_cloud_service_poll_vm_branch.py` | **核心回歸測試**（見下表） |
| `src/tests/test_haos_seed_423.py` | W5b 的 seed 契約（形制對齊 `test_opendesign_seed_380.py` / `test_mcp_3type_seed_375.py`） |
| `src/tests/test_haos_suspend_resume_runstrategy.py` | suspend/resume ＋ values `runStrategy` 同源 |

**`test_cloud_service_poll_vm_branch.py` 的必測案例**（mock operator client 回應）：

| # | 情境 | 期望 | 防的是 |
|---|---|---|---|
| 1 | `workload_type='vm'`，回 `pods: []` ＋ `vmis: []` | state **維持 `deploying`**（**不得**翻 running） | 🔴 **design 抓到的 fail-open 回歸測試** |
| 2 | `workload_type='vm'`，`vmis=[{phase:Running, Ready:True}]` | 翻 `running` | 正常路徑 |
| 3 | `workload_type='vm'`，`vmis=[{phase:Running, Ready:False}]` | 維持 `deploying` | guest 還沒就緒就標 running |
| 4 | `workload_type='vm'`，`vmis=[{phase:Pending}]` | 維持 `deploying` | 同上 |
| 5 | `workload_type='container'`，回 `pods: []` | **維持現行行為**（characterization） | VM 改動波及既有服務 |
| 6 | `workload_type='container'`，正常 pod ready | 翻 `running` | 同上 |

**`test_haos_suspend_resume_runstrategy.py` 的必測案例**：

| # | 情境 | 期望 |
|---|---|---|
| 1 | VM 型服務 `_do_suspend` | 呼叫 `scale_release(replicas=0)`，state → `suspended`，`pre_suspend_state` 被記錄 |
| 2 | VM 型服務 `_do_resume` | 呼叫 `scale_release(replicas=1)`，state → `pre_suspend_state` |
| 3 | `state=='suspended'` 時組 values | `vm.runStrategy == 'Halted'` |
| 4 | `state=='running'` 時組 values | `vm.runStrategy == 'Always'` |

> #3/#4 是**防 upgrade 喚醒**的回歸測試 —— 這個失效是沉默的（停權租戶自己活過來繼續用），且會演變成計費爭議，必須有測試守住。

> ⚠ **ACL 測試陷阱**（`reference_odoo_sudo_getter_cache_priming_test`）：若要測 `workload_type` 的讀取權限，**不要**透過 `to_dict()` 驗（cache priming 會假綠）。要重設 group、`invalidate_all()`、直呼 getter。

### W5b — Seed（PR-W5b，版號 → `.81`）

**前置閘門**：🔴 **Q5（定價／限量）必須先拍板**——plan 的價格與是否限量直接決定 seed 內容。

- [ ] **W5b.1** `src/data/cloud_app_templates.xml` 新增 `cloud_app_haos`
  - `slug: haos`、`category: container`（或視前台分類決定）、`workload_type: vm`
  - `helm_chart_name: oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos`、`helm_chart_version: 0.1.0`
  - `helm_default_values`：`image.tag` 用 **`tag@sha256:` 雙釘**（W2.4 的值）、`persistence.size: 64Gi`、`vm.cores` / `vm.memory`、`service.port/targetPort: 8123`、`ingress.enabled: false`
  - `default_port: 8123`、`min_vcpu` / `min_ram_gb` / `min_storage_gb` ＝ **W1-R5 回填值**
  - 註解須寫明 **chart↔image 原子變更鐵律**（沿用 `cloud_app_ha_paas`（:554-576）的既有寫法）
- [ ] **W5b.2** `src/data/subscription_products.xml` 新增 plan ＋ product（價格待 Q5）
- [ ] **W5b.3** `src/data/subscription_template_links.xml` 新增 M2M link（形制：`eval="[(6, 0, [ref('plan_haos_monthly'), …])]"`）
  - ⚠ **變體 links 必須 `noupdate=0`**（既有教訓）
- [ ] **W5b.4** `src/__manifest__.py` 的 `data` 清單無需改（三個檔都已在列）
- [ ] **W5b.5** 🔴 **seed 拓撲 gate**：改 seed 會動 `scripts/audit/repo-lint.py:316` 的 `check_seed_topology`
  - 現行 baseline：`base_plans 51` / `linked_bases 48` / `subscription_products 267` / `tiers 24` / `variant_plans 216` / `visible_linked_bases 48`
  - **必須逐一檢視 `SEED_TOPOLOGY_DEPENDENTS` 的 5 個位置**（repo-lint 會列出來），確認都已同步，再跑：
    ```bash
    python3 scripts/audit/repo-lint.py --update-baseline
    ```
  - 5 個待檢位置：`test_plan_group_keys_368.py` 的 `_seeded_link_refs()`／`test_subscription_tax_clear_366.py` 的 `_MIN_SEEDED_PRODUCTS`／`test_plan_variants_generated.py`（**GENERATED，不可手改**）／`scripts/seed/test_generate_plan_variants.py` 的 family 分佈／`2026-07-19-cloud-services-220-manifest.md`（變體單一事實源）

### W5 驗收信號

```bash
# 1. 全測試綠
./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init
# 2. repo-lint 全過
python3 scripts/audit/repo-lint.py
# 3. 版號已 bump
grep '"version"' src/__manifest__.py    # → 18.0.1.0.81
```

### W5 中止／回滾條件

- **Q5 未拍板 → W5b 不得開始**（W5a 可先合，它不含價格）
- seed 是 `noupdate=0` → 回滾需 revert PR ＋ `-u woow_paas_platform` 重跑（不是單純 revert 就好）
- ⚠ **prod 的 `-u` 曝險窗**：發版走既有兩段式 SOP（見 prod SOP §5.1）

---

## W6 — stg e2e

**依賴**：W5b merge ＋ 發版到 stg。
**環境**：`/woow:deploy-stg` 建的 stg 沙盒（`dev-sandbox-{slug}`）。

- [ ] **W6.1 開通**：從 `/woow` 前台下單一台 HAOS → 觀察 state 走 `pending → deploying → running`
  - 記錄各段耗時，與 W1-R8 的 PoC 數字比對
  - **驗 fail-closed**：在 DataVolume 還在 import 時，state **必須**維持 `deploying`（這是對 design fail-open 的端到端驗證）
- [ ] **W6.2 曝露**：CF tunnel hostname 可達 8123，HAOS onboarding 畫面正常
  - ⚠ 用 `playwright-cli --headed`（UX 流程類）；截圖驗證用 `agent-browser --headed`
  - ⚠ `/woow` 是 OWL SPA：hash 深路由直接開會 404，要走 in-app 點擊（`reference_woow_playwright_e2e_gotchas`）
- [ ] **W6.3 suspend/resume**：手動觸發訂閱停權 → VM `runStrategy` 變 `Halted`、VMI 消失、**PVC 存活**；resume → 回 `Always` 且資料還在
- [ ] **W6.4 upgrade 不喚醒**（W5a.4 的端到端驗證）：在 suspended 狀態下跑一次 `helm upgrade`（改個無關 value）→ VM **必須維持 Halted**
- [ ] **W6.5 delete 三路徑資料保全**：走平台 delete → 驗 DataVolume ＋ PVC 存活且帶 orphan label
  ```bash
  kubectl --context woow-k3s get dv,pvc -n <ws-ns> -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{.metadata.labels}{"\n"}{end}'
  ```
- [ ] **W6.6 quota**：確認 ns ResourceQuota 的實際消耗與 W5b 的 `min_*` 推算相符（`kubectl get resourcequota -n <ws-ns> -o yaml`）
- [ ] **W6.7** 報告留言到 #423；把 R4（recorder purge soak）列為上線後觀察項

### W6 驗收信號

```bash
# 六項全過的機械判定
kubectl --context woow-k3s get vmi -n <ws-ns>                         # W6.1 → Running + Ready
curl -sSI https://<subdomain>.<paas_domain> | head -1                 # W6.2 → 200/302
kubectl --context woow-k3s get vm -n <ws-ns> -o jsonpath='{.items[0].spec.runStrategy}'  # W6.3/W6.4 → Halted
kubectl --context woow-k3s get pvc -n <ws-ns> -l paas.woowtech.io/orphaned-at            # W6.5 → 非空
```

### W6 中止／回滾條件

- 任一項失敗 → **不進 prod**，回對應 wave 修正
- stg 沙盒回滾：`/woow-dev:k8s-dev-sandbox destroy <slug> --context woow-k3s`
- ⚠ stg 上真的建了租戶 ns 時，e2e 結束要盤點交還（`reference_odoo_second_server_dev_assets_e2e` 的副作用教訓）

---

## 3. 風險映射（design §9 的 12 項 → 哪個 wave 回答）

| # | 風險 | 阻塞性 | 回答的 wave | 若答案推翻設計，回寫哪一節 |
|---|---|---|---|---|
| **R1** | VM 層 IO 開銷 | 中 | **W1.9**（⚠ 需 Eugene 同意） | design §9 R1（SLA 論述，不阻塞上線） |
| **R2** | 多租戶競爭 | 中 | **W1.9**（⚠ 需同意） | design §9 R2 |
| **R3** | 64 GiB 大 volume 行為 | 中 | **W1.8**（順帶量） | design §8.2 |
| **R4** | recorder 定期 purge | 低 | **W6.7 後的上線觀察期**（v1 不做） | — |
| **R5** | virt-launcher 容器數與 overhead | 🔴 高 | **W1.3** | design §6.3、§8.2 → 同步 W5b seed 值 |
| **R6** | NetworkPolicy 對 virt-launcher | 🔴 高 | **W1.4** | design §3.6 → 同步 W3a.5 |
| **R7** | DataVolume 刪除語意 | 🔴 高 | **W1.5** | design §4.3 → 阻塞 W3a／決定 W4b 範圍 |
| **R8** | 首次開通耗時 | 🔴 高 | **W1.6**（需 W2 的 image） | design §5.4（觸發 Q2 重議）、§7.4 |
| **R9** | HAOS 是否內建 qemu-guest-agent | 低 | **W1.7** | design §4.1 第 5 點（補註） |
| **R10** | Longhorn Backing Image 省儲存 | 低 | **v1 不做**（v1.1 專案） | — |
| **R11** | CDI 是否複製 DV labels 到 PVC | 中 | **W1.5**（附帶） | design §4.3 失效點 3 → 決定 W4.4 的標記標的 |
| **R12** | KubeVirt 版本綁 k8s 版本 | 長期治理 | **W0**（安裝時確認）＋長期 | 叢集升 k8s 的 SOP |

**四項高阻塞全部集中在 W1** —— 這是把 W1 定為「重中之重」的原因：W1 沒跑完，W3a 的 chart 就是在猜。

---

## 4. 執行紀律（貫穿全部 wave）

- **kubectl scope**：只碰 `dev-sandbox-*`（W1）與唯讀盤點。`kubevirt` / `cdi` / 其他客戶 ns **絕不碰**（CLAUDE.md 共用叢集鐵律）。
- **IO 壓測需事前同意**：Longhorn 與 production 共用，W1.9 的 R1/R2 必須 (a) 控強度時長 (b) 挑離峰 (c) **取得 Eugene 同意後才跑**。
- **證據紀律**：每個 wave 的驗收都要貼**實際命令輸出**，不可只寫「已完成」。digest 必自驗、狀態多信號交叉驗、刪除後同命令複驗。
- **報告的三級標註**：PoC 與 e2e 報告沿用 design §0 的分級（實測／設計論述／實查），不可把推論寫成實測。
- **PR 描述**：zh-tw，含 `Refs #423` ＋ design 檔連結；跨 repo PR 互相貼連結。

---

## 5. 待拍板事項

| # | 題 | 何時要答 | 擋住什麼 |
|---|---|---|---|
| **Q5** | 定價與是否限量（成本模型：128 GiB/租戶、天花板 15–18 台、CF 無 per-tenant 成本、golden image 無儲存折扣） | **W5b 開始前** | 只擋 W5b（seed 的價格）；W0–W5a 不受影響 |
| **Q2 重議** | 若 W1-R8 量出 > 30 分鐘，golden PVC clone 是否升為 v1 必做 | W1 結束時 | W3a 的 DataVolume source 設計 |

---

## 附錄：本 plan 引用的位置

| 位置 | 用於 |
|---|---|
| `docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md` | 設計母本（§3 chart／§4 operator／§6 quota／§9 風險／§12 wave 骨架） |
| `src/__manifest__.py:3` | 版號 `18.0.1.0.79` → W5a `.80`／W5b `.81` |
| `src/models/cloud_app_template.py:122-126` | `min_storage_gb`（`workload_type` 接在其後） |
| `src/models/cloud_service.py:703-741` | helm values 注入段（W5a.4 的 `runStrategy` 落點） |
| `src/models/cloud_service.py:874-926` | `_inject_storage_class`（:918 預設 fallback、:904-915 顯式 entry 慣例） |
| `src/models/cloud_service.py:1219-1303` | `_poll_service_status`（:1261 是 fail-open） |
| `src/models/cloud_service.py:1409-1484` | `_do_suspend` / `_do_resume`（:1437 失敗寫 error） |
| `src/services/paas_operator.py:705` | `get_status`（W5a.3 加 `include_vm`） |
| `src/data/cloud_app_templates.xml:554-576` | chart↔image 原子變更鐵律的既有寫法範本 |
| `src/data/subscription_products.xml:491-506` | plan seed 形制範本 |
| `src/data/subscription_template_links.xml:8-14` | M2M link 形制範本 |
| `scripts/audit/repo-lint.py:56-68` | `SEED_TOPOLOGY_DEPENDENTS`（W5b.5 的 5 個待檢位置） |
| `scripts/audit/repo-lint.py:316` | `check_seed_topology` gate |
| `extra/paas-operator/src/api/releases.py:215-311` | `delete_release`（:246-248 orphan 撈取選擇器） |
| `extra/paas-operator/src/api/releases.py:390-440` | `get_release_status`（W4.2） |
| `extra/paas-operator/src/api/releases.py:443-514` | `scale_release`（:477-481 的 404）（W4.3） |
| `extra/paas-operator/src/services/helm.py:1047-1087` | LimitRange ＋ ResourceQuota 形狀（W1.1 複製來源） |
| `extra/paas-operator/src/services/helm.py:1089-1151` | baseline NetworkPolicy 形狀（W1.4 複製來源） |
| `extra/paas-operator/src/services/helm.py:1646-1674` | `label_pvcs`（W4.4 的對稱物） |
| `charts/home-assistant/tests/test-deployment-contract.sh` | chart render 契約測試形制範本（W3a.6） |
| `charts/paas-operator/templates/clusterrole.yaml` | 94 行，無 kubevirt/cdi apiGroup（W3b） |
| `charts/paas-operator/templates/pvc-cleanup-cronjob.yaml:72` | `kubectl delete pvc`（W4b 的改動點） |
