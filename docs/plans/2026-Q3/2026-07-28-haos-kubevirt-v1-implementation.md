---
title: "HAOS × KubeVirt Cloud Service v1 實作 Plan（#423）"
category: plans
status: active
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
| **Q1** | HAOS OS 版本 | **實作當下官方 stable**，版本編進 image tag 與 chart `appVersion`<br>✅ **D11 已定案：18.1** | W2.1 已完成（見 W2）；W3a 的 `appVersion` ＝ `18.1` |
| **Q2** | golden PVC clone | **v1 不做**，等 R8 開通耗時實測後再議 | W1-R8 的判定門檻直接對應這題；W2 只做 registry source |
| **Q3** | VM 型判定 | **`workload_type` 顯式欄位**（不嗅探；fail-loud 慣例） | W5a 新增欄位；W4 的 status API 以 query param 收 |
| **Q4** | `stopped` state | **v1 不預留**（design §7.2 方案 C）；欠費停權沿既有 `suspended` 路徑映射 `runStrategy` Halted/Always | W5a 不動 `state` Selection；W5a 測試須釘住「不新增 state」 |
| **Q6** | chart 名 | **`haos`** | W3a 目錄名 |
| **Q7** | 副本數 | **維持 2**（Longhorn 2 副本＋HAOS Supervisor backup 兩層保護） | W3a 不建新 SC；容量帳固定 128 GiB/租戶 |
| **Q5** | 定價／限量 | **延至 W5 前拍板**，不擋 W0–W4 | W5b 的**前置閘門**；成本模型以 128 GiB/租戶、天花板 15–18 台、CF 無 per-tenant 成本、golden image 無儲存折扣為準 |

---

## 1. Global Constraints

- **版號 bump**：plan 撰寫時 `src/__manifest__.py` 為 `18.0.1.0.79`，原規劃 W5a → `.80`、W5b → `.81`。
  ⚠ **實際已漂移（2026-07-29 更正）**：`#424①` 先落地把 develop 推到 `.80`，故 **W5a 實取 `.81`**（PR #466），**W5b 順延為 `.82`**。每個動 `src/` 的 PR bump 一次，沿既有慣例；W4 只動 `extra/paas-operator/` 不需 bump。
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
W4 operator code（本 repo extra/paas-operator/）
W4b pvc-cleanup cronjob 擴充（charts repo；依賴 W1-R7）
 ▼
W4.6 🔴 部署 operator 到 stg（build → CD repo values-stg.yaml pin → 驗 pod digest）
 │    ← 缺這步 W6 必假失敗：stg 接的是 paas-stg-system 的「共用」operator，
 │      不會自動跑 W4 的新 code（.sandbox/post-create.sh:32-44 只做探索）
 ▼
W5a 平台側程式（欄位／poll 分支 model+controller／values／注入／測試）
 ▼
W5b seed（template + plan + M2M + repo-lint baseline）  ← Q5 拍板為前置閘門
 ▼
W6 stg e2e（W6.0 先驗 operator digest 再開跑）
```

**可併行**：W2 ∥ W1；W3b ∥ 任何階段（純加法 RBAC，建議最早合以免 W4 開發撞 Forbidden）。
**不可省的串行點**：W4.6 卡在 W4 與 W6 之間——W5a/W5b 可以先寫先合，但 **W6 開跑前 W4.6 必須完成**。

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
| **PR-W4.6** | `paas-app-bundle-cd` | stg pin：`paas-operator-stg/values-stg.yaml` → 新 operator tag | PR-W4 merge ＋ CI build | ✅ **W6 的硬前置** |
| **PR-W5a** | 本 repo | `workload_type` 欄位、**model ＋ controller 兩層** poll VM 分支（含 fail-open 回歸測試）、values `runStrategy` 同源、`_inject_storage_class` | W4 | ✅ |
| **PR-W5b** | 本 repo | CloudAppTemplate seed ＋ plan seed ＋ M2M link ＋ seed 測試 ＋ `repo-lint --update-baseline` | W5a ＋ **Q5 拍板** | ✅ |
| — | — | **W6** 無 PR：stg e2e 報告留言到 #423 | W5b ＋ **PR-W4.6** | — |

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
  - 🔴 **不得啟用 DataVolume GC**：CDI CR 的 `spec.config.dataVolumeTTLSeconds` **必須留空（不設）**
    - **理由**：啟用後 DataVolume 會在 import 成功時被自動刪除、PVC 的 ownerReference 被移除。平台的整條 delete 保護鏈（chart 的 `helm.sh/resource-policy: keep` 標在 DataVolume 上、operator 的 `label_datavolumes` orphan 標記、cleanup cron 刪 DataVolume）都以「DataVolume 持續存在」為前提，**啟用 GC 會讓這條鏈靜默踩空**（不報錯、但保護不生效）。
    - 若日後有人要開，須先回頭改 design §4.3 的整套刪除語意。
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

# 5. DataVolume GC 未啟用（平台 delete 保護鏈的前提）
kubectl --context woow-k3s get cdi -o jsonpath='{.items[0].spec.config.dataVolumeTTLSeconds}'
#    → 空（未設）
```

### W0.4 中止／回滾條件

- **中止**：virt-handler 在任一節點反覆 CrashLoop、或安裝後既有 workload 出現異常 → 立即停止、不進 W1。
- **回滾**：`kubectl delete -f kubevirt-cr.yaml`（先刪 CR 讓 operator 收斂）→ 再刪 operator YAML；CDI 同理。**由叢集運維執行。**
- ⚠ 節點層變更的外溢教訓（`reference_killmode_process_ha_stack_self_restart`）：安裝前先盤點跨節點健檢型 workload；若安裝過程需要重啟 kubelet，須預期鄰節點的 HA 堆疊可能自我重啟。

---

## W1 — PoC（重中之重）✅ **已於 2026-07-28 執行完成**

