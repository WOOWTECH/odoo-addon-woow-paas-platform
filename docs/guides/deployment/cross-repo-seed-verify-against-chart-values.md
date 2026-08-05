# reference cross repo seed verify against chart values

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_cross_repo_seed_verify_against_chart_values`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：上架 CS template 時，seed 要直接對 chart 的 values.yaml 逐鍵核對（派工摘要會漏鍵）；multi-arch digest 要先查既有 seed 釘哪一層

跨 repo 上架 cloud service（platform seed ↔ woow-paas-charts）時，三個會靜默出錯的點：

**1. 派工文件的 values 契約摘要不等於 chart 真實 values.yaml。**
#424① 實例：契約摘要寫 `authProxy.basicAuth: true`，chart 卻同時有 `basicAuth`（布林）
＋ `basicAuthUsername`；而且摘要**完全沒提**的 4 個鍵是必寫的——`authProxy.image`
（chart 註解明寫「platform seed re-states it to prevent drift」）、`workerProcesses`
（nginx `worker_processes auto` 不看 cgroup，256 vCPU node 會 fork 爆 128Mi limit →
OOMKilled）、`dashboard.bindHost`（**非 loopback 才讓上游 fail-closed auth gate 保持
啟用**，改 127.0.0.1 是靜默降級成單層防護）、`dashboard.externalPath`（空字串＝不對外
路由，但平台照樣把 URL 當 OAuth redirect_uri 發出去＝靜默死鏈）。
做法：chart 未發佈時用 `git clone --depth 1 --filter=blob:none --no-checkout` ＋
`git fetch --depth 1 origin <branch>` ＋ `git show FETCH_HEAD:<path>` 取單檔（~128K）；
已發佈就 `helm show values oci://…  --version X`。chart 註解常直接點名平台注入點
（storageClass「do not rename」等），比任何二手摘要可靠。

**2. multi-arch image：`docker manifest inspect --verbose` 給的是 per-arch 子 manifest，
不是 index digest。** 兩者都是合法的釘法，但**必須跟既有 seed 一致**：
- index digest 要用 `docker buildx imagetools inspect <ref>` 的頂層 `Digest:`。
- 本 repo 慣例＝**釘 amd64 子 manifest**（實查 `open-design-console:20260720.0332`：
  index `bb802e63…`，而 seed 釘的 `2b91c4ad…` 是 amd64 child）。
- 選 per-arch 的代價是排到他架 node 會 ImagePullBackOff「no matching manifest」——
  woow-k3s 四 node 全 amd64 故無風險，但**本機 docker-desktop 是 arm64 → amd64-only
  的 CS 一律不能在本機沙盒跑 e2e**。

**3. `PENDING` 佔位的白名單必須自清。** 用「顯式例外清單 + 專屬 fail-loud 測試」起手
（SOP380 模式）沒問題，但補釘時**只改 tag、忘了從白名單移除**＝那顆 image 從此永久
跳過全域掃描守衛，且**沒有任何測試會紅**。守法：全域掃描測試末尾反向斷言「白名單內
已補釘者」與「白名單指向 seed 不存在的路徑」皆為空。

參見 [[project_424_item1_phase_b_seed]]、[[project_opendesign_cs_onboarding_380]]、
[[reference_prod_values_digest_pin_precedence]]。
