---
title: "租戶 pod 的 egress gate 與節點迴避——用 operator 的 helm post-renderer 一次涵蓋所有 chart"
category: decisions
status: active
created: 2026-10-17
source: 商用驗收 AI-FIX-NET-1（缺陷 X-SEC-003-1／AI-PI-004-5／AI-LLM-011-1／DATA-NET-001-1／INFRA-XSVC-007-1；#1497）
---

# 租戶 pod 的 egress gate 與節點迴避

> **一句話**：k3s 的 kube-router 在 pod 啟動**之後**才補 NetworkPolicy 的 iptables，新 pod 最初約 2 秒不受
> `paas-baseline-egress` 約束；同時租戶 pod 沒有任何機制避開正式站所在的節點。兩件事都在 operator 的 helm
> post-renderer 裡處理——所有 chart（現有與未來）一次涵蓋，不逐 chart 改版。

## 問題（實測，不是推測）

1. **啟動空窗（#1497）**。在複製自正式站 `paas-baseline-*` 的沙盒 ns 建新 pod，行程一啟動就每 100 ms 做 TCP connect：
   12 個「應被擋」的目標（他 ns 的 pod、k8s API、Rancher、Loki、三台節點的 kubelet 與 ssh、雲端 metadata）中有 11 個在 **12/12** 個 pod 裡都有成功連線
   （169.254.169.254 從第 0 秒就被擋），最後一次成功落在啟動後 **1.2–2.3 秒**（`evidence/AI-FIX-NET-1/window-summary.json` 的 `control`）。同 ns 的服務也有反向空窗：
   新 pod 連同 ns 既有 pod 在前幾百毫秒被拒（12 次中只有 2 次一開始就通）。可以執行任意碼的 SKU（Code Server、pi-agent、
   n8n Code 節點、open-design console）在 Redeploy 取得新 pod 時可利用這段空窗。
2. **節點落點（INFRA-XSVC-007）**。QA 範圍 31 個 active pod 中 14 個在 `ubuntuserver-59mqj`（全部 `paas-ws-*` 租戶 pod 30 個），
   這台是正式站與 Artifactory 所在、10-07 曾 k3s 程序停擺的節點；租戶 pod 的 `spec.affinity` 是空的、節點沒有 taint。

多播（`224.0.0.0/4`）不需要修：`paas-baseline-egress` 早已排除（正式站 54/54 個租戶 ns 實測），pi 243 的 mDNS／SSDP 實測沒穿出去。

## 選項

| 選項 | 為什麼不選／選 |
|---|---|
| CNI 端提早套規則 | kube-router 的行為，不在我們手上；換 CNI 是叢集級決定 |
| 逐 chart 加 init container（`shared/` helper） | 要改 17 個 chart、每個都升版、每個服務都要各自 Redeploy 才生效；新 chart 漏加就是新洞 |
| mutating admission webhook | 多一個叢集級元件，掛掉會擋住所有租戶 pod 建立；比 helm 層更難驗證 |
| 節點 taint 59mqj | 需要主機操作核准，且 taint 會連平台自己的 pod 一起影響；nodeAffinity 只作用在租戶 |
| **operator 的 helm post-renderer（採用）** | 已經存在（#317 PVC-keep、#1407 live reuse），install／upgrade 一定會走；一處改動涵蓋全部 chart；預設關、可用 kill switch 回退 |

## 設計

`extra/paas-operator/src/tenant_workload_policy.py`（純函式），由 `pvc_keep_post_renderer.py` 的新階段呼叫：

* **egress gate**：每個 pod template 的 `initContainers` 最前面插 `woow-egress-gate`（busybox:1.36，與現有 chart 的 init container
  同一個映像；non-root、drop ALL、唯讀根檔案系統、seccomp RuntimeDefault，資源與 ephemeral-storage 明寫）。反覆
  `nc -z -w 1` 探測 k8s API ClusterIP（`KUBERNETES_SERVICE_HOST:PORT`，每個 pod 都有），**連續兩次連不到**才算 baseline 已生效、
  容器結束，應用容器因此從第 0 秒就在規則之內（同時也消掉反向空窗）。
  * 逾時（預設 60 秒）：`proceed`（預設，退回修補前行為，CNI 故障時不讓所有服務起不來）或 `fail`。
  * chart 自己的 NetworkPolicy 若放行 k8s API（open-design console 明確放 `10.43.0.1/32`），改探測 `status.hostIP:10250`
    （節點 kubelet，空窗期間實測連得到、baseline 的 `192.168.0.0/16` 擋它）；兩個都被放行就略過。pod template 加
    annotation `paas.woowtech.io/egress-gate: skip` 可逐一退出。
  * 不插：`hostNetwork`、KubeVirt VM（virt-launcher 由 KubeVirt 建）、PSA 例外 ns（管理者用的 Omnigent）。
  * 非 helm hook 的 `Job`／bare `Pod`（template 不可變，已存在的物件被改 `helm upgrade` 會整個 immutable 失敗；
    2026-10-17 netpol-startup-window 補洞）：operator 每次 install／upgrade 讀一次 `kubectl get jobs,pods`，
    叢集上**還沒有**同名物件（新安裝、Job 已被 TTL 清掉）就照常插；**已存在**就照叢集上那份渲染——有 gate 原樣帶回
    （設定之後改過也帶回舊的那份）、有節點迴避就帶回叢集上的 affinity、沒有就不加；讀不到叢集現況＝沿用舊行為不動。
  * 只在 `TENANT_BASELINE_EGRESS_ENABLED` 也開、且該 ns 真的有 `paas-baseline-egress` 時才插（否則 gate 永遠等不到）。