> **執行結果**：R5 / R6 / R7 / R9 / R11 **全部有答案**，報告見 [#423](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/423)（2026-07-28 22:38 留言）；產出 D1–D11 待回寫清單，leader 已核准全數回寫。
> **R8 未執行**——依 W1.2 的循環解法，必須等 W2 的 OCI golden image；用 `http` source 量的數字對 Q2 判定無意義。
> **R1 / R2 未執行**——依 W1.9 約定需 Eugene 事前同意（Longhorn 與 prod 共用）。
> 以下步驟保留原文，供 W2 之後補測 R8、或日後需要重跑時直接沿用；命令中的 selector 等處已依實測修正。

**負責**：本 repo。**環境**：`dev-sandbox-423-poc` namespace（CLAUDE.md 白名單內）。
**依賴**：W0 完成。
**產出**：PoC 報告（留言到 #423）＋（條件式）PR-W1b design 回寫。

### W1.0 環境邊界（必讀，否則會踩錯 scope）

| 元件 | 裝在哪 | 誰負責 |
|---|---|---|
| KubeVirt CRD / virt-operator / virt-api / virt-controller / **virt-handler DaemonSet** | `kubevirt` ns ＋ 叢集級 CRD | **叢集運維（W0）** |
| CDI operator / apiserver / uploadproxy | `cdi` ns | **叢集運維（W0）** |
| **被測的 VM / DataVolume / Service / NetworkPolicy / ResourceQuota / LimitRange** | `dev-sandbox-423-poc*` 三顆 ns | **本 repo（W1）** |

**KubeVirt 不可能「只裝在一個 namespace」**——它有叢集級 CRD 與每節點 DaemonSet。所以 W1 只在自己的 sandbox ns 內建 workload，**絕不碰 `kubevirt` / `cdi` ns**。

**W1 用到的三顆 ns**（全部落在 CLAUDE.md 白名單的 `dev-sandbox-*`）：

| ns | 用途 |
|---|---|
| `dev-sandbox-423-poc` | 主 PoC：VM / DataVolume / Service / quota / NP |
| `dev-sandbox-423-poc-cf` | **R6 專用**：扮演 cloudflared connector ns（見 W1.4 的理由） |
| `dev-sandbox-423-poc-other` | **R6 專用**：扮演「第三方 ns」，驗反面應被擋 |

- [ ] **工具前置**：下載 **virtctl v1.8.0**（⚠ **必須與 server 版本完全一致**，否則行為異常——#423 Elmo 材料的實測教訓）
  ```bash
  kubectl --context woow-k3s get kubevirt -n kubevirt -o jsonpath='{.items[0].status.observedKubeVirtVersion}'   # 先確認 server 版本
  # 依該版本下載對應的 virtctl，放進 PATH
  virtctl version   # → client 與 server 版本一致
  ```

### W1.1 前置：建立與 CS ns 同構的 quota ＋ registry 環境

沒有這步就複現不出 R5 的 quota 洞（dev sandbox 預設不建 ResourceQuota／LimitRange），也拉不動私有 registry 的 image。

- [ ] 建三顆 ns：
  ```bash
  for n in dev-sandbox-423-poc dev-sandbox-423-poc-cf dev-sandbox-423-poc-other; do
    kubectl --context woow-k3s create ns "$n"
  done
  ```
- [ ] 套用**與 CS ns 同構**的 LimitRange 到 `dev-sandbox-423-poc`（複製 `extra/paas-operator/src/services/helm.py:1047-1059` 的形狀）
  - `default` / `defaultRequest` 皆 `cpu: 500m` / `memory: 512Mi`，`type: Container`，**不設 `max`**
- [ ] 套用**與 CS ns 同構**的 ResourceQuota（複製 `helm.py:1061-1087` 的 6-key hard）
  - 值取「單台 HAOS」的預期 quota：`requests/limits.cpu: 9`、`requests/limits.memory: 15Gi`、`persistentvolumeclaims: 10`、🔴 **`requests.storage: 216Gi`**
  - （＝design §6.3 的 `min_*` × 3，**已回填 2026-07-28 W1 PoC 實測值** `3 / 5.0 / 72`）
  - ⚠ **`requests.storage` 用 192Gi 會讓 import 永久卡住**——CDI 的 target+prime+scratch 三 PVC 峰值實測 203.5 GiB（design §6.2 洞 3）
- [ ] 🔴 **建 `paas-registry` dockerconfigjson secret**（**W1.6 R8 的硬前置**）
  - 正式 CS ns 由 operator 的 `create_namespace` 順手建（`helm.py:1028` 呼叫 `ensure_registry_secret`），**PoC ns 沒有人建** → OCI registry source 會直接 ImagePullBackOff／import 失敗
  ```bash
  # 從既有 CS ns 複製一份（或用 jcr-prod 憑證重建）
  kubectl --context woow-k3s get secret paas-registry -n <既有 paas-ws-stg-* ns> -o json \
    | python3 -c "import sys,json;d=json.load(sys.stdin);d['metadata']={'name':'paas-registry','namespace':'dev-sandbox-423-poc'};print(json.dumps(d))" \
    | kubectl --context woow-k3s apply -f -
  # 驗證
  kubectl --context woow-k3s get secret paas-registry -n dev-sandbox-423-poc \
    -o jsonpath='{.type}'   # → kubernetes.io/dockerconfigjson
  ```
- [ ] 🔴 **確認 CDI 的 DataVolume GC 未啟用**（**R7 的硬前置**）
  ```bash
  kubectl --context woow-k3s get cdi -o jsonpath='{.items[0].spec.config.dataVolumeTTLSeconds}'
  # → 必須為空（未設）
  ```
  - **為什麼**：若 CDI 啟用 DataVolume GC，DataVolume 會在 import 成功後被自動刪除、PVC 的 ownerReference 被移除。此時 R7 的三條路徑全部量到「假的」結果，而 W4.4／W4b 整條以 DataVolume 為標的的 delete 鏈會**靜默踩空**（標記標的不存在、清理找不到東西）。
  - 若已啟用 → **停止 W1.5**，回報並與叢集運維協調關閉（見 W0.1）
- [ ] 套用 chart 端與 operator baseline 的 NetworkPolicy 等價物（R6 用，見 W1.4）

### W1.2 PoC 用的 image 來源（刻意的臨時手段）

W1 與 W2 併行，W2 的 OCI image 可能還沒好。切法：

- **R5 / R6 / R7 / R9 / R11**：**可以用 CDI `http` source** 直接抓 HAOS 官方 `.qcow2.xz` URL。這些項目驗的是「VM 起來之後的行為」，與 image 從哪來無關。

  > **D10（2026-07-28 W1 PoC 實證補記）：CDI v1.64 走 volume populator，import 期間 namespace 內同時存在三顆 PVC。**
  >
  > | PVC | 說明 |
  > |---|---|
  > | target（`<dv-name>`） | DataVolume 的目標 PVC，import 完成前是 `Pending` |
  > | `prime-<uuid>` | populator 的中介 PVC，實際承接寫入 |
  > | `prime-<uuid>-scratch` | **解壓／格式轉換用的暫存空間**；`.qcow2.xz` 這種壓縮來源必用 |
  >
  > 三顆都計入 ns 的 `requests.storage` quota → **峰值 ≈ 宣告值 × 3**（實測 203.5 GiB）。這是 design §6.2 洞 3 的來源，也是 `min_storage_gb` 要從 64 提到 72 的原因。
  >
  > 另兩個 populator 的觀察（影響 W4 的 orphan 標記邏輯）：
  > - **prime PVC 的 owner 是 target PVC**（不是 DataVolume）——ownership 是兩層。
  > - **import 成功後 CDI 會自行回收 prime／scratch PVC**（穩態每台只剩 1 顆）；但若刪除發生在 import 中途，`list_pvcs` 會多撈到它們。
  - ⚠ **這是 PoC 專用的臨時手段，不是 v1 設計**（v1 是 registry source，見 design §5.3）。PoC 報告必須註明，避免日後被當成設計依據。
- **R8（開通耗時）**：**必須等 W2 的 OCI image**——來源不同、耗時不同，用 http source 量出來的數字對 Q2 的判定沒有意義。

### W1.3 R5 — virt-launcher 容器數與 KubeVirt overhead 🔴 高阻塞

**要回答什麼**：design §6.3 的 `min_*`（3 vCPU / 6.0 GB / 64 GB）是**設計推算**，容器數（假設 3）與 KubeVirt memory overhead 的實值未知。這兩個數字直接決定 seed 值與 quota 是否夠用。

- [ ] **步驟 1**：在 W1.1 的 ns 內，用 design §3.3／§3.4 的 manifest 草案建一台 HAOS VM（DataVolume + VM，**先不建 Service**）
- [ ] **步驟 2**：列容器名單
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l vm.kubevirt.io/name=<vm-name>   # D2：kubevirt.io/domain 選不到 \
    -o jsonpath='{.items[0].spec.containers[*].name}'
  ```
- [ ] **步驟 3**：列每個容器的**實際生效** resources（含 LimitRange 補上的）
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l vm.kubevirt.io/name=<vm-name>   # D2：kubevirt.io/domain 選不到 -o json \
    | python3 -c "import sys,json;p=json.load(sys.stdin)['items'][0];[print(c['name'],c.get('resources')) for c in p['spec']['containers']]"
  ```
- [ ] **步驟 4**：推算 KubeVirt memory overhead
  - ⚠ **不要**去找 annotation：KubeVirt 並不保證把 overhead 寫成 pod annotation（原 plan 此處前提有誤）。
  - 改用**相減法**：`overhead = compute 容器的實際 requests.memory − guest 宣告的 4Gi`
  ```bash
  kubectl --context woow-k3s get pod -n dev-sandbox-423-poc \
    -l vm.kubevirt.io/name=<vm-name>   # D2：kubevirt.io/domain 選不到 -o json \
    | python3 -c "import sys,json;p=json.load(sys.stdin)['items'][0];c=[x for x in p['spec']['containers'] if x['name']=='compute'][0];print('compute requests:',c['resources'].get('requests'))"
  ```
- [ ] **步驟 5**：讀 ns quota 實際消耗
  ```bash
  kubectl --context woow-k3s get resourcequota -n dev-sandbox-423-poc -o yaml
  ```
- [ ] **步驟 6（負面測試，證實「洞 1」的機轉）**：另建一台**刻意省略 `domain.resources.limits`** 的 VM
  - 預期：LimitRange 補上 `limit: 512Mi` < request（guest 4Gi）→ **admission 拒收**，pod 建不起來
  - 觀察錯誤訊息（`kubectl describe vmi` / `kubectl get events`）
  - **目的**：證明 design §6.2 洞 1 的機轉真實存在，而不是只驗 happy path 就宣稱「chart 設 limits 解決了問題」——若這台**竟然建得起來**，代表洞 1 的成因判斷有誤，chart 的 `limits == requests` 就不是必要條件（要回頭修 design §6.2 的論述）
  - 測完立刻刪除，避免佔用 quota 影響步驟 1–5

**判定標準**

| 觀察 | 判定 | 動作 |
|---|---|---|
| 容器數 = 3 且 **Σrequests.memory ∈ [5.0, 6.0] GiB**、**Σlimits.cpu ∈ [2.5, 3.5]** | 設計推算成立（容差內） | `min_*` 維持 3 / 6.0 / 64 |
| 容器數 ≠ 3（例如 DataVolume 路徑只有 2 個：`compute` + `guest-console-log`） | 推算需修正 | **回寫 design §6.3**（`min_*` 下調），同步 W5b seed 值與 W1.1 的 quota |
| Σrequests.memory > 6.0 GiB 或 Σlimits.cpu > 3.5 | 額度不足風險 | **回寫 design §6.3**（`min_*` 上調），本 wave 內重跑驗證 |
| Σrequests.memory < 5.0 GiB | 估過寬（無安全風險，但 quota 浪費） | 可選擇下調；記錄實值即可 |
| 步驟 6 的無 limits VM **竟然建得起來** | 洞 1 的成因判斷有誤 | **回寫 design §6.2**（`limits == requests` 從「必要」降為「建議」） |
| 步驟 1–5 的**有 limits** VM 建不起來且錯誤是 `must specify limits.*` 或 `exceeded quota` | 洞 1 在「chart 有設 limits」下仍存在 | 🔴 **回寫 design §6.2**，可能須改動 LimitRange 策略 → 升級為設計級問題，回 design review |

**回寫落點**：design §6.2（洞 1 機轉）、§6.3 表格、§8.2「RAM（pod requests）」「CPU（limits 帳面）」兩列。

### W1.4 R6 — NetworkPolicy 對 virt-launcher 的實際行為 🔴 高阻塞

**要回答什麼**：design §3.6 判定「virt-launcher 是普通 pod，NP 零改動」是**架構推論**，未在真 KubeVirt 上驗過。若 default-deny egress 擋掉 virt-launcher 需要的東西，VM 會起不來。

#### ⚠ 為何需要兩顆額外的 ns（不能「幫測試 ns 標上 cloudflared label」）

operator baseline 的 `allow-cloudflared` 用的是 `namespaceSelector.matchLabels.kubernetes.io/metadata.name: <cf_ns>`（`helm.py:1135-1142`）。**`kubernetes.io/metadata.name` 自 k8s 1.22 起由 apiserver 強制寫入且等於 namespace 自身名稱，不可改**——所以「把某顆 ns 標上 cloudflared 的 label」照字面不可能做到。

正確做法：**PoC 的等價 NP 把 `cf_ns` 值填成我們自己的 `dev-sandbox-423-poc-cf`**，讓那顆 ns 天然帶有相符的 `metadata.name`，由它扮演 connector。反面測試則從 `dev-sandbox-423-poc-other` 發起。

> ⚠ **共用叢集鐵律**：測試 pod **只准**落在 `dev-sandbox-423-poc` / `-cf` / `-other` 這三顆 ns。**不得**借用任何既有 ns（客戶 ns、`paas-*`、`paas-cloudflare-system`）當「第三方來源」。

- [ ] **步驟 1**：VM 先維持 `runStrategy: Halted`（**順序很重要**：先確保 NP 在 VMI 首次啟動前就位，才測得到「NP 是否阻擋啟動」；VM 已在跑才套 NP 會漏掉啟動期的失效）
- [ ] **步驟 2**：套用 operator baseline NP 四條的等價物（形狀複製 `helm.py:1089-1151`，`podSelector: {}`），**`cf_ns` 值填 `dev-sandbox-423-poc-cf`**
  - `default-deny-ingress` / `allow-same-ns` / `allow-cloudflared` / `allow-mcp-reach`
- [ ] **步驟 3**：套用 chart NP（design §3.6：default-deny Ingress+Egress ＋ allow DNS ＋ `allowAllEgress: true`）
- [ ] **步驟 4**：**現在**才把 VM 切到 `Always`，觀察能否進入 `Running`
  ```bash
  kubectl --context woow-k3s patch vm <vm> -n dev-sandbox-423-poc --type=merge -p '{"spec":{"runStrategy":"Always"}}'
  kubectl --context woow-k3s get vmi <vm> -n dev-sandbox-423-poc -w
  ```
- [ ] **步驟 5**：guest 內測對外（`virtctl console` 進 guest）
  - `curl -I https://version.home-assistant.io/stable.json`（HAOS 的更新檢查端點）
- [ ] **步驟 6（正面）**：建 Service，從 **`dev-sandbox-423-poc-cf`** 的 long-lived pod 打 `http://<svc>.dev-sandbox-423-poc.svc.cluster.local:8123` → **應通**
- [ ] **步驟 7（反面）**：從 **`dev-sandbox-423-poc-other`** 的 long-lived pod 打同一位址 → **應被擋**

> ⚠ **測試方法紀律**（`reference_woow_k3s_netpol_fresh_pod_race`）：woow-k3s 的 NP 對新建 pod 有**啟動編程空窗**。臨時 pod 打完就走會系統性誤判「全毀」。步驟 6/7 的測試端**必須是 long-lived pod（`sleep 3600`），且進去後先 `sleep 20–30s` 才發第一個請求**。
> ⚠ **TCP 探測紀律**（`reference_dash_devtcp_false_negative`）：不要用 `sh` 的 `/dev/tcp`（dash 下靜默假失敗）。用 `curl` 或 python socket。

**判定標準**

| 觀察 | 判定 |
|---|---|
| 步驟 4 進 Running ＋ 5 通 ＋ 6 通 ＋ 7 被擋 | ✅ PASS，design §3.6 成立、零改動 |
| 步驟 4 卡住（VMI 進不了 Running） | 🔴 virt-launcher 需要額外 egress → **回寫 design §3.6**，chart NP 須加 allow（先用 `kubectl describe vmi` + virt-handler log 定位缺哪條） |
| 步驟 5 不通但 4 通 | guest 對外被擋 → 檢查 `allowAllEgress` 是否生效、DNS 規則是否足夠 → **回寫 design §3.6** |
| 步驟 6 不通 | tunnel 路徑被自家 NP 擋 → **回寫 design §3.6** |
| 步驟 7 **沒**被擋 | 🔴 隔離破口 → **回寫 design §3.6**，這是 security 級問題，須在 W3a 前解 |

**回寫落點**：design §3.6 全節；若需新增 egress 規則，同步 W3a 的 `network-policy.yaml` 任務清單。

### W1.5 R7 — DataVolume 刪除語意 🔴 高阻塞（最危險）

**要回答什麼**：design §4.3 的三個失效點全是**設計論述**。若路徑 A 失敗，`helm uninstall` 會連租戶資料一起刪 —— 這是資料滅失級風險，必須在 chart 定案前實證。

> **用隔離 fixture**（另建一顆 VM/DV，**不要用 R5/R6 那台**），形制對齊 #281 的 PVC orphan-label e2e recipe：測的是「平台的刪除行為不碰不該碰的東西」，污染會讓結論不可信。

- [ ] **前置 1**：確認 W1.1 的「CDI DataVolume GC 未啟用」檢查已通過。**GC 開著的話這一節量到的全是假結果**（DataVolume 會在 import 完成時自己消失）。
- [ ] **前置 2**：以 `helm install` 方式（而非裸 `kubectl apply`）安裝一份最小 chart，DataVolume 帶 `helm.sh/resource-policy: keep`。這樣才驗得到 helm 的 stored-manifest 行為（`reference_helm_resource_policy_stored_manifest`：helm 讀 stored manifest、不讀 live 物件）。
  ```bash
  helm install <release> <chart> -n dev-sandbox-423-poc --kube-context woow-k3s
  ```

- [ ] **R11（必須在路徑 C 之前做完）** — CDI 有沒有把 DataVolume 的 labels 複製到它建的 PVC
  - ⚠ **順序理由**：路徑 C 會刪掉 DataVolume 並 cascade 帶走 PVC，屆時就沒有東西可讀。R11 的觀察必須在 A/B/C 之前（或至少 C 之前）完成。
  ```bash
  kubectl --context woow-k3s get pvc <pvc> -n dev-sandbox-423-poc -o jsonpath='{.metadata.labels}'
  kubectl --context woow-k3s get pvc <pvc> -n dev-sandbox-423-poc -o jsonpath='{.metadata.ownerReferences}'
  ```
  | 觀察 | 判定 | 動作 |
  |---|---|---|
  | PVC 帶 `app.kubernetes.io/instance` | `delete_release` 的 `list_pvcs(label_selector=…)`（`releases.py:246-248`）撈得到 | W4 的 orphan 標記可沿用現行選擇器 |
  | PVC 不帶該 label | 🔴 orphan 標記會漏 | **W4 的 `label_datavolumes` 必須以 DataVolume 為標的**，且 orphan 撈取邏輯要改；**回寫 design §4.3 失效點 3** |

- [ ] **路徑 A — `helm uninstall`**
  ```bash
  helm uninstall <release> -n dev-sandbox-423-poc --kube-context woow-k3s
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

**回寫落點**：design §4.3 全節、§9 的 R7/R11 兩列。

### W1.6 R8 — 開通耗時分段（決定 Q2 是否重議）

**依賴 W2 的 OCI image**（用 http source 量的數字對這題無意義）。

- [ ] 🔴 **前置：驗 `paas-registry` secret 存在**（W1.1 已建；沒有它 OCI import 必定失敗，而錯誤訊息會指向 image 而非 secret，容易誤判成 W2 的 image 有問題）
  ```bash
  kubectl --context woow-k3s get secret paas-registry -n dev-sandbox-423-poc -o jsonpath='{.type}'
  # → kubernetes.io/dockerconfigjson；DataVolume 的 source.registry.secretRef 要指向它
  ```
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

> ⚠ **本表只產出「事實 + 是否送 Eugene 重議」，不替 Q2 定結論。** Q2（golden PVC clone 做不做）是 Eugene 已拍板的題（v1 不做），只有 Eugene 能改。PoC 的職責是把數字與影響講清楚，然後把決定權交回去。

| 總耗時（步驟 1 → 4） | 判定 | 動作 |
|---|---|---|
| **< 15 分鐘** | 在預期內 | Q2 維持原拍板（不做），**不需重議** |
| **15–30 分鐘** | 邊際 | 記錄數字並**提交 Eugene 決定是否重議 Q2**；v1 仍可上，但 UI 進度條（design §7.4）從加分變成必要 |
| **> 30 分鐘** | 顯著劣於預期 | 🔴 **必須提交 Eugene 重議 Q2**（附耗時分段、租戶等待體感、golden PVC clone 的成本估算）；Eugene 決定前 W3a 的 DataVolume source 設計暫緩定案 |

**回寫落點**：design §5.4（記錄實測耗時；是否改結論待 Eugene）、§7.4 的進度可視需求、§9 的 R8 列。

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
- [ ] **清理**：刪除三顆 PoC ns
  ```bash
  for n in dev-sandbox-423-poc dev-sandbox-423-poc-cf dev-sandbox-423-poc-other; do
    kubectl --context woow-k3s delete ns "$n"
  done
  ```
  - ⚠ **Longhorn Retain 陷阱**（#319 殘留來源）：若 PoC 用到 `longhorn` SC（reclaimPolicy=Retain），**先把 PV patch 成 `Delete` 再刪 PVC**，讓 CSI 連後端 volume 一起回收。用 `longhorn-delete`（平台預設）則無此問題。
  - 刪除後**同命令複驗**：`kubectl --context woow-k3s get ns | grep 423-poc` 應無輸出；`kubectl get pv | grep Released` 確認無新增殘留
- [ ] （條件式）開 **PR-W1b** 回寫 design 檔被推翻的章節

### W1 驗收信號

```bash
# PoC 報告已留言
tea issues 423 --repo odoo-addons/woow_paas_platform --comments | grep -c "W1 PoC"   # ≥ 1
# 三顆 ns 都已清理
kubectl --context woow-k3s get ns | grep -c 423-poc    # → 0
```

### W1 中止條件

- **R7 路徑 A 失敗** → 🔴 **阻塞 W3a**，回 design review（資料滅失級）
- **R6 步驟 6 沒被擋** → 🔴 隔離破口，須在 W3a 前解
- **R5 出現無解的 admission 拒收** → 升級為設計級問題

---

## W2 — Golden image（`woow-paas-ci`）

**負責**：`woow-paas-ci` repo。**可與 W1 併行。**
**依賴**：無（Q1 已拍板＝實作當下官方 stable）。

- [x] **W2.1** ✅ **已完成（D11，2026-07-28 W1 PoC 實查 GitHub API）**——結果如下，W2.2 直接用，不需重查：

  | 項 | 值 |
  |---|---|
  | HAOS 當前 stable | **18.1** |
  | qcow2 資產實名 | **`haos_ova-18.1.qcow2.xz`** |
  | 大小 | **535 MB**（≠ #423 材料寫的 ~300 MB——那是 17.1 的數字） |
  | 下載 URL | `https://github.com/home-assistant/operating-system/releases/download/18.1/haos_ova-18.1.qcow2.xz` |

  ⚠ **`generic-x86-64` 變體沒有 qcow2**（只有 `.img.xz` 與 `.raucb`）——要 qcow2 必須取 **`haos_ova-*`** 這個 VM 用變體。
  （#423 §3.2 的原教訓仍成立：「2026.7.2」是 **HA Core** 版號、不是 HAOS OS 版號，兩者別搞混。）
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

> ✅ **先合的安全性已確認**：K8s RBAC 對「尚未註冊的 apiGroup」是純字串比對、惰性生效——W3b 可以在 W0 裝好 KubeVirt **之前**就合入，規則會在 CRD 註冊後自動開始生效，不會讓 ClusterRole 變成無效物件。

- [ ] **開工第一步**：`cd woow-paas-charts && git pull`，**就地複驗本節引用的行號**（本 plan 撰寫時未在 charts repo 內逐行複驗，行號可能已漂移）
  ```bash
  wc -l charts/paas-operator/templates/clusterrole.yaml       # 撰寫時為 94
  grep -n "apiGroups" charts/paas-operator/templates/clusterrole.yaml
  grep -c "kubevirt.io" charts/paas-operator/templates/clusterrole.yaml   # 撰寫時為 0
  ```
- [ ] 在 `charts/paas-operator/templates/clusterrole.yaml`（撰寫時 94 行，實查**無**任何 kubevirt/cdi apiGroup）新增：
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
  - `source.registry`（Q2：不做 PVC clone）、`accessModes: [ReadWriteOnce]`、🔴 **`volumeMode: Filesystem`（D1，非 `Block`——Block 在本叢集會讓 CDI importer `Permission denied`，見 design §3.4）**
  - **`storageClassName` 顯式**（values 鍵名固定 `persistence.storageClass`，才能吃到平台 `_inject_storage_class` 的預設 fallback）
  - **`helm.sh/resource-policy: keep` annotation**（W1-R7 路徑 A 驗證過的機制）
  - labels 含 `app.kubernetes.io/instance`（R11 相關）
- [ ] **W3a.3** `templates/virtualmachine.yaml`（design §3.3）
  - `runStrategy` 由 values 驅動（預設 `Always`）—— **這是防 upgrade 喚醒的關鍵**
  - `firmware.bootloader.efi.secureBoot: false`
  - `domain.resources` 顯式設 `limits == requests`，值取 W1-R5 回填後的規格
  - `masquerade` ＋ **ports 顯式只列 8123**
  - **`spec.template.metadata.labels` 必須含 chart selectorLabels**（含 `app.kubernetes.io/instance`）—— 同時解決 fail-open 與 NP podSelector 兩件事
- [ ] **W3a.4** `templates/service.yaml`：selector 🔴 **`vm.kubevirt.io/name`（D2，非 `kubevirt.io/domain`——後者在 KubeVirt v1.8.0 不是自動 label，selector 會選不到任何 pod）**、`targetPort` 用數字、名稱沿用 release fullname
- [ ] **W3a.5** `templates/network-policy.yaml`：對齊 `charts/home-assistant` 形制；`ingressNamespaces` 空則不發 ingress allow；`allowAllEgress: true` 預設；**加上 W1-R6 實測後發現需要的額外規則**
- [ ] **W3a.6** 5 支 render 契約測試（形制對齊 `charts/home-assistant/tests/*.sh`：`helm template` + `yq` + `fail()`）

| 測試 | 釘住什麼 |
|---|---|
| `test-vm-firmware.sh` | `firmware.bootloader.efi.secureBoot == false` 必存在（缺則 HAOS 開不了機） |
| `test-masquerade-ports.sh` | `interfaces[0].ports` 長度 == 1 且 port == 8123（**防有人為了 MQTT 偷開全埠**） |
| `test-service-selector.sh` | Service selector == **`vm.kubevirt.io/name: <fullname>`**（D2）；並**斷言不是** `kubevirt.io/domain`（防回退） |
| `test-dv-keep-annotation.sh` | DataVolume 帶 `helm.sh/resource-policy: keep`（**防資料滅失**） |
| `test-labels-passthrough.sh` | VM 的 `spec.template.metadata.labels` 含 `app.kubernetes.io/instance`（**防 fail-open 復發**） |

- [ ] **W3a.7** 空 image tag 必須讓 render 失敗（沿用 home-assistant chart 的做法：**不設 `appVersion` fallback**，因為 registry 不會有 HAOS 版本號的 tag）

### W3 驗收信號

```bash
cd woow-paas-charts
helm lint charts/haos
for t in charts/haos/tests/*.sh; do bash "$t" || exit 1; done   # → 全 PASS
helm template t charts/haos --set image.tag="" 2>&1 | grep -q "required"   # → 空 tag 必失敗

# chart 真的發佈到 OCI 且可被 operator 拉到（W4/W6 的前提）
helm pull oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos --version 0.1.0
# → 成功下載；失敗代表 chart 沒推上去或版本號對不上，W4 之後都會撞
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
- [ ] （條件式，依 W1-R7 路徑 B）**PR-W4b**：`charts/paas-operator/templates/pvc-cleanup-cronjob.yaml` 的 `kubectl delete pvc`（撰寫時在 `:72`）改為「對帶 orphan label 且過期的 DataVolume 執行 `delete datavolume`」
  - [ ] **開工第一步**：`cd woow-paas-charts && git pull`，**就地複驗行號**（本 plan 未在 charts repo 內逐行複驗）
    ```bash
    grep -n "delete pvc" charts/paas-operator/templates/pvc-cleanup-cronjob.yaml   # 撰寫時為 72
    ```
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

### W4.6 🔴 把新 operator 部署到 stg（**W6 的硬前置，不可省**）

**為什麼這一節必須存在**：stg e2e 沙盒**不會**自帶 operator——`.sandbox/post-create.sh:32-44` 是去 `$OPERATOR_NS`（stg ＝ `paas-stg-system`）**探索既有的共用 operator**，把它的 URL 與 api-key 寫進沙盒設定。也就是說 **W6 接的是共用 operator，不會自動跑 W4 的新 code**。

照字面直接跑到 W6 會出現兩個「症狀不指向根因」的失效：

| 症狀 | 真因 |
|---|---|
| W6.1 永遠卡在 `deploying` | 舊 operator 的 status 回應不含 `vmis` → W5a 的 fail-closed 正確地拒絕翻 running |
| W6.3 suspend 把服務打成 `error` | 舊 `scale_release` 撞 `releases.py:477-481` 的 404 → `_do_suspend` 寫 error（`cloud_service.py:1437`）——**正是 W4.3 要修的那個失效在 stg 原樣重演** |

- [ ] **W4.6.1** 建置 operator image：`/woow:build-operator`（產 `operator-v{YYYYMMDD.HHMM}` tag → CI `build-paas-operator.yml` 建置並推送）
- [ ] **W4.6.2** 等 CI 完成，並**從 registry 直驗 image digest**
  - ⚠ 教訓（`project_prod_rollout_20260708`）：digest 必自驗，不可從 CI log 抄
  - ⚠ 查 CI 狀態用 Gitea API `/actions/runs?`，**不要用 `tea runs list`**（只列 completed，in-progress/queued 會靜默漏列）
- [ ] **W4.6.3** 開 PR 更新 **CD repo** `paas-app-bundle-cd` 的 stg pin
  - 檔案：`paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` → `tag: "{VERSION}"`
  - ⚠ **本階段只動 stg，不動 `paas-operator/values-prod.yaml`**（prod 隨正式發版走，且 prod 有 digest pin 優先於 tag 的既有陷阱）
  - commit + push → Fleet 自動同步
- [ ] **W4.6.4** 驗證 stg 真的跑在新 image 上（**認 pod digest，不認 values 檔字面**）
  ```bash
  kubectl --context woow-k3s get pod -n paas-stg-system -l app.kubernetes.io/name=paas-operator \
    -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
  # → 與 W4.6.2 自驗的 digest 相同
  ```
- [ ] **W4.6.5** 驗證新 API 真的生效（契約層，不只看 pod 起來）
  ```bash
  # 從叢集內打 status API，帶 include_vm
  curl -sS -H "X-API-Key: <key>" \
    "http://paas-operator.paas-stg-system.svc.cluster.local/releases/<ns>/<release>/status?include_vm=true" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print('vmis' in d, 'datavolumes' in d)"
  # → True True
  ```

> ⚠ **共用 operator 的影響面**：`paas-stg-system` 的 operator 服務所有 stg 租戶。W4 的改動是加法（既有路徑零改動、新欄位預設關閉），但升級仍會短暫重啟該 Deployment → 挑離峰、升級後跑一次既有 stg 服務的 status 抽驗確認無回歸。

### W4 驗收信號

```bash
# 1. 單元 + 整合測試
cd extra/paas-operator
.venv/bin/pytest tests/ -v                          # → 全綠
HELM_BINARY=$(which helm) .venv/bin/pytest -m integration tests/integration/ -v   # → 全綠（含新增的 VM 契約測試）

# 2. stg 已跑在新 image（W4.6.4）
kubectl --context woow-k3s get pod -n paas-stg-system -l app.kubernetes.io/name=paas-operator \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'   # → 與自驗 digest 相同

# 3. 新 API 契約生效（W4.6.5）→ vmis / datavolumes 欄位存在
```

### W4 中止／回滾條件

- 改動全是加法（新方法 ＋ 既有方法加分支），既有路徑零改動 → 回滾 ＝ revert PR ＋ 重建 operator image（`/woow:build-operator`）
- **stg 部署回滾**：把 `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` 的 tag 改回前一版 → Fleet 同步；**回滾後同樣要以 pod digest 複驗**（只改 values 字面不等於生效）
- ⚠ prod 不在本 wave 範圍；prod 的 operator 升級隨正式發版流程走

---

## W5 — 平台側（本 repo `src/`）

### W5a — 程式（PR-W5a，版號 → **`.81`**；原規劃 `.80`，因 #424① 先落地而順延）

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
- [ ] **W5a.2** 🔴 **就緒判定的 VM 分支必須同時覆蓋 model 層與 controller 層（兩份複製品）**

  **這條 fail-open 在 repo 裡有兩份**，只修 model 層會留下一半的破口：

  | 層 | 位置 | fail-open 實碼 | 誰在跑 |
  |---|---|---|---|
  | model（cron） | `src/models/cloud_service.py:1218-1303` | `:1261` `) if active_pods else (not pods)` | `_poll_transitional_services` cron |
  | **controller（前台）** | `src/controllers/paas.py:2694` `_update_service_status` | **`:2742-2749` 同型複製** | `:2417`（服務詳情每次 poll）、`:2172`（workspace 服務清單） |

  只修 `_poll_and_update_status` 的話，**租戶只要打開前台服務詳情頁，controller 就會把還在 import 的 VM 標成 `running`** —— cron 修好的東西被前台查詢覆寫回去。

  - [ ] **建議做法（優先）：重構消滅雙寫** —— 讓 `paas.py:2694` 的 `_update_service_status` 委派到 model 的 `_poll_and_update_status`，controller 只保留它自己需要的回傳/序列化部分
    - 收益：VM 分支只寫一次；日後任何就緒判定的修改自動同步兩條路徑
    - 風險控制：委派前先用 characterization test 把 controller 現行輸出釘住（見測試表 #7–#8），確保重構前後行為一致
  - [ ] **退路（若重構風險過高）**：兩處各自加 VM 分支，並在兩處互相加註解交叉指向（`# 同型邏輯另一份在 …，改這裡要同步改那裡`）
  - **VM 分支的語意（兩層一致）**：`template.workload_type == 'vm'` → 呼叫 status 時帶 `include_vm=true`；ready ⟺ **至少一個 VMI** 且 `phase == 'Running'` 且 `Ready` condition == True
  - **fail-closed**：`vmis` 為空 → **不翻 running**，維持 `deploying`
  - `container` 分支**兩層都一行不動**（見下方「為何不順手修既有 fail-open」）
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

