# project f4231 pods empty fail closed

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_f4231_pods_empty_fail_closed`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："F-423-1 評估→Step1 PR#490（pods=[] 改 fail-closed）＋Step2 issue#489；重估被擱置的 fail-open 要兩邊都重算"

2026-07-31。F-423-1（容器就緒判定 `pods=[]` 回 True 的 fail-open，#423 W5a 刻意 v1 不修）評估後拍板修：**Step 1 = PR#490**（`fix/f423-1-pods-empty-fail-closed`，base `c935df48`，未 merge）、**Step 2 = issue#489**（部署 age timeout）。承 [[project_484_named_failure_fields]]。

**方法論（最可複用）：重估一個被擱置的 fail-open，要把「代價」與「收益」兩邊都重算，別只驗證原始理由。** 本案原理由是「改 fail-closed 會讓所有現存租戶服務多卡一輪 poll」，兩邊都錯：

- **代價被高估**：poll 只撈 `deploying`/`upgrading`/`initializing`，所以「多卡一輪」只發生在**正在轉移**的服務上，已 `running` 的機隊零影響；一輪＝前台 5s／cron 60s。
- **收益被低估**：`pods=[]` **不必然短暫**——ResourceQuota 拒絕 pod 建立時 ReplicaSet 產不出 Pod 物件（`FailedCreate`），空陣列永久持續；且誤報**黏住**（寫進 running 後沒東西再 poll），15 個 template 有 13 個 `init=none`＝連 post-deploy init 那層巧合的網都沒有。

**找到已發生的實例＝評估的決勝點**：#456 兩筆 hermes-agent 永久壞掉但 DB/UI 顯示 Running。因果鏈是自己補的——全 codebase 寫 `state='running'` 只有 6 處，該 template `init=none` ⇒ 唯一路徑就是這個判定回 True。**「找 issue 裡的既有症狀來反推」比抽象論證有力得多。**

**實測推翻 code 註解（別採信「所以不會怎樣」型的斷言）**：舊註解說「容器端對 pods 為空回 True，所以 VM 合取不會讓純 VM 卡住」。實測 [[reference_virt_launcher_carries_instance_label]]——virt-launcher pod **帶** `app.kubernetes.io/instance`、回報 `2/2`，純 VM 的 `pods` 從來不是空的。

**翻 fail-open 會炸掉「編碼了舊前提」的 fixture**：3 條 W5a VM 測試（`test_2` / `datavolume` / `test_10`）用 `pods=[]` 建 VM 就緒情境而紅——不是修錯，是 fixture 模擬了一個不存在的世界。補 `_VIRT_LAUNCHER` 而非放寬實作。**改判定前先 grep 誰的 fixture 依賴舊行為。**

合併前置閘＝15 template／9 相異 chart 逐一實證 pod 帶 instance label（7 支 live＋render 雙證據，redis 與 mcp-server 僅 render）。驗證要取 **workload 的 `spec.template.metadata.labels`**（只標在 Deployment 外層不算，operator 抓的是 pod）。render 用本地 `woow-paas-charts` checkout 免 registry 認證（jcr-prod 需 auth，且讀 pull secret 會被權限 classifier 擋）。

RED 4/27 → GREEN 37/37；全量 1998 vs baseline 1997 兩向差集皆空；repo-lint 9 gates 全綠。
