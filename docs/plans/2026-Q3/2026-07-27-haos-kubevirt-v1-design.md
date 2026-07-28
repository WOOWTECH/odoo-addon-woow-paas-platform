---
title: "HAOS × KubeVirt Cloud Service v1 設計案（#423）"
category: plans
status: draft
created: 2026-07-27
updated: 2026-07-27
related:
  - docs/decisions/patterns/README.md
  - docs/plans/2026-Q3/2026-07-12-336-ha-paas-provision.md
  - docs/plans/2026-Q3/2026-07-07-w1-ns-quota-governance-design.md
  - docs/plans/2026-Q3/2026-07-08-317-chart-owned-pvc-keep.md
---

# HAOS × KubeVirt Cloud Service v1 設計案（#423）

> **上游**：[#423](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/423) 可行性評估已完成、結論「可行 (a)」、fallback 條款不觸發；Eugene 於 2026-07-27 拍板 v1 scope（該 issue 最末留言為權威）。
> **本檔範圍**：只寫 design。implementation plan 為下一步（本檔通過 review 後才寫）。
> **本檔不含任何叢集寫入**：撰寫期間對 `woow-k3s` 僅執行唯讀查詢（`get`），未安裝、未建立任何資源。

---

## 0. 依據分級（引用紀律）

#423 的材料混合了三種性質，本檔每一項數字都標明出處等級，**不可混為一談**：

| 級別 | 意義 | 本檔標記 |
|------|------|----------|
| **實測** | 有真實 `kubectl` / `virsh` / benchmark 輸出佐證 | 「實測」＋日期＋出處 |
| **設計論述** | 模型憑既有知識或二手文件產生，零實測 | 「設計論述」／「二手」 |
| **本檔實查** | 本檔撰寫當下（2026-07-27）由作者親自唯讀查證 | 「本檔實查」 |

### 0.1 可引用為實證的事實（實測）

| 事實 | 值 | 出處 |
|------|----|------|
| 四節點 KVM | 4/4 皆有、**全裸機**（hypervisor flag=0）、AMD EPYC 7713、`svm`+`npt` 完整、`/dev/kvm` (10,232) | #423 §2，2026-07-25 |
| 叢集 k8s 版本 | `v1.34.5+k3s1` | #423 §3.1；本檔實查複驗 |
| Longhorn IO（容器直寫 PVC） | SQLite commit p50 **3.443 ms** / p99 **4.880 ms** / **284 commits/sec**；4K fsync p50 3.487 ms / p99 4.736 ms | #423 2026-07-26 留言 |
| local-path 對照組 | 0.306 / 0.367 ms、3,244 commits/sec（Longhorn 慢 11.3–13.3×） | 同上 |
| #422 pod 額度 | maxPods 110 → **200**、Pending 殘留全清 | #423 2026-07-27 留言 |
| Elmo 真實 VM 規格 | **2 core / 4Gi RAM（requests=limits）/ 64Gi PVC** | #423 Elmo 材料抽取，process `6c827424`（有真 kubectl 輸出） |
| Elmo 版本組合 | KubeVirt **v1.8.0** + CDI **v1.64.0** + virtctl 1.8.0，跑在 K3s v1.34.3/1.34.5 | 同上 |
| UEFI 為開機硬需求 | 缺 `firmware.bootloader.efi.secureBoot: false` 會卡在 `Booting from Hard Disk...` | 同上；HAOS 官方文件亦明載 |
| 網路組態 | `masquerade` + `pod: {}`（不需 Multus） | 同上 |
| Service selector | `kubevirt.io/domain: <vm-name>`（KubeVirt 自動加在 virt-launcher Pod 上） | 同上 |
| MQTT/ngrok 測試 | 基本 pub/sub/QoS/retained 通過；**10KB payload 失敗未 debug**、**LWT 未驗到** | #423，process `aab03578` |

### 0.2 明確標為設計論述／二手（**不可**當已驗）

- 「HAOS qcow2 解壓後約 **34Gi**」— #423 自己標註為二手來源（KubeVirt user-guide／HA 論壇），非實測。
- 「還原 30–60 分鐘」— 同上，二手。
- 「VM 層 virtio-blk + qcow2 額外開銷約數十 %」— 設計推論，**未實測**。
- 「virt-launcher 有 3 個容器」— #423 §3.5 的觀察；DataVolume（PVC-backed）路徑下可能只有 2 個（無 container-disk），**未實測**（見 §9 R5）。
- 「NetworkPolicy 對 virt-launcher 零改動」— #423 §3.4 的架構推論，**未在真 KubeVirt 上驗過**。

### 0.3 本檔實查（2026-07-27，唯讀）

| 查證項 | 結果 | 意義 |
|--------|------|------|
| KubeVirt / CDI CRD | **不存在**（`get crd \| grep -iE 'kubevirt\|cdi\|datavolume\|virtualmachine'` 無輸出） | 前置依賴仍未安裝 |
| StorageClass | `local-path (default)`、`longhorn (default)`、`longhorn-build`、`longhorn-delete`、`longhorn-static` | **雙 default 隱患仍在**（#423 §3.3 發現，至今未收斂） |
| `longhorn-delete` 參數 | `{"numberOfReplicas":"2","staleReplicaTimeout":"2880"}` | ⚠ **平台預設 SC 是 2 副本，不是 3** — 修正 §8 容量帳 |
| `longhorn` 參數 | `numberOfReplicas: 2`、`fsType: ext4`、`dataLocality: disabled` | 同上 |
| 節點 pod 額度 | 4 × 200；實際使用 31zix 14 / 59mqj 89 / pggrg 104 / z0v3w 73（**共 280 / 800**） | #422 確已解，virt-handler DaemonSet 有充裕空位 |
| 節點規格 | 4 × 256 vCPU / ~125 GiB RAM | 算力充裕 |
| Longhorn 容量 | 4 × 688 GiB＝**2752 GiB 總量**，**目前可用 2342 GiB** | §8 密度上限的權威輸入 |

---

## 1. v1 Scope（照 #423 拍板，不擴張）

**v1 ＝ stock HAOS VM ＋ CF tunnel 曝露 ＋ 租戶自行 onboarding。**

### In

1. KubeVirt 開通 stock HAOS VM（官方 qcow2、UEFI、masquerade、**2C / 4Gi / 64Gi** 起點規格）
2. CF tunnel 曝露 **8123**（沿用現行 `paas-cloudflare-system` 模式；Service selector 用 `kubevirt.io/domain`）
3. 租戶自行走 HAOS 原生 onboarding（含自裝 add-on；**平台不進 guest**）
4. Golden image 策略（CDI 供給，避免每租戶外網下載＋展開）
5. 三個差異點的平台改動：**VMI 就緒判定**、**runStrategy 啟停映射**、**VM 型 quota/LimitRange 建模**
6. 容量成本建模 → 定價輸入

### Out（明確排除，本檔不設計）

- 平台自動進 guest 操作（裝 add-on／注入設定）＝ v1+ 另案
- WOOWTECH rebrand image（Google Drive 無 artifact 通路＋版本跨層綁死，硬阻塞未解）
- MQTT 對外曝露 ＝ 另案，與 #421 議題 4 的 Headscale package 合併評估
- live migration、Pod/VM snapshot 商品化
- 通用 VM 服務（本案是 **HAOS Cloud Service**）

> **本檔新增的第 4 個平台改動**：拍板列了三個差異點，但 **delete 語意**（CDI DataVolume 打破 #317 的 PVC keep 假設）與 **operator RBAC**（`kubevirt.io` / `cdi.kubevirt.io` 缺 apiGroup）不是 scope 擴張，而是「開通一台 VM」這件事本身不做就會壞的必要條件。#423 §3.6 已把 delete 標為「⚠ 需驗證，是真實新失效模式」。詳見 §4.3 / §4.4。

---

## 2. 跨 repo 分工

```
┌─────────────────────┐   ┌──────────────────────┐   ┌────────────────────┐
│ woow-paas-charts    │   │ woow_paas_platform   │   │ 叢集運維            │
│ （chart 本體）       │   │ （本 repo，平台側）   │   │ （machines-operator）│
├─────────────────────┤   ├──────────────────────┤   ├────────────────────┤
│ charts/haos/*       │   │ CloudAppTemplate seed│   │ KubeVirt v1.8 安裝  │
│ charts/paas-operator│◀──│ operator 三差異點+   │   │ CDI v1.64 安裝      │
│   clusterrole RBAC  │   │   delete 語意        │   │ 雙 default SC 收斂  │
│                     │   │ plan seed + M2M link │   │                    │
│                     │   │ _inject_storage_class│   │                    │
└─────────────────────┘   └──────────────────────┘   └────────────────────┘
          ▲
          │ image
┌─────────────────────┐
│ woow-paas-ci        │
│ HAOS golden OCI 打包 │
└─────────────────────┘
```

| Repo / 單位 | 交付物 | 備註 |
|---|---|---|
| **woow-paas-charts** | ① `charts/haos`（新 chart v0.1.0）：DataVolume + VirtualMachine + Service + NetworkPolicy<br>② `charts/haos/tests/*.sh` render 契約測試（對齊 `charts/home-assistant/tests/` 四支既有測試的形制）<br>③ `charts/paas-operator/templates/clusterrole.yaml` 補 KubeVirt/CDI apiGroups | operator 自己的 chart 也住這個 repo，故 RBAC 改動落此 |
| **woow_paas_platform**（本 repo） | ① `src/data/cloud_app_templates.xml` 新 CloudAppTemplate（slug `haos`）<br>② `src/models/cloud_app_template.py` 新增 `workload_type` 欄位<br>③ `extra/paas-operator/` 三差異點 + delete 語意<br>④ `src/models/cloud_service.py` `_poll_service_status` VM 分支、`_do_suspend`/`_do_resume` 相容、`_inject_storage_class` path_map<br>⑤ `src/data/subscription_products.xml` plan seed ＋ `subscription_template_links*.xml` M2M | seed 改動會動 `check_seed_topology` baseline（`scripts/audit/repo-lint.py:316`），需同步 `--update-baseline` |
| **woow-paas-ci** | HAOS 官方 qcow2 → OCI containerDisk image，推 `jcr-prod.woowtech.io/woow-paas-docker-local/haos` | 與 `Dockerfile.open-design` 同 repo 慣例 |
| **叢集運維**（`woow-machines-operator`） | KubeVirt v1.8 + CDI v1.64 安裝（官方 operator YAML，無 Helm chart）；apiserver `--allow-privileged=true`；雙 default SC 收斂 | **v1 的硬前置**，本 repo 不碰（CLAUDE.md 共用叢集鐵律） |

**分工判準**：沿用既有慣例——chart 本體一律進 `woow-paas-charts`（`home-assistant` / `mcp-server` / `open-design` / `agent-runtime` 皆是），平台只透過 CloudAppTemplate 的 `helm_chart_name` + `helm_chart_version` + `helm_default_values` 釘住並覆寫。

---

## 3. Chart 結構（`woow-paas-charts/charts/haos`）

### 3.1 命名與檔案佈局

chart 名取 **`haos`**（不是 `home-assistant`）——現有 `charts/home-assistant` 是容器版 HA（woowtech-ha image），兩者是不同產品、不同交付形態，**同名會讓 CloudAppTemplate 的 chart pin 與 image pin 語意糊掉**（#336 的 chart↔image 原子變更鐵律就是被這種耦合咬過）。

```
charts/haos/
├── Chart.yaml            # version 0.1.0；appVersion = HAOS OS 版本
├── values.yaml
├── templates/
│   ├── _helpers.tpl
│   ├── datavolume.yaml   # CDI DataVolume（golden image → 租戶 disk）
│   ├── virtualmachine.yaml
│   ├── service.yaml      # ClusterIP:8123，selector kubevirt.io/domain
│   ├── network-policy.yaml
│   └── NOTES.txt
└── tests/
    ├── test-vm-firmware.sh        # secureBoot=false / UEFI 必存在
    ├── test-masquerade-ports.sh   # ports 顯式列且僅 8123
    ├── test-service-selector.sh   # selector = kubevirt.io/domain
    ├── test-dv-keep-annotation.sh # DataVolume 帶 resource-policy: keep
    └── test-labels-passthrough.sh # VMI template labels 含 app.kubernetes.io/instance
```

### 3.2 DataVolume → VM 的順序表達

**方案比較：**

| 方案 | 機制 | 順序保證 | PVC 生命週期 | 判定 |
|---|---|---|---|---|
| A. VM 內嵌 `spec.dataVolumeTemplates` | VM controller 自建 DV | 天然正確 | DV/PVC 綁 VM，VM 刪即刪 | ❌ 刪 VM 就沒資料，且 Helm 看不見 DV 這個物件、無法標 keep |
| **B. 獨立 DataVolume + VM 引用（推薦）** | chart 各出一份 CR，VM 的 `volumes[].dataVolume.name` 指過去 | **由控制器收斂**：DV 未 Succeeded 時 VMI 不會啟動 | DV 是 Helm 管理的獨立物件 → 可直接標 `helm.sh/resource-policy: keep` | ✅ |
| C. Helm hook（pre-install Job 等待 DV） | 命令式等待 | 正確但多一顆 Job | 同 B | ❌ 沒必要，KubeVirt 已宣告式處理 |

**採 B。設計論述要點：**

Helm 的安裝順序表（install order）只涵蓋內建 kind；`DataVolume` 與 `VirtualMachine` 同屬 CRD 實例，**Helm 不保證誰先套用**。這在此處不構成問題——順序不是靠 Helm，而是靠 KubeVirt 控制器：VMI 在其引用的 DataVolume 未 `Succeeded` 前不會進入 `Running`。也就是「宣告式順序」取代「命令式順序」。

> ⚠ 這條是 KubeVirt 的既定行為（官方 user-guide 的 DataVolume 用法），但本團隊**未在真叢集驗過**，列入 §9 R7 的 PoC 驗證項。

### 3.3 VirtualMachine 關鍵欄位（草案）

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: {{ include "haos.fullname" . }}
  labels: {{- include "haos.labels" . | nindent 4 }}
spec:
  # 啟停由 values 驅動（見 §4.2）：Always | Halted
  runStrategy: {{ .Values.vm.runStrategy | default "Always" }}
  template:
    metadata:
      labels:
        # ⚠ 必要：KubeVirt 把 VMI labels pass-through 到 virt-launcher Pod。
        # operator 的 status API 以 app.kubernetes.io/instance 撈 pod
        # （extra/paas-operator/src/api/releases.py:416-419），漏掉這行會讓
        # 平台撈到「零顆 pod」→ 誤判成 running（見 §4.1 問題 B）。
        {{- include "haos.selectorLabels" . | nindent 8 }}
    spec:
      domain:
        firmware:
          bootloader:
            efi:
              secureBoot: false        # ← HAOS 硬需求（實測：缺此卡開機）
        cpu:
          cores: {{ .Values.vm.cores }}          # 2
        resources:
          requests:
            memory: {{ .Values.vm.memory }}      # 4Gi
            cpu: {{ .Values.vm.cores | quote }}
          limits:                                 # ← limits == requests（Guaranteed QoS）
            memory: {{ .Values.vm.memory }}
            cpu: {{ .Values.vm.cores | quote }}
        devices:
          disks:
            - name: rootdisk
              disk:
                bus: virtio
          interfaces:
            - name: default
              masquerade: {}
              # ⚠ 顯式列 ports：不列 = 全開。v1 只曝露 HA UI，MQTT(1883) 明確 out-of-scope。
              ports:
                - name: http
                  port: {{ .Values.service.targetPort }}   # 8123
                  protocol: TCP
      networks:
        - name: default
          pod: {}
      volumes:
        - name: rootdisk
          dataVolume:
            name: {{ include "haos.fullname" . }}-root
```

**逐點理由：**

| 欄位 | 值 | 依據 |
|---|---|---|
| `firmware.bootloader.efi.secureBoot: false` | 固定 | HAOS 官方要求 UEFI 且須 **non-secureboot OVMF**；KubeVirt 原生支援、無 workaround（#423 §3.2）。實測缺此會卡 `Booting from Hard Disk...` |
| `masquerade` + `pod: {}` | 固定 | Elmo 實測 manifest；不需 Multus。bridge 模式不支援 live migration（v1 不做，但 masquerade 也不擋未來） |
| `ports:` 顯式列 8123 | 固定 | masquerade 未列 ports 時**全部轉發**。v1 只開 HA UI；MQTT 是另案，不能因為「反正之後要用」就先開 |
| `resources.limits == requests` | 固定 | KubeVirt 對 VM 的建議做法（Guaranteed QoS）；同時消除 §6 洞 1 的 LimitRange admission 拒收 |
| `cpu.cores: 2` / `memory: 4Gi` | values | 拍板起點規格；與 Elmo 實測規格一致 |
| `runStrategy` 由 values 驅動 | values | §4.2 的 suspend/upgrade 打架防護 |

### 3.4 DataVolume（草案）

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: {{ include "haos.fullname" . }}-root
  labels: {{- include "haos.labels" . | nindent 4 }}   # 含 app.kubernetes.io/instance
  annotations:
    # ⚠ #317 的 PVC keep post-renderer 只認 kind: PersistentVolumeClaim
    #   （extra/paas-operator/src/pvc_keep_post_renderer.py:38），DataVolume 不在其中。
    #   chart 自帶 keep 是本 v1 唯一擋住「helm uninstall 連 DV 帶 PVC 一起刪」的機制。
    helm.sh/resource-policy: keep
spec:
  source:
    registry:
      url: "docker://{{ .Values.image.repository }}:{{ .Values.image.tag }}"
      pullMethod: node
      secretRef: {{ .Values.imagePullSecret | quote }}   # paas-registry
  storage:
    accessModes:
      - ReadWriteOnce                 # v1 不做 live migration → RWO 路徑最短
    volumeMode: Block                 # 見下方理由
    storageClassName: {{ .Values.persistence.storageClass | quote }}   # ⚠ 必須顯式，見下
    resources:
      requests:
        storage: {{ .Values.persistence.size }}   # 64Gi
```

**三個關鍵決定：**

1. **`storageClassName` 顯式釘（不可留空）**
   本檔實查：`local-path` 與 `longhorn` **同時**帶 `is-default-class: true`。DataVolume 不指定 SC 時落點不確定，可能掉到 node-local 的 `local-path`（無法遷移、節點故障即資料失聯）。
   chart 的 values 鍵名刻意取 **`persistence.storageClass`**，因為平台的 `_inject_storage_class` 預設 fallback 路徑正是 `('persistence', 'storageClass')`（`src/models/cloud_service.py:918`）→ **零 operator 改動即可吃到注入值**（預設 `longhorn-delete`，`src/models/cloud_service.py:703-704`）。
   仍建議在 `path_map` 補一條顯式 `'haos': [('persistence','storageClass')]`——這是既有慣例（三型 MCP 的註解寫明「Explicit entries … to document intent and guard against default drift」，`src/models/cloud_service.py:904-915`）。

2. **`volumeMode: Block`（非 Filesystem）**
   VM disk 走 block device 少一層 host filesystem，效能優於 filesystem 模式 PVC；且這是未來要做 live migration 時 Longhorn `migratable: "true"` RWX 的必要前提（#423 §3.3 第 3 點）。v1 用 **RWO + Block**：路徑最短、不需新 StorageClass、不觸碰叢集級設定。

3. **`source.registry`（非 `http`）** — 見 §5。

### 3.5 Service

```yaml
spec:
  type: ClusterIP
  ports:
    - name: http
      port: {{ .Values.service.port }}         # 8123
      targetPort: {{ .Values.service.targetPort }}
      protocol: TCP
  selector:
    kubevirt.io/domain: {{ include "haos.fullname" . }}
```

- selector 用 **`kubevirt.io/domain`**（KubeVirt 自動打在 virt-launcher Pod 上，實測），**不是** chart 的 `selectorLabels`。理由：這是 KubeVirt 官方的 Service 對接慣例，且它保證只選到「當前這台 VM 的 launcher」，VM 重啟換 pod 時仍正確。
- `targetPort` 用數字而非具名 port：virt-launcher 的 container port 不由 chart 定義，具名 port 對不上。
- Service 名沿用 release fullname（`svc-{ref[:8]}`）—— CS 共用 workspace namespace，固定名開兩台會撞（`cloud_app_templates.xml` 對 ha-paas/open-design 的既有註解已載明此坑）。
- CF tunnel 端零改動：`generate_service_url()` 產出的 `http://{svc}.{ns}.svc.cluster.local:{port}`（`extra/paas-operator/src/services/cloudflare.py:818-834`）原封可用。

### 3.6 NetworkPolicy

對齊 `charts/home-assistant/templates/network-policy.yaml` 的既有形制：

```yaml
# 1) default-deny（Ingress + Egress），podSelector = chart selectorLabels
# 2) allow：
#    ingress — 僅當 networkPolicy.ingressNamespaces 非空才發 allow 規則；
#              預設空 → 交給 operator baseline NP 治理（#279/#280）
#    egress  — DNS(kube-system:53) ＋ allowAllEgress 預設 true
```

- **podSelector 用 chart 的 `selectorLabels`**（不是 `kubevirt.io/domain`）：這要求 §3.3 的 VMI template labels 確實帶上 chart labels——與 §4.1 問題 B 的修法是同一件事，一石二鳥。
- `allowAllEgress: true` 為預設：HAOS 要對外（整合、更新檢查、HA Cloud）。
- 與 operator baseline NP 的關係：`_apply_baseline_netpol()`（`extra/paas-operator/src/services/helm.py:1089`）四條 ns-wide 規則照常生效（virt-launcher 是普通 pod）。chart 端不重複發 ingress allow，避免兩層打架——這正是 home-assistant chart 的既有決定。

> ⚠ **未驗**：default-deny egress 下 virt-launcher 能否正常啟動（libvirt/virt-handler 走 hostPath socket 而非 pod network，理論上不受影響，但**未實測**）。列 §9 R6。

---

## 4. Operator 差異點（含接點 file:line）

### 4.1 就緒判定：pod → VMI

**現行接點：**

| 層 | 位置 | 現行行為 |
|---|---|---|
| operator API | `extra/paas-operator/src/api/releases.py:390-440` | `GET /releases/{ns}/{name}/status` 回 `{release, pods}` |
| operator k8s | `extra/paas-operator/src/api/releases.py:416-419` | `k8s_service.get_pods(ns, label_selector="app.kubernetes.io/instance={name}")` |
| k8s service | `extra/paas-operator/src/services/helm.py:882` | `kubectl get pods -o json` → `PodInfo(phase, ready="n/m", …)` |
| Odoo poll | `src/models/cloud_service.py:1219-1303` | `_poll_service_status`：`release_status=='deployed'` 且所有 Running pod ready → 翻 `running` |

**兩個問題：**

- **問題 A（已知）**：virt-launcher pod Running ≠ guest 開機完成。pod 一起來就翻 `running`，但 HAOS 還在 import／開機。
- **問題 B（本檔新發現，更嚴重）**：`src/models/cloud_service.py:1261` 的判定是
  ```python
  all_ready = all(...) if active_pods else (not pods)
  ```
  **`pods` 為空列表時 `not pods` 為 True → 立刻翻 `running`**。若 chart 沒把 `app.kubernetes.io/instance` 傳進 VMI template labels，operator 撈到零顆 pod → 服務在 34Gi 還沒 import 完時就被標成「執行中」。這是 fail-open，必須雙層堵。

**設計：**

1. **CloudAppTemplate 新增 `workload_type` 欄位**（`src/models/cloud_app_template.py`）
   ```python
   workload_type = fields.Selection(
       selection=[('container', 'Container'), ('vm', 'Virtual Machine')],
       string='Workload Type', default='container', required=True,
       help='VM 型 workload 的就緒／啟停判定與容器型不同（見 #423 v1 設計）',
   )
   ```
   取顯式欄位而非讓 operator 自動嗅探的理由：operator 每次 status 都多打一次 `get vmi` 是無謂成本，且叢集沒裝 CRD 時的錯誤處理會把「真的沒 VM」與「CRD 不存在」混在一起。顯式旗標讓失效模式可判讀。

2. **operator 新增 VMI 讀取**（`extra/paas-operator/src/services/helm.py`，`KubectlService` 內）
   ```python
   def get_vmis(self, namespace, label_selector=None) -> List[VmiInfo]:
       """kubectl get vmi -o json → phase + Ready condition + nodeName。
       CRD 不存在時 raise KubectlException（呼叫端轉成明確錯誤，不當成「沒有 VM」）。"""
   ```
   以及讀 DataVolume 進度供 UI 用：
   ```python
   def get_datavolumes(self, namespace, label_selector=None) -> List[DvInfo]:
       """phase（Pending/ImportInProgress/Succeeded/Failed）+ status.progress（百分比）。"""
   ```
   底子已存在：`apply_manifest()`（`helm.py:1491`）證明泛用 kubectl 通道可行；本次是加讀取側對稱物。

3. **status API 擴充**：`GET /releases/{ns}/{name}/status?include_vm=true` → 回應多兩個欄位 `vmis: []`、`datavolumes: []`。預設 `false`、既有呼叫端零影響（向後相容）。

4. **Odoo poll 分支**（`src/models/cloud_service.py:_poll_service_status`）
   ```
   workload_type == 'vm':
       ready ⟺ 至少一個 VMI 且 該 VMI status.phase == 'Running'
                且 conditions 有 {type: Ready, status: "True"}
       # fail-closed：vmis 為空 → 維持 deploying，絕不翻 running（堵問題 B）
   workload_type == 'container'（預設）：
       維持現行 pod-ready 判定，一行不動
   ```

5. **就緒語意的誠實邊界**：VMI `Ready` 代表 **QEMU 跑起來了**，不代表 guest 內 HA 的 8123 已服務。v1 明確定義「就緒 ＝ VMI Ready」，前端補一句文案（「VM 已啟動，HAOS 首次開機約需數分鐘」）。要更準需 `qemu-guest-agent`，而 HAOS 是否內建**未知**（§9 R9）——但那也已經逼近 v1 out-of-scope 的「平台進 guest」邊界，v1 不做。

### 4.2 啟停：scale-to-0 → runStrategy patch

**現行接點：**

| 層 | 位置 | 現行行為 |
|---|---|---|
| Odoo | `src/models/cloud_service.py:1409-1444`（`_do_suspend`）/ `:1446-1484`（`_do_resume`） | 呼叫 `client.scale_release(release, ns, replicas=0/1)` |
| Odoo client | `src/services/paas_operator.py:513` | `POST /releases/{ns}/{name}/scale?replicas=N` |
| operator | `extra/paas-operator/src/api/releases.py:443-514` | 撈 Deployment/StatefulSet → `scale_deployment`（`helm.py:1349`）/ `scale_statefulset`（`helm.py:1471`） |

**問題（真實失效，非理論）**：VM release 既沒有 Deployment 也沒有 StatefulSet → `releases.py:477-481` 直接回 **404**；`_do_suspend` 捕捉例外後把服務寫成 `state='error'`（`cloud_service.py:1437`）。**欠費停權路徑會把每一台 HAOS 服務打成 error**。

**設計：**

1. **`scale_release` 改為 workload-type 感知**（`extra/paas-operator/src/api/releases.py:443`）
   ```
   deployments/statefulsets 有 → 現行路徑（一行不動）
   都沒有 → 撈 kubectl get vm -l app.kubernetes.io/instance={name}
             有 → kubectl patch vm {vm} --type=merge \
                    -p '{"spec":{"runStrategy":"Halted"}}'   # replicas == 0
                    -p '{"spec":{"runStrategy":"Always"}}'   # replicas >= 1
   三者皆無 → 維持 404
   ```
   回應 payload 增加 `virtualmachines: "…"` 欄位，與現行 `deployments` / `statefulsets` 對稱。

2. **為何用 `runStrategy` 而非舊的 `spec.running`**：兩者互斥，`runStrategy` 是 KubeVirt 現行推薦。`Halted` ＝ 停機但保留 VM 定義與磁碟（正是 suspend 要的語意）；`Always` ＝ 保持執行。

3. **為何用 `kubectl patch` 而非 `helm upgrade --set vm.runStrategy=Halted`**：
   - suspend/resume 在計費路徑上、頻率高，走 helm 會重跑整個 chart render（含 DataVolume——重算是否會觸發 re-import 未知，屬新風險）
   - `--reuse-values` 系列的坑本 repo 已付過學費（#411 全量 values 管線）
   - 外科式 patch 與現行 `kubectl scale` 的語意對稱、失效面最小

4. **⚠ 必須同時處理的副作用**：`kubectl patch` 讓 live 物件與 Helm stored manifest 漂移 → 下一次 `helm upgrade` 會把 `runStrategy` 打回 chart 預設 `Always`，**把 suspend 中（欠費停權）的 VM 意外喚醒**。
   **對策**：chart 的 `runStrategy` 由 values 驅動（§3.3 已如此設計），Odoo 端在組 helm values 時依 `state == 'suspended'` 決定 `vm.runStrategy`（`src/models/cloud_service.py` 的 values 組裝路徑，對齊 #411「全量 values 管線」）。兩條路徑因此同源、不打架。
   *這條若漏，症狀是「停權的租戶自己活過來繼續用」——沉默且會計費爭議，務必列進 implementation 的必測項。*

### 4.3 delete 語意（#317 的假設在 CDI 下失效）

**現行機制：**

| 環節 | 位置 | 行為 |
|---|---|---|
| keep 標記 | `extra/paas-operator/src/pvc_keep_post_renderer.py:38` | post-renderer 對 **`kind: PersistentVolumeClaim`** 蓋 `helm.sh/resource-policy: keep` |
| uninstall | `extra/paas-operator/src/services/helm.py:673` | `helm uninstall`（依 **stored manifest** 決定刪誰） |
| orphan 標記 | `extra/paas-operator/src/api/releases.py:246-253` | 以 `app.kubernetes.io/instance={name}` 撈 PVC → `label_pvcs()`（`helm.py:1646`）打 `paas.woowtech.io/orphaned-at` |
| 延遲清理 | `charts/paas-operator/templates/pvc-cleanup-cronjob.yaml:72` | 過 90 天寬限 → `kubectl delete pvc` |

**三個失效點：**

1. **DataVolume 不是 PVC** → post-renderer 不會標它 → `helm uninstall` **直接刪掉 DataVolume → cascade 刪掉它 own 的 PVC → 租戶資料當場全滅**。這比 #317 的情境更嚴重（#317 至少 PVC 還在 release manifest 裡有機會被標）。
2. **cleanup cron 直接 `delete pvc`** → CDI 的 DataVolume controller 是該 PVC 的 owner，PVC 被刪會**被重建**（並重新 import 整顆 image）。清理不但沒清掉，還憑空跑一次 import。
3. **orphan 標記撈不到**：`list_pvcs(label_selector="app.kubernetes.io/instance={name}")` 依賴 CDI 是否把 DataVolume 的 labels 複製到它建的 PVC 上——**未實證**。

**設計：**

| 環節 | v1 做法 |
|---|---|
| keep | **chart 在 DataVolume 上直接宣告** `helm.sh/resource-policy: keep`（§3.4）。chart 是我們自己的，顯式宣告即可，不必先擴 post-renderer |
| orphan 標記 | `delete_release` 除了標 PVC，**也標 DataVolume**（新增 `label_datavolumes()`，與 `label_pvcs()` 對稱） |
| 延遲清理 | cleanup cron 增加一段：對帶 orphan label 且過期的 **DataVolume**，`kubectl delete datavolume`（cascade 帶走 PVC）；**不可**對 DV-owned 的 PVC 直接 `delete pvc` |
| 縱深（可延後） | post-renderer 的 kind 白名單加入 `DataVolume` — 防止未來有人新增 VM chart 時忘了寫 annotation |

> **驗證要求**：這三點全部是設計論述。#423 §3.6 已把它標為「⚠ 需驗證，是真實新失效模式（#317 的 keep 機制不能假設直接適用）」。implementation 階段必須用隔離 fixture 實測三條路徑（`helm uninstall` / 直接刪 PVC / 刪 DV），形制對齊 #281 的 PVC orphan-label e2e recipe。列 §9 R7。

### 4.4 operator RBAC（#315 教訓）

`charts/paas-operator/templates/clusterrole.yaml`（本檔實查全文 94 行）**沒有任何 `kubevirt.io` / `cdi.kubevirt.io` apiGroup**。缺這條 → `helm install` 建 VM/DataVolume 當場 Forbidden。

這正是 #315 的原樣重演（當時是 ClusterRole 缺 `limitranges`，e2e 才撞出來）。必補：

```yaml
  # KubeVirt VM lifecycle（#423 v1）
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines", "virtualmachineinstances"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  # CDI DataVolume（golden image → 租戶 disk）
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

> 若日後採 §5 階段 2 的跨 namespace PVC clone，還需要 `datavolumes/source` 子資源權限——v1 不需要。

---

## 5. Golden image 策略

### 5.1 問題

Elmo 的做法（實測）：**每台 VM 各自從 GitHub 下載 ~300MB 再展開、零 dedup**。放到多租戶平台上就是「每開通一台就對外網拉一次、慢且不可控」。

同時 #423 指出的釘版難題：**CDI 的 `http` source 沒有 digest pin 慣例**，與平台「image 一律 `tag@sha256` 雙釘」的治理慣例（woowtech-ha / open-design / 三型 MCP seed 皆是）對不上。

### 5.2 CDI source 選型

| source | 機制 | 版本釘選 | 外網依賴 | 開通耗時 | 判定 |
|---|---|---|---|---|---|
| `http` | 直接抓 HAOS release URL | ❌ 無 digest pin | 每租戶一次 | 下載 + 展開 | ❌ |
| **`registry`（OCI，推薦）** | 從 `jcr-prod` 拉 OCI containerDisk | ✅ **`tag@sha256` 可 pin**，與既有慣例同構 | 零（內網） | 內網拉 + 展開 | ✅ v1 |
| `pvc` clone / `DataSource` | 從叢集內 golden PVC clone | golden PVC 由 registry source 一次建立、內容不可變 | 零 | clone（較快） | ⏭ v1.1 |

### 5.3 v1 決定：階段 1 — registry source + digest pin

**核心手法：把「不可 pin 的 HTTP artifact」一次性轉成「可 pin 的 OCI artifact」。**

1. `woow-paas-ci` 新增一支 build：下載指定版本的 HAOS `.qcow2.xz` → 打包成 OCI containerDisk → 推 `jcr-prod.woowtech.io/woow-paas-docker-local/haos`
2. tag 規則：`{haos-os-version}-{UTC 時間戳}`（例 `18.1-20260801.0930`），對齊平台既有 timestamp tag 慣例
3. CloudAppTemplate seed 的 `helm_default_values.image.tag` 用 **`tag@sha256:…` 雙釘**（與 `cloud_app_ha_paas` / `cloud_app_opendesign` 完全同形）
4. chart 的 `imagePullSecret` 預設 `paas-registry`（operator 在租戶 ns cold-start 建的 dockerconfigjson，`helm.py:1153`）——與 home-assistant chart 的「自足預設」立場一致（agent-runtime 的 e2e 曾因空預設撞 401 ImagePullBackOff）

**附帶好處**：#423 §3.2 指出「拔掉 HAOS 自動更新的 patch 版需要有地方託管與版本治理」——registry source 天然提供這個位置。（v1 out-of-scope 不做 rebrand，故 v1 的 image 內容 ＝ **stock HAOS qcow2 原樣重新封裝**，不改內容。）

### 5.4 v1.1 評估：階段 2 — golden PVC clone

在每個環境維護一顆 golden PVC（`paas-system` / `paas-stg-system`），租戶 DataVolume 改 `source.pvc` clone。

- 收益：省掉每租戶的展開時間
- 成本：跨 namespace clone 需 CDI 的 `datavolumes/source` RBAC，且租戶 ns ↔ 系統 ns 的跨界會踩「租戶 ns 只能被 operator 動」的邊界；golden PVC 本身需要生命週期治理（誰建、誰換版、換版時舊 clone 怎麼辦）
- **判定**：v1 不做。等 §9 R8（首次開通耗時）實測出來，如果 registry source 的開通時間可接受（例如 < 15 分鐘），就不需要階段 2。

### 5.5 ⚠ 必須誠實說清楚的一點：golden image **不省儲存**

`registry` source 與 PVC clone 都是「每租戶一份完整 64 GiB volume」。省的是**外網頻寬與開通時間**，**不是容量**。拍板 in-list 寫的「避免每租戶外網下載＋34Gi 展開」正是這個意思，不要在定價時誤算成儲存折扣。

真正能省儲存的路徑是 **Longhorn Backing Image**（多顆 volume 共用同一唯讀底層映像、只存差異），理論上能把每租戶 128 GiB 降到「差異量 × 副本數」。但本團隊**未評估、未驗證**該機制與 KubeVirt block volume 的相容性 → 列 §9 R10（v1.1）。

### 5.6 開放問題：HAOS OS 版本

#423 §3.2 已抓到 issue 原文的「HAOS 2026.7.2」其實是 **HA Core** 版號；Elmo 材料用的是 **HAOS 17.1**；HAOS 官方安裝頁當時是 **18.1**。v1 必須釘一個明確的 HAOS OS 版本並編進 image tag → 見 §11 Q1。

---

## 6. Quota / LimitRange 建模

### 6.1 現行機制（實查）

| 機制 | 位置 | 值 |
|---|---|---|
| ns LimitRange | `extra/paas-operator/src/services/helm.py:1047-1059` | `default` 與 `defaultRequest` 皆 **500m / 512Mi**（per container）；**未設 `max`** |
| ns ResourceQuota | `extra/paas-operator/src/services/helm.py:1061-1087` | 6-key hard：`requests/limits.cpu`、`requests/limits.memory`、`persistentvolumeclaims: 10`、`requests.storage` |
| quota 計算 | `src/models/cloud_service.py:1486-1506` | workspace 內非 `deleting` 服務的 `allocated_*` 加總 **× 3**，下限 8 vCPU / 8Gi / 100Gi |
| 資源來源 | `src/models/cloud_service.py:1740-1742` | `plan.cs_* or template.min_*` |

### 6.2 兩個洞（#423 §3.5）與解法

**洞 1 🔴（會讓 VM 直接建不起來）**
virt-launcher pod 是多容器，而 **KubeVirt 只在 `compute` 容器上設 resources**。若 compute 只設 request（4Gi）而無 limit，LimitRange 會補上 `limit: 512Mi` → **limit < request → admission 拒收**。

**解**：chart 在 `domain.resources` 顯式設 `limits == requests`（§3.3）。KubeVirt 對 VM 本來就建議 Guaranteed QoS，所以這不是 workaround 而是正解。

**洞 2 ⚠（額度被憑空吃掉 ＝ #380 教訓原樣重演）**
compute 以外的容器（`guest-console-log`，以及若走 containerDisk 還有 `container-disk`）沒有 resources → 各被 LimitRange 補 **500m / 512Mi**，**憑空吃掉 quota 額度**。#380 的教訓就是「sidecar 記憶體不入帳」，這裡是同一個形狀。

**解**：`min_*` 欄位填 **virt-launcher pod 的實際總量**，不是 guest 規格。

### 6.3 min_* 建模（v1 取值）

以「compute（4Gi/2core，limits==requests）＋ KubeVirt memory overhead ＋ 2 個無 resources 的容器各補 500m/512Mi」估：

| 欄位 | v1 取值 | 推算 |
|---|---|---|
| `min_vcpu` | **3** | compute limits 2 core ＋ 2 × 500m ＝ 3.0 |
| `min_ram_gb` | **6.0** | guest 4Gi ＋ KubeVirt overhead（數百 Mi）＋ 2 × 512Mi ≈ 5.3Gi → 保守進位 6 |
| `min_storage_gb` | **64** | DataVolume 宣告值 |

> ⚠ 這是**設計推算**，不是實測。容器數（2 或 3）與 KubeVirt overhead 的實際值必須在 PoC 量出來後回填（§9 R5）。取保守值（3 vCPU / 6 GiB）的理由：估太少會撞 quota 拒收（症狀是開通失敗），估太多只是 quota 上限寬鬆（無實體成本）——**不對稱，所以往寬估**。

**推導出的 workspace quota**（×3，`cloud_service.py:1503-1505`）：單台 HAOS ⇒ 9 vCPU / 18Gi / 192Gi。三個下限（8/8Gi/100Gi）都被超過，故 floor 不生效。開第二台時 quota 自動重算（存活集加總），不會撞牆。`persistentvolumeclaims: 10` 硬上限對「每台 1 顆 PVC」也綽綽有餘。

### 6.4 不動 LimitRange 的理由（與一條守線）

- 現行 LimitRange **只有 `default`/`defaultRequest`、沒有 `max`** → 4Gi 的 limits 可以通過。**v1 不需要改 LimitRange。**
- ns 是 workspace 共用的（一個 `paas-ws-*` 內可能同時有容器型與 VM 型服務），所以 **per-template LimitRange 不可行**——這也是為什麼解法要落在 `min_*` 建模而不是 LimitRange。
- **守線（寫進 chart NOTES 與 pattern doc）**：日後若有人要在 CS ns LimitRange 加 `max`（例如為了防止租戶亂開大容器），**必須同時考慮 VM 型 workload**，否則會一次打死所有 HAOS 服務。這是可機械化的檢查點，適合進 `scripts/audit/repo-lint.py`。

### 6.5 CPU 超賣註記

KubeVirt 預設 `cpuAllocationRatio: 10` → 2 vCPU 的 guest 只 request 200m。**帳面很省，但 guest 真跑滿時會吃實體核。** 本叢集 CPU requests 僅 17–29%（#423 §2.3 實測），短期不是問題，但這是有意識的超賣，定價與 SLA 論述要知道。

---

## 7. CloudService lifecycle 映射

### 7.1 現行狀態機

`src/models/cloud_service.py:113-127`：
`pending → deploying → (initializing) → running` / `error` / `upgrading` / `suspended` / `deleting`

### 7.2 「已建立但關機」怎麼映射

| 方案 | 說明 | 判定 |
|---|---|---|
| A. 複用 `suspended` 表達租戶自主關機 | `suspended` 的語意是**訂閱停權**，`pre_suspend_state` 與 resume 條件全綁在計費上 | ❌ 兩種「停」混在一起，帳務與 UI 都會錯 |
| **B. 新增 `stopped` state** | 明確、可擴充 | ⏭ v1 不需要，但**設計預留** |
| **C. v1 不提供租戶自主開關機（採用）** | VM 建立後 `runStrategy: Always`；只有訂閱 suspend/resume 才 `Halted`/`Always` | ✅ v1 |

**採 C。** 拍板的 in-list 沒有「租戶自主開關機」這個功能；三差異點中的「runStrategy 啟停映射」對應的是**既有的訂閱停權路徑**（§4.2），不是新功能。v1 不新增 state ＝ 不動 Selection、不動前端 badge、不動任何 state 過濾 domain。

**現行機制已相容的部分（實查確認）**：`suspended` 不在 poll cron 的 transitional 清單裡（`cloud_service.py:1198` 只撈 `deploying/upgrading/initializing`），所以停權中的 VM 不會被 poll 覆寫回 `running`。✓

### 7.3 若日後要做 `stopped`（設計預留）

需要動的位置（供 v1.1 估點）：
1. `state` Selection 加 `('stopped', 'Stopped')`（`cloud_service.py:113`）
2. 前端 state badge / 篩選
3. `_compute_ns_quota` 的 domain（`cloud_service.py:1495-1498`）—— **不改**：關機的 VM 仍佔 PVC，而 quota 是「保留額度」不是「實際用量」，關機不該降 quota。這是設計上正確的行為，要在 doc 裡說明以免日後被當成 bug「修掉」。
4. `_poll_service_status` 的 transitional 清單 —— `stopped` 不加入（與 `suspended` 同理）
5. 計費：關機是否折價 ＝ 產品決策，不是技術問題

### 7.4 開通期的長尾與進度可視

首次開通要 import 一整顆 image 到 64 GiB block volume，耗時**未知**（§9 R8）。現行 poll cron 沒有 timeout，服務可能長期停在 `deploying` 而看不出是「正在跑」還是「卡住」。

**設計**：§4.1 已規劃 operator 讀 DataVolume 的 `status.progress`（CDI 原生提供百分比），Odoo 端把它顯示在服務卡片上。這不需要新 state，只是 `deploying` 的附屬資訊。

---

## 8. 容量 / 成本建模（定價輸入）

### 8.1 ⚠ 先修正一個數字

拍板留言寫「64Gi × Longhorn **3 副本** ＝ 192Gi 實體/租戶」。

**本檔實查**：平台預設 SC 是 `longhorn-delete`（`src/models/cloud_service.py:703-704` 的預設值），其參數為 `{"numberOfReplicas":"2",…}`；`longhorn` SC 亦為 2。「3 副本」是 Longhorn **上游預設值**，不是本叢集的實際設定——#423 §3.3 自己寫的也是 2 副本（32 GiB → 64 GiB 實體）。

> **定價應採 128 GiB/租戶**（64 × 2）。若日後為了可用性改成 3 副本，則為 192 GiB——這是一個明確的成本槓桿，值得在定價時當作可調參數。

### 8.2 每租戶佔用

| 資源 | 每台 HAOS VM | 依據 |
|---|---|---|
| PVC 宣告容量 | **64 GiB** | Elmo 實測 manifest；v1 拍板 |
| **Longhorn 實體佔用** | **128 GiB** | 64 × 2 副本（本檔實查 `longhorn-delete`） |
| RAM（pod requests） | ~5.3 GiB | 設計推算（§6.3），容器數待實測 |
| CPU（limits 帳面） | ~3 vCPU | 設計推算；實際 request 因 `cpuAllocationRatio: 10` 更低 |
| pod 格數 | **1**（virt-launcher） | KubeVirt 架構 |

### 8.3 叢集密度上限（本檔實查數字推算）

| 維度 | 現有容量 | 目前已用 | 每台 HAOS | **可再開台數** |
|---|---|---|---|---|
| **Longhorn 儲存** | 2752 GiB（4 × 688） | 已用 410 GiB（可用 **2342 GiB**） | 128 GiB | **≈ 18 台** |
| RAM | ~500 GiB（4 × 125） | — | 5.3 GiB | ≈ 94 台 |
| pod 額度 | 800（4 × 200） | 280 | 1 | ≈ 520 台 |
| vCPU | 1024（4 × 256） | requests 17–29% | 3（帳面） | > 200 台 |

**結論：儲存是唯一瓶頸，而且瓶頸很近。** 實務上 Longhorn 還需保留 rebuild／snapshot 空間，安全水位下**約 15 台**。

這與 #423 Elmo 材料一節的結論一致（「RAM 遠遠不是瓶頸，儲存才是」），但本檔給出的是**現在這座叢集的實際可用量**而非比例論述。

### 8.4 給定價的輸入數字

| 項目 | 數字 | 用途 |
|---|---|---|
| 每租戶硬成本錨點 | 128 GiB Longhorn 實體 ＋ 4 GiB RAM ＋ 2 vCPU（超賣） | 單位成本計算 |
| 不擴容的容量天花板 | **≈ 15–18 台** | **銷售節奏的硬約束**——超過必須先擴儲存 |
| 擴容單位 | 每加一顆 688 GiB 磁碟 ≈ +5 台 | 擴容 ROI |
| 副本數槓桿 | 2 副本 128 GiB → 3 副本 192 GiB（成本 +50%，換可用性） | 可調參數 |
| golden image 的省 | 省外網頻寬與開通時間，**不省容量**（§5.5） | 避免誤算折扣 |
| 對照組：容器版 HA | PVC 5 GiB（`cloud_app_ha_paas` seed）→ 實體 10 GiB | **HAOS 的儲存成本是容器版的 ~13 倍**，定價必須拉開級距 |

> 最後一列是本檔認為最重要的定價訊號：現行 HA PaaS 是 0 元、5 GiB；HAOS VM 是 64 GiB × 2 副本。兩者不能用相近價位並列販售。

---

## 9. 風險與未驗項（implementation 階段驗證清單）

| # | 項目 | 目前狀態 | 驗證方式 | 阻塞性 |
|---|---|---|---|---|
| **R1** | VM 層 IO 開銷 | 容器直寫 PVC 已實測（284 commits/s、p99 < 5 ms，2026-07-26）＝**上界**。VM 多一層 virtio-blk ＋ qcow2→raw，開銷「約數十 %」為**設計推論、未實測** | PoC 裝好 KubeVirt 後，在 VM 內跑**同一組** SQLite/fsync 測試比對 | 中（上界已合格，實測是為了定 SLA） |
| **R2** | 多租戶競爭 | 未測（實測為單租戶、無競爭） | 2–3 台 HAOS 同時跑 recorder 壓測 | 中 |
| **R3** | 64 GiB 大 volume 行為 | 未測（實測用 5 GiB） | 建 64 GiB Block volume，量首次 import 與穩態 | 中 |
| **R4** | recorder 定期 purge（重 IO 批次） | 未模擬 | 長時間 soak | 低 |
| **R5** | virt-launcher 容器數與 KubeVirt overhead 實值 | 設計取保守 3 容器；DataVolume 路徑可能只有 2 | `kubectl get pod -o jsonpath='{.spec.containers[*].name}'` ＋ 讀實際 requests | **高**（直接決定 §6.3 的 `min_*`） |
| **R6** | NetworkPolicy 對 virt-launcher | #423 判定「零改動」＝**架構推論** | PoC 在 default-deny 下驗 VM 能啟動、能對外、tunnel 能進來 | **高** |
| **R7** | DataVolume 刪除語意 | 全為設計論述（§4.3） | 隔離 fixture 實測三條路徑：`helm uninstall` / 直接刪 PVC / 刪 DV；形制對齊 #281 的 PVC orphan-label e2e | **高**（錯了會刪掉租戶資料） |
| **R8** | 首次開通耗時（拉 image → import → VMI Ready → 8123 可用） | 未測 | PoC 分段計時 | **高**（決定 §5.4 要不要做 golden PVC clone） |
| **R9** | HAOS 是否內建 `qemu-guest-agent` | 未知 | PoC 檢查 | 低（v1 就緒判定不依賴它） |
| **R10** | Longhorn Backing Image 能否省儲存 | 未評估 | v1.1 專案 | 低（v1 不依賴） |
| **R11** | CDI 是否把 DataVolume labels 複製到它建的 PVC | 未知 → 影響 orphan 標記能否撈到（§4.3 失效點 3） | PoC 檢查 PVC labels | 中 |
| **R12** | KubeVirt 版本綁 k8s 版本 | v1.8 支援 k8s 1.34/1.35；叢集為 1.34.5 ✓ | 叢集升 k8s 前必須先確認對應 KubeVirt 版本 | 長期治理 |

---

## 10. 前置依賴（不在本 repo 職權內）

| # | 項目 | 現況（本檔實查） | 負責 | 阻塞性 |
|---|---|---|---|---|
| P1 | **KubeVirt v1.8 + CDI v1.64 安裝** | **未安裝**（叢集無任何相關 CRD） | 叢集運維 `woow-machines-operator` | 🔴 硬阻塞 |
| P2 | apiserver `--allow-privileged=true`；virt-handler 為 privileged DaemonSet | 待確認 | 同上 | 🔴 硬阻塞 |
| P3 | pod 額度 | ✅ **已解**（#422，實查 280/800，最熱節點 104/200） | — | 已解 |
| P4 | 雙 default StorageClass 收斂 | ⚠ **仍在**（`local-path` 與 `longhorn` 皆 default） | 叢集運維 | 🟡 chart 顯式釘 SC 可規避，非 v1 阻塞，但屬叢集級隱患 |

> KubeVirt 官方只提供 operator YAML（`kubectl apply -f kubevirt-operator.yaml` → 建 KubeVirt CR；CDI 同理），**無官方 Helm chart**。安裝與升級節奏被 k8s 版本綁死（KubeVirt 支援 release 當下最新三個 k8s 版本）。

---

## 11. 開放問題（留給 review 拍板）

| # | 問題 | 選項 / 本檔傾向 |
|---|---|---|
| **Q1** | 釘哪一版 HAOS OS？ | #423 已釐清「2026.7.2」是 HA Core 版號；Elmo 用 17.1、官方頁當時 18.1。**傾向：取拍板當下的官方 stable，版本編進 image tag 與 chart `appVersion`** |
| **Q2** | golden PVC clone（§5.4）v1 要不要做？ | **傾向：不做**，等 R8 開通耗時實測結果再定 |
| **Q3** | VM 型判定用 `workload_type` 顯式欄位，還是 operator 自動嗅探？ | **傾向：顯式欄位**（§4.1 已論述） |
| **Q4** | `stopped` state v1 要不要預留？ | **傾向：不預留**（§7.2 方案 C），v1.1 再加 |
| **Q5** | 定價：128 GiB/租戶、容量天花板 ≈15–18 台，怎麼轉嫁？是否先限量開放？ | 產品決策；本檔只提供數字（§8.4） |
| **Q6** | chart 名 `haos` 是否確定（vs `home-assistant-os`）？ | **傾向 `haos`**：短、與既有容器版 `home-assistant` 明確區隔 |
| **Q7** | 副本數維持 2 還是為 VM 型 workload 上調到 3？ | 成本 +50% 換可用性；**傾向維持 2**（Longhorn 2 副本 ＋ HAOS 自帶 Supervisor backup 已是兩層） |

---

## 12. 交付切分建議（給下一步的 implementation plan）

> 本節只是估點骨架，實際任務拆解在 implementation plan 寫。

| Wave | 內容 | 依賴 |
|---|---|---|
| **W0** | 叢集運維裝 KubeVirt v1.8 + CDI v1.64（P1/P2） | — |
| **W1** | PoC：在 dev sandbox 手動跑通一台 HAOS VM，逐項回答 R5–R9、R11 | W0 |
| **W2** | `woow-paas-ci` HAOS golden OCI image build ＋ 推 jcr-prod ＋ 取 digest | — （可與 W1 併行） |
| **W3** | `woow-paas-charts`：`charts/haos` v0.1.0 ＋ chart tests ＋ `paas-operator` ClusterRole RBAC | W1（用 PoC 的實測值定 resources） |
| **W4** | operator：`get_vmis` / `get_datavolumes` / status 擴充 / `scale_release` VM 分支 / `label_datavolumes` / cleanup cron 擴充 | W3 |
| **W5** | 本 repo 平台側：`workload_type` 欄位、poll VM 分支、values 的 `runStrategy` 同源、`_inject_storage_class` path_map、CloudAppTemplate seed、plan seed ＋ M2M link（含 `repo-lint --update-baseline`） | W4 |
| **W6** | stg e2e：開通 → CF tunnel 可達 8123 → suspend/resume → delete 三路徑資料保全驗證 | W5 |

---

## 附錄 A：本檔引用的程式碼位置

| 位置 | 內容 |
|---|---|
| `src/models/cloud_service.py:113-127` | CloudService `state` Selection（8 態） |
| `src/models/cloud_service.py:703-704` | 預設 StorageClass ＝ `longhorn-delete` |
| `src/models/cloud_service.py:874-926` | `_inject_storage_class`（預設 fallback `('persistence','storageClass')` 在 :918） |
| `src/models/cloud_service.py:1198` | poll cron 的 transitional state 清單 |
| `src/models/cloud_service.py:1219-1303` | `_poll_service_status`（:1256-1261 是 ready 判定，含問題 B 的 `not pods`） |
| `src/models/cloud_service.py:1409-1444` | `_do_suspend`（:1425 呼叫 `scale_release`；:1437 失敗寫 error） |
| `src/models/cloud_service.py:1446-1484` | `_do_resume` |
| `src/models/cloud_service.py:1486-1506` | `_compute_ns_quota`（×3、floor 8/8Gi/100Gi） |
| `src/models/cloud_service.py:1740-1742` | `allocated_* = plan.cs_* or template.min_*` |
| `src/models/cloud_app_template.py:112-126` | `min_vcpu` / `min_ram_gb` / `min_storage_gb` |
| `src/services/paas_operator.py:513` | `scale_release` client |
| `src/services/paas_operator.py:705` | `get_status` client |
| `src/data/cloud_app_templates.xml:577-620` | `cloud_app_ha_paas` seed（chart↔image 原子變更鐵律的範本） |
| `src/data/subscription_products.xml:491-506` | `plan_ha_paas_monthly`（0 元隱藏 plan 範本） |
| `extra/paas-operator/src/api/releases.py:215-311` | `delete_release`（:246-253 orphan PVC 標記） |
| `extra/paas-operator/src/api/releases.py:390-440` | `get_release_status`（:416-419 pod 撈取） |
| `extra/paas-operator/src/api/releases.py:443-514` | `scale_release`（:477-481 是「無 Deployment/StatefulSet → 404」） |
| `extra/paas-operator/src/services/helm.py:439-531` | `install`（:491 掛 PVC keep post-renderer） |
| `extra/paas-operator/src/services/helm.py:673-692` | `uninstall` |
| `extra/paas-operator/src/services/helm.py:882-937` | `get_pods` |
| `extra/paas-operator/src/services/helm.py:1047-1059` | `_build_limitrange_manifest`（500m/512Mi，無 `max`） |
| `extra/paas-operator/src/services/helm.py:1061-1087` | `update_namespace_quota`（6-key hard） |
| `extra/paas-operator/src/services/helm.py:1089-1151` | `_apply_baseline_netpol`（#279 四條 ns-wide NP） |
| `extra/paas-operator/src/services/helm.py:1153` | `ensure_registry_secret`（`paas-registry`） |
| `extra/paas-operator/src/services/helm.py:1491-1525` | `apply_manifest`（泛用 kubectl 通道的底子） |
| `extra/paas-operator/src/services/helm.py:1646-1674` | `label_pvcs` |
| `extra/paas-operator/src/services/cloudflare.py:818-834` | `generate_service_url`（tunnel ingress 規則來源） |
| `extra/paas-operator/src/pvc_keep_post_renderer.py:38` | keep 只認 `kind: PersistentVolumeClaim` |
| `scripts/audit/repo-lint.py:316` | `check_seed_topology`（seed 改動的 gate） |

跨 repo（`woow-paas-charts`）：

| 位置 | 內容 |
|---|---|
| `charts/home-assistant/values.yaml` | 本 chart 的形制參照（image pin 註解、persistence、networkPolicy、resources、seedResources） |
| `charts/home-assistant/Chart.yaml` | chart 版本演進紀錄與 OPERATOR COUPLING 警語的寫法範本 |
| `charts/home-assistant/templates/network-policy.yaml` | default-deny ＋ allow 的既有形制（`ingressNamespaces` 空則不發 ingress allow） |
| `charts/home-assistant/templates/service.yaml` | Service 形制 |
| `charts/paas-operator/templates/clusterrole.yaml` | ClusterRole 全文（94 行，**無** kubevirt/cdi apiGroup） |
| `charts/paas-operator/templates/pvc-cleanup-cronjob.yaml:72` | `kubectl delete pvc`（對 DV-owned PVC 會被重建） |

## 附錄 B：外部來源

- [KubeVirt getting-started（KVM 需求）](https://github.com/kubevirt/kubevirt/blob/main/docs/getting-started.md)
- [KubeVirt ↔ Kubernetes 支援矩陣](https://github.com/kubevirt/sig-release/blob/main/releases/k8s-support-matrix.md)
- [KubeVirt 虛擬硬體 / EFI 韌體](https://kubevirt.io/user-guide/compute/virtual_hardware/)
- [KubeVirt Service objects（label pass-through）](https://kubevirt.io/user-guide/network/service_objects/)
- [KubeVirt resources requests and limits](https://kubevirt.io/user-guide/compute/resources_requests_and_limits/)
- [KubeVirt node overcommit（記憶體 overhead）](https://kubevirt.io/user-guide/compute/node_overcommit/)
- [KubeVirt live migration（RWX 需求）](https://kubevirt.io/user-guide/compute/live_migration/)
- [CDI（Containerized Data Importer）](https://kubevirt.io/user-guide/storage/containerized_data_importer/)
- [Longhorn RWX volumes](https://longhorn.io/docs/1.10.0/nodes-and-volumes/volumes/rwx-volumes/)
- [Home Assistant OS — 虛擬機安裝（2GB RAM / 2 vCPU、qcow2、UEFI non-secureboot OVMF）](https://www.home-assistant.io/installation/alternative/)
- [Home Assistant OS — generic-x86-64（UEFI 必須、Secure Boot 須關）](https://www.home-assistant.io/installation/generic-x86-64/)