design 抓到的 `all_ready = all(...) if active_pods else (not pods)`（model `:1261`／controller `:2749`）在 `pods` 為空時回 True。**v1 只在 VM 分支堵，兩層的 container 分支都不改**：

- 既有容器型服務有一個合法的短暫狀態是「helm release 已 deployed 但 pod 還沒被 controller 建出來」，此時 `pods` 為空。改成 fail-closed 會讓這些服務停在 `deploying` 直到下一輪 poll —— 影響面是**所有現存租戶服務**，風險大於收益。
- v1 的做法：**用 characterization test 把現行 container 行為釘住**（兩層都要，確保 VM 分支與 M4 的重構都沒有波及），並在 `docs/tracking/followups/follow-ups.md` 記一筆「container 分支 fail-open 待評估（model `:1261` ＋ controller `:2749` 兩處）」。
- ⚠ 若採 M4 的重構路線（controller 委派 model），這個「不改」的承諾**更需要**測試守住——重構本身就會改變 controller 的執行路徑。

### W5a 測試策略

| 測試檔 | 涵蓋 | 所屬 PR |
|---|---|---|
| `src/tests/test_haos_workload_type.py` | ① `workload_type` 預設 `container`；② 既有 template（n8n/odoo/ha-paas/opendesign…）全部是 `container`（**零行為改變的證明**）；③ `required=True` 生效；④ **`state` Selection 仍是 8 態**（Q4 拍板「不預留 `stopped`」的守線） | W5a |
| `src/tests/test_cloud_service_poll_vm_branch.py` | **核心回歸測試**，**model ＋ controller 兩層**（見下表） | W5a |
| `src/tests/test_haos_suspend_resume_runstrategy.py` | suspend/resume ＋ values `runStrategy` 同源 | W5a |
| `src/tests/test_haos_seed_423.py` | seed 契約（形制對齊 `test_opendesign_seed_380.py` / `test_mcp_3type_seed_375.py`） | **W5b**（seed 是 Q5 閘住的 W5b 產物，不能放在 W5a——W5a 合入時 seed 還不存在，測試會直接紅） |

