---
title: "CDI DataVolume GC 必須維持關閉——租戶資料刪除保護鏈的叢集級前提"
category: decisions
status: active
created: 2026-07-31
source: Maestri canvas note「w0-woow-k3s-kubevirt」（W0 委託單，2026-07-28；本檔落地後該 note 退役）
---

# CDI DataVolume GC 必須維持關閉

> **一句話**：woow-k3s 的 CDI CR **不得設定 `spec.config.dataVolumeTTLSeconds`**（必須留空）。
> 開了它，平台整條租戶資料刪除保護鏈會**靜默踩空**——不報錯、但不生效。

## 適用對象

叢集運維（machines-operator）與任何要調整 `cdi` namespace / CDI CR 設定的人。
這是 #423（HAOS×KubeVirt v1）W0 安裝時的叢集級約束，**效期＝只要平台還有 VM 型
cloud service 就永久有效**，不隨 #423 收官而失效。

## 為什麼（保護鏈的前提）

CDI 的 DataVolume GC（`dataVolumeTTLSeconds`）在 import 成功後會**自動刪除
DataVolume 物件、並移除 PVC 上的 ownerReference**。而平台的租戶資料刪除保護鏈
有三層，**全部以「DataVolume 物件持續存在」為前提**：

1. **`helm.sh/resource-policy: keep` 標在 DataVolume 上**（charts/haos，寫死非
   values 驅動）——`helm uninstall` 不得級聯刪掉租戶的 64 GiB 磁碟。DV 被 GC
   收走後這個標記無處可標。
2. **operator 的 orphan 標記**（`paas.woowtech.io/orphaned-at` 等三標）打在 DV
   （CDI 會複製到 PVC，#423 W1 PoC D9 實證）——服務刪除後資料保留 7 天的依據。
3. **cleanup CronJob（charts/paas-operator ≥0.1.20，#423 W4b）刪的是逾期
   DataVolume 而非 PVC**——直接刪 DV-owned PVC 會被 CDI 重建並重跑整個
   multi-GiB import（W1 PoC 實測），所以 GC 必須落在 DV 層。DV 不在了，
   逾期回收就無的放矢。

三層都不會在 GC 開啟時報錯——症狀是「保護不再發生」，只有在租戶資料意外消失或
逾期磁碟堆積時才會被發現。這正是本檔存在的原因：**約束不可機械強制
（CDI CR 在叢集不在 repo），只能靠文件與健檢守住**。

## Trade-off（為何不開 GC）

| 選項 | 得 | 失 |
|---|---|---|
| **GC 關（採用）** | 三層保護鏈成立；刪除語意單一（一律走 operator teardown → orphan → cron） | 已完成 import 的 DV 物件常駐（每租戶一顆，成本≈0） |
| GC 開 | 省下常駐 DV 物件 | 保護鏈靜默踩空；刪除語意分裂（import 完成前後行為不同） |

**日後若真要開**：必須先改平台 design（#423 design §4.3 刪除語意）＋ charts/haos
的 keep 承載位置＋ cleanup cron 的回收對象，並經平台側簽核——不是叢集側可以單方調的參數。

## 現況健檢（機械可判）

```bash
# DV GC 未啟用 → 輸出空（未設）
kubectl --context woow-k3s get cdi -o jsonpath='{.items[0].spec.config.dataVolumeTTLSeconds}'
```

順帶的安裝完整性五檢（W0 驗收信號，日後升級 KubeVirt/CDI 後可重跑）：

```bash
# 1. CRD 三件套存在
kubectl --context woow-k3s get crd | grep -E 'virtualmachines\.kubevirt\.io|virtualmachineinstances\.kubevirt\.io|datavolumes\.cdi\.kubevirt\.io'
# 2. KubeVirt CR → Deployed
kubectl --context woow-k3s get kubevirt -n kubevirt -o jsonpath='{.items[0].status.phase}'
# 3. virt-handler DaemonSet 全節點 → 4/4
kubectl --context woow-k3s get ds -n kubevirt virt-handler -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}'
# 4. CDI CR → Deployed
kubectl --context woow-k3s get cdi -o jsonpath='{.items[0].status.phase}'
# 5. 即上方 GC 檢查
```

2026-07-31 實查：KubeVirt `Deployed v1.8.0`、CDI `Deployed v1.64.0`、GC 未設 ✅。

## 版本組合與回滾（W0 存檔）

- 版本選型：KubeVirt v1.8.0＋CDI v1.64.0＋virtctl 1.8.0（virtctl 須與 server
  完全一致）——k8s v1.34.5+k3s1 支援矩陣＋Elmo 實跑組合。
- 回滾順序：先刪 CR（讓 operator 收斂）再刪 operator YAML；CDI 同理。
- ⚠ 節點層變更外溢前例：k3s restart 曾外溢鄰節點 repmgr/pgpool 自我重啟
  （約 2 分鐘自復原）——動 kubelet 前先盤點跨節點健檢型 workload。

## 相關

- `docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md` §4.3（刪除語意）
- `docs/plans/2026-Q3/2026-07-28-haos-kubevirt-v1-implementation.md` §W0／§W4b
- Gitea issue #423（W0 驗收留言含安裝原始輸出）