* **節點迴避**：`TENANT_AVOID_NODES`（逗號分隔）→ 每個 pod template 加 required `nodeAffinity` `hostname NotIn [...]`。
  `nodeSelectorTerms` 之間是 OR，所以條件要 AND 進**每一個**既有 term；`nodeName` 釘死或 `nodeSelector` hostname 與清單矛盾的 pod 不動。
  選 required 而不是 preferred：驗收標準是「0 個落在 59mqj」，preferred 保證不了。
* **reset-config 不得因此滾動重啟**：INFRA-HS-002-1 的教訓——一次改 pod template 的密碼重設會撞 RWO 重新 attach 與 atomic 15 秒。
  reset 兩條路徑傳 `tenant_policy_mirror=True`：已存在的 workload 只在**叢集上已經有**該項時才套，沒有就保持現狀；新物件照常套。
  其餘 upgrade（Redeploy、換方案、換版）預設「採用」＝該服務的 pod 會重啟一次。
* **失效安全（fail-open）**：政策階段的任何例外都原樣放行 manifest，不會讓租戶的 install／upgrade 因政策 bug 失敗。

## 沙盒實證（正式映像、正式叢集、複製自 operator 自己的 baseline builder）

`evidence/AI-FIX-NET-1/`（商用驗收工作區）：同一個 Deployment，控制組只有手加節點迴避、實驗組經真的 post-renderer 渲染。

| | 控制組（無 gate） | 實驗組（gate） |
|---|---|---|
| 新 pod 數 | 12（31zix 8、z0v3w 2、pggrg 2） | 12（同樣分佈） |
| 有任何一個應被擋目標連得上的 pod | **12/12**，最後成功在 1.2–2.3 秒 | **0/12**，第一個 tick 就被擋 |
| 同 ns 服務（反向空窗）一開始就通 | 2/12 | 12/12 |
| 落在 59mqj 的 pod | 0（手加 affinity） | 0（affinity 來自 post-renderer） |
| 新 pod 建立到主容器 running | 約 1 秒 | 6–11 秒（**+5–10 秒**） |

* 代價：每個新 pod 多 5–10 秒（空窗本身約 2 秒＋兩次被擋的探測各約 1 秒——busybox `nc` 被擋時固定等約 1 秒，`-w` 無法縮短）。
* 17 個真實 chart 以 `helm template` → post-renderer → `kubectl apply --dry-run=server`（PSA baseline enforce）：全部通過，
  gate 不增加任何 PodSecurity 警告。逾時兩條路徑（proceed／fail）在沙盒實測。

## 已知限制（沒有被這個變更涵蓋）

* 不是 helm 建立的租戶 pod：KubeVirt 的 virt-launcher；operator 在執行期建立的資料取回 Pod（`services/data_retrieval.py`，
  平台自己的 nginx、自帶 egress 全擋的 NetworkPolicy，不跑租戶程式碼）；chart 內程式在執行期建立的 pod——Omnigent managed
  runner 由 omnigent chart 0.1.9 自己插同一個 gate（woow-paas-charts #226）。叢集層通用解（所有 `paas-ws-*` 新 pod 由
  MutatingAdmissionPolicy 或自建 webhook 注入）需要 k3s feature gate 或新元件，另案決策。
* 既有 pod：政策只在之後的 install／upgrade 生效，要等該服務下一次 Redeploy（會重啟一次）。
* gate 容器本身在空窗內執行——它是我們的程式碼，不是租戶的。
* 上線順序與驗收：見 PR 描述；上線後由各 QA 實例所屬 lane 以 RETEST 重跑原測項。