**`test_cloud_service_poll_vm_branch.py` 的必測案例**（mock operator client 回應）：

| # | 層 | 情境 | 期望 | 防的是 |
|---|---|---|---|---|
| 1 | model | `workload_type='vm'`，回 `pods: []` ＋ `vmis: []` | state **維持 `deploying`**（**不得**翻 running） | 🔴 **design 抓到的 fail-open 回歸測試** |
| 2 | model | `workload_type='vm'`，`vmis=[{phase:Running, Ready:True}]` | 翻 `running` | 正常路徑 |
| 3 | model | `workload_type='vm'`，`vmis=[{phase:Running, Ready:False}]` | 維持 `deploying` | guest 還沒就緒就標 running |
| 4 | model | `workload_type='vm'`，`vmis=[{phase:Pending}]` | 維持 `deploying` | 同上 |
| 5 | model | `workload_type='container'`，回 `pods: []` | **維持現行行為**（characterization） | VM 改動波及既有服務 |
| 6 | model | `workload_type='container'`，正常 pod ready | 翻 `running` | 同上 |
| **7** | **controller** | 走 `paas.py:2417`（服務詳情）路徑，`workload_type='vm'` ＋ `vmis: []` | **維持 `deploying`** | 🔴 **M4：只修 model 層時，前台一 poll 就把 import 中的 VM 標 running** |
| **8** | **controller** | 走 `paas.py:2172`（workspace 服務清單）路徑，同上 | **維持 `deploying`** | 同上（第二個呼叫點） |
| **9** | **controller** | `workload_type='container'`，回 `pods: []` | **維持現行行為**（characterization） | M4 若採重構路線，守住 container 行為不變 |
| **10** | **controller** | `workload_type='vm'`，`vmis=[{phase:Running, Ready:True}]` | 翻 `running` | 兩層語意一致 |

> #7–#10 是 M4 的守線。若採「controller 委派 model」的重構路線，這四條同時是重構的驗收測試。

**`test_haos_suspend_resume_runstrategy.py` 的必測案例**：

| # | 情境 | 期望 |
|---|---|---|
| 1 | VM 型服務 `_do_suspend` | 呼叫 `scale_release(replicas=0)`，state → `suspended`，`pre_suspend_state` 被記錄 |
| 2 | VM 型服務 `_do_resume` | 呼叫 `scale_release(replicas=1)`，state → `pre_suspend_state` |
| 3 | `state=='suspended'` 時組 values | `vm.runStrategy == 'Halted'` |
| 4 | `state=='running'` 時組 values | `vm.runStrategy == 'Always'` |

> #3/#4 是**防 upgrade 喚醒**的回歸測試 —— 這個失效是沉默的（停權租戶自己活過來繼續用），且會演變成計費爭議，必須有測試守住。

> ⚠ **ACL 測試陷阱**（`reference_odoo_sudo_getter_cache_priming_test`）：若要測 `workload_type` 的讀取權限，**不要**透過 `to_dict()` 驗（cache priming 會假綠）。要重設 group、`invalidate_all()`、直呼 getter。

### W5b — Seed（PR-W5b，版號 → **`.82`**；原規劃 `.81`，隨 W5a 順延）

**前置閘門**：🔴 **Q5（定價／限量）必須先拍板**——plan 的價格與是否限量直接決定 seed 內容。

- [ ] **W5b.0** 新增 `src/tests/test_haos_seed_423.py`（seed 契約測試，與本 PR 的 seed 同進同出）
  - template 存在、`slug`、`workload_type == 'vm'`、chart pin（name ＋ version）、`image.tag` 為 `tag@sha256:` 雙釘格式、`default_port == 8123`
  - 🔴 **`min_vcpu=3` / `min_ram_gb=5.0` / `min_storage_gb=72`**（D5/D6 實測回填值，非原設計的 3 / 6.0 / 64）
  - plan ↔ template M2M link 存在
- [ ] **W5b.1** `src/data/cloud_app_templates.xml` 新增 `cloud_app_haos`
  - `slug: haos`、`category: container`（或視前台分類決定）、`workload_type: vm`
  - `helm_chart_name: oci://jcr-prod.woowtech.io/woow-paas-docker-local/haos`、`helm_chart_version: 0.1.0`
  - `helm_default_values`：`image.tag` 用 **`tag@sha256:` 雙釘**（W2.4 的值）、`persistence.size: 64Gi`、`vm.cores` / `vm.memory`、`service.port/targetPort: 8123`、`ingress.enabled: false`
  - `default_port: 8123`、🔴 **`min_vcpu: 3` / `min_ram_gb: 5.0` / `min_storage_gb: 72`**（D5/D6）
  - ⚠ `min_storage_gb=72` **不是賣給租戶的磁碟大小**（那是 64 GiB）——72 是為了讓 `×3` 的 quota（216 GiB）容納 **CDI import 的三 PVC 峰值 203.5 GiB**；seed 註解必須寫明這一點，否則日後有人「看到 64 就改回去」會讓所有開通卡死
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
grep '"version"' src/__manifest__.py    # → 18.0.1.0.82（W5b 後）
```

### W5 中止／回滾條件

- **Q5 未拍板 → W5b 不得開始**（W5a 可先合，它不含價格）
- seed 是 `noupdate=0` → 回滾需 revert PR ＋ `-u woow_paas_platform` 重跑（不是單純 revert 就好）
- ⚠ **prod 的 `-u` 曝險窗**：發版走既有兩段式 SOP（見 prod SOP §5.1）

---

## W6 — stg e2e

**依賴**：W5b merge ＋ 發版到 stg ＋ 🔴 **W4.6 已完成（`paas-stg-system` 的共用 operator 已跑在含 VM 支援的新 image 上）**。
**環境**：`/woow:deploy-stg` 建的 stg 沙盒（`dev-sandbox-{slug}`）。

- [ ] **W6.0 前置檢查（跑任何 e2e 之前先做）**：確認沙盒接到的 operator 有 VM 能力
  ```bash
  # 沙盒的 operator URL 來自 .sandbox/post-create.sh:32-44 的探索結果（指向 paas-stg-system 的共用 operator）
  kubectl --context woow-k3s get pod -n paas-stg-system -l app.kubernetes.io/name=paas-operator \
    -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
  ```
  - 若不是 W4.6 的 digest → **停止 W6**，回去完成 W4.6。否則 W6.1 會永遠卡 `deploying`、W6.3 會把服務打成 `error`，而症狀都不指向真因。

- [ ] **W6.1 開通**：從 `/woow` 前台下單一台 HAOS → 觀察 state 走 `pending → deploying → running`
  - 記錄各段耗時，與 W1-R8 的 PoC 數字比對
  - **驗 fail-closed**：在 DataVolume 還在 import 時，state **必須**維持 `deploying`（這是對 design fail-open 的端到端驗證）
- [ ] **W6.2 曝露**：CF tunnel hostname 可達 8123，HAOS onboarding 畫面正常
  - ⚠ 用 `playwright-cli --headed`（UX 流程類）；截圖驗證用 `agent-browser --headed`
  - ⚠ `/woow` 是 OWL SPA：hash 深路由直接開會 404，要走 in-app 點擊（`docs/guides/testing/woow-playwright-e2e-gotchas.md`）
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

狀態欄已於 **2026-07-28 W1 PoC 執行後更新**。

| # | 風險 | 阻塞性 | 回答的 wave | **狀態 / 結果** |
|---|---|---|---|---|
| **R1** | VM 層 IO 開銷 | 中 | **W1.9**（⚠ 需 Eugene 同意） | ⏭ **未執行**，待同意後排程 |
| **R2** | 多租戶競爭 | 中 | **W1.9**（⚠ 需同意） | ⏭ **未執行**，同上 |
| **R3** | 64 GiB 大 volume 行為 | 中 | **W1.8**（順帶量） | ✅ **已答 → D7**：replica=2 ✓、`actualSize` 4.4 GiB（thin）→ 改寫 design §8.2/§8.3 |
| **R4** | recorder 定期 purge | 低 | **W6.7 後的上線觀察期**（v1 不做） | — |
| **R5** | virt-launcher 容器數與 overhead | 🔴 高 | **W1.3** | ✅ **已答 → D3/D4/D5**：1 main + 1 init、overhead 384 MiB、Σreq.mem 4.431 GiB |
| **R6** | NetworkPolicy 對 virt-launcher | 🔴 高 | **W1.4** | ✅ **已答：四項全 PASS**，design §3.6 零改動 |
| **R7** | DataVolume 刪除語意 | 🔴 高 | **W1.5** | ✅ **已答：路徑 A PASS（非資料滅失級，W3a 解除阻塞）**；路徑 B 證實 → **W4b 必做** |
| **R8** | 首次開通耗時 | 🔴 高 | **W1.6**（需 W2 的 image） | ⏭ **待 W2**（唯一未答的高阻塞項） |
| **R9** | HAOS 是否內建 qemu-guest-agent | 低 | **W1.7** | ✅ **已答 → D8：內建**（`AgentConnected=True`） |
| **R10** | Longhorn Backing Image 省儲存 | 低 | **v1 不做**（v1.1 專案） | — |
| **R11** | CDI 是否複製 DV labels 到 PVC | 中 | **W1.5**（附帶） | ✅ **已答 → D9：有複製**，現行選擇器可沿用 |
| **R12** | KubeVirt 版本綁 k8s 版本 | 長期治理 | **W0**（安裝時確認）＋長期 | ✅ W0 已確認（KubeVirt v1.8.0 / k8s v1.34.5） |
| **新 B1** | `volumeMode: Block` 可用性 | 🔴 高 | **W1（未預期）** | 🔴 **Block 不可用 → D1 改 Filesystem** |
| **新 B2** | CDI import 三 PVC 峰值撞 quota | 🔴 高 | **W1（未預期）** | 🔴 **`min_storage_gb` 64 會讓開通永久卡住 → D6 改 72** |

**四項原定高阻塞已答三項（R5/R6/R7），R8 待 W2。** 但 W1 另外撞出兩個 plan 未預期的阻斷點（B1/B2），其中 B2 會讓真實 CS namespace 的開通永久卡住且症狀不指向根因——**這正是「W1 沒跑完，W3a 的 chart 就是在猜」的最好證明**。

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
| **Q2 重議（條件式）** | 若 W1-R8 量出 ≥ 15 分鐘，PoC 提交數字**由 Eugene 決定**是否重議「golden PVC clone v1 不做」 | W1 結束時 | 僅在 Eugene 決定重議時才擋 W3a 的 DataVolume source 定案 |

---

## 6. 修訂記錄

### 2026-07-28（二）— W1 PoC 實證回寫（D1–D11，leader 於 #423 核准）

W1 已執行完成（R5/R6/R7/R9/R11；R8 待 W2）。PoC 推翻 design 數處設計論述，並撞出兩個本 plan 未預期的阻斷點。本檔受影響處同步如下：

| D# | 本檔落點 | 內容 |
|---|---|---|
| **D1** | **W3a.2** | chart DataVolume 規格 `volumeMode: Block` → **`Filesystem`** |
| **D2** | **W3a.4**、**W3a.6 測試表**、**W1.3 步驟 2–4 命令** | Service selector `kubevirt.io/domain` → **`vm.kubevirt.io/name`**；chart test 加「斷言不是 `kubevirt.io/domain`」防回退；PoC 命令的 `-l` selector 同步 |
| **D5 / D6** | **W1.1**、**W5b.0**、**W5b.1** | `min_*` 回填實測值 **3 / 5.0 / 72**；W1.1 的 quota 由 `18Gi / 192Gi` 改為 **`15Gi / 216Gi`**；W5b.1 加註「72 不是賣給租戶的磁碟大小」 |
| **D10** | **W1.2** | 補記 CDI v1.64 的 **volume populator 三 PVC 流程**（target／prime／scratch）與兩個 ownership 觀察 |
| **D11** | **§0 Q1 表**、**W2.1** | Q1 定案：HAOS **18.1**、資產實名 **`haos_ova-18.1.qcow2.xz`（535 MB）**；W2.1 標記已完成、附完整 URL 與「`generic-x86-64` 沒有 qcow2」的坑 |
| D3／D4／D7／D8／D9 | 僅落 design 檔 | 本檔無對應任務需改（詳見 design §13 修訂記錄） |
| — | **W1 章標題**、**§3 風險映射表** | W1 標記已執行完成、附執行結果；風險表補「狀態/結果」欄與 B1／B2 兩列 |

**解鎖**：D1 + D2 落檔後 **W3a chart 解除阻塞、可開工**；D11 落檔後 **W2 可開工**。
**仍待**：R8（待 W2 的 OCI image）、R1/R2（待 Eugene 同意）、Q5 定價（閘 W5b）。

### 2026-07-28（一）— PR #447 深審（4 維度平行對抗審查）findings 修訂

零 FAIL、零 blocker；骨架全部成立。以下 4 MAJOR ＋ 9 MINOR 已逐條修訂：

| # | Finding | 修訂落點 |
|---|---|---|
| **M1** | W4→W6 缺 operator 部署到 stg 的環節（stg 接 `paas-stg-system` 的共用 operator，不會自動跑新 code） | 新增 **W4.6**（build → CD repo pin → 驗 pod digest → 驗 API 契約）；W3a 驗收加 `helm pull`；W6 新增 **W6.0 前置檢查**；依賴圖與 PR 總表同步 |
| **M2** | R6 步驟「標 cloudflared ns label」照字面不可執行（`kubernetes.io/metadata.name` 由 apiserver 強制、不可改） | W1.4 改**雙 PoC ns 方案**（`-cf` / `-other`）＋明寫共用叢集鐵律；W1.0 補三顆 ns 表 |
| **M3** | R8 的 OCI import 缺 `paas-registry` secret 前置（正式 ns 由 `helm.py:1028` 順手建，PoC ns 沒人建） | W1.1 補建 secret 步驟；W1.6 補前置驗證 |
| **M4** | fail-open 在 controller 層有第二份複製品（`paas.py:2742-2749`，呼叫點 `:2417`／`:2172`） | W5a.2 改寫為**兩層覆蓋**，優先建議重構為 controller 委派 model（消滅雙寫）；測試表新增 #7–#10 controller 案例 |
| m1 | R8 判定表預先替 Q2 定了結論 | 改為「產出事實 ＋ 是否送 Eugene 重議」，與 §5 一致 |
| m2 | `test_haos_seed_423.py` 誤列在 W5a 測試表 | 移到 **W5b.0**（seed 是 Q5 閘住的 W5b 產物） |
| m3 | 缺 CDI DataVolume GC 檢查 | W0.1 委託單註明「**不得啟用**」＋ W0.3 加第 5 條驗收；W1.1／W1.5 加前置檢查 |
| m4 | R5 只驗 happy path | 新增**步驟 6 負面測試**（省略 limits 的 VM 應被 admission 拒收） |
| m5 | R5「讀 memory overhead annotation」前提可疑 | 改為**相減法**推算（compute 實際 request − 4Gi） |
| m6 | R6 缺順序、R11 缺時序 | R6 明寫「先 Halted → 套 NP → 再 Always」；R11 移到**路徑 C 之前** |
| m7 | helm 命令缺 context、缺 virtctl 前置 | W1.5 helm 命令補 `--kube-context woow-k3s`；W1.0 補下載 virtctl（與 server 版本一致） |
| m8 | R5 判定門檻無容差 | 改為區間（記憶體 5.0–6.0 GiB、CPU 2.5–3.5） |
| **m9** | **方法名勘誤**：`_poll_service_status` 不存在 | 全文修正為 **`_poll_and_update_status`**（`cloud_service.py:1218`），行號 `1219-1303` → `1218-1303` |

#### ⚠ Design 檔勘誤（本次順帶發現）—— ✅ **已於 2026-07-28 D1–D11 回寫 PR 一併收掉**

`docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md` 有兩處同源錯誤，**本 plan 當時已修正，design 檔已於本次回寫 PR 收掉**：

1. §4.1 表格與附錄 A 的 `_poll_service_status` → 實名 **`_poll_and_update_status`**
2. 同兩處的 `src/models/cloud_service.py:1219-1303` → **`:1218-1303`**（`def` 在 1218）

另外 design §4.1「兩個問題」一節只點出 model 層的 fail-open，**遺漏 controller 層的同型複製品**（`paas.py:2742-2749`）——這是 M4 的來源，**已於本次回寫 PR 補進 design §4.1 與附錄 A**。

---

## 附錄：本 plan 引用的位置

| 位置 | 用於 |
|---|---|
| `docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md` | 設計母本（§3 chart／§4 operator／§6 quota／§9 風險／§12 wave 骨架） |
| `src/__manifest__.py:3` | 版號：plan 撰寫時 `.79`；實際 W5a `.81`／W5b `.82`（#424① 先落地致順延） |
| `src/models/cloud_app_template.py:122-126` | `min_storage_gb`（`workload_type` 接在其後） |
| `src/models/cloud_service.py:703-741` | helm values 注入段（W5a.4 的 `runStrategy` 落點） |
| `src/models/cloud_service.py:874-926` | `_inject_storage_class`（:918 預設 fallback、:904-915 顯式 entry 慣例） |
| `src/models/cloud_service.py:1218-1303` | `_poll_and_update_status`（:1261 是 model 層 fail-open） |
| `src/controllers/paas.py:2694` | `_update_service_status`（**model 層邏輯的第二份複製品**；:2742-2749 是 controller 層 fail-open） |
| `src/controllers/paas.py:2172` / `:2417` | 上者的兩個呼叫點（workspace 服務清單／服務詳情 poll） |
| `.sandbox/post-create.sh:32-44` | stg 沙盒**探索**共用 operator（不自建）——W4.6 存在的理由 |
| `.claude/skills/build-operator/SKILL.md` | operator build 與 stg pin 流程（`paas-app-bundle-cd/paas-operator-stg/values-stg.yaml`） |
| `extra/paas-operator/src/services/helm.py:1028` | `create_namespace` 呼叫 `ensure_registry_secret`（PoC ns 沒有這一步 → M3） |
| `extra/paas-operator/src/services/helm.py:1135-1142` | baseline `allow-cloudflared` 用 `kubernetes.io/metadata.name`（不可偽造 → M2 的雙 ns 方案） |
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
