# reference stg db manual upgrade recipe

> ⛔ **舊沙盒已隨 cd#289 退役（2026-10-23）**：下方是已刪除的 `dev-sandbox-stg` 的歷史 recipe，
> Secret／PVC／Job 範本與 binary 路徑都不適用現行 stg，不可照抄或只替換 namespace 後執行。
> 現行平台是 Fleet 管理的 `paas-stg-system/paas-odoo-platform-stg`（DB `woow_stg`），
> 換版走 paas-app-bundle-cd 的 `paas-odoo-platform-stg/values-stg.yaml` image pin PR（`image.digest`＋`image.tag`），
> 人工覆核 merge；chart 的升級閘負責升級與 post-verify，不再以本檔手動 Job 作為 stg 操作入口。
> 正本：[cd#289](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/289)、
> [stg runbook](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/src/branch/main/docs/platform-odoo-stg-runbook.md)、
> [CD SOP](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/src/branch/main/docs/sop-cd.md)。
> 下方保留框架傳播的更正與當時實測，不代表舊資源仍存在。

<details>
<summary>歷史手動 upgrade Job recipe（不適用現行 stg）</summary>

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_stg_db_manual_upgrade_recipe`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：stg dev-sandbox DB 升版（新 seed/功能生效）完整 recipe＋三大陷阱：--image 拿不到新版、升版需手動 upgrade job、job 撞 ResourceQuota

> 🔴 **本檔的定位（2026-08-11 修正）**：這是**手動** upgrade Job 的操作 recipe——Job 怎麼
> clone、怎麼避開 ResourceQuota 與 RWO Multi-Attach、log 在哪、怎麼驗。**這些操作細節全部
> 仍然正確、仍然有用。**
>
> 但本檔原本的**前提**——「deploy-stg 換 image 不會升 addon，所以每次都得手動跑這個 Job」
> ——**已被 2026-08-11 的對照實驗推翻**。`woow_paas_platform` 是 `web` 的 dependent，
> deployment 恆帶的 `-u web` 會經 Odoo 的 reverse-dependency 傳播連帶升它
> （`odoo/modules/loading.py:458-462` → `odoo/addons/base/models/ir_module.py:706-714`，
> 且 `:699-712` 的 `while` 迴圈使其遞移）。機制與對照實驗詳見
> [`stg-upgrade-recipe-dev-sandbox.md` §換 image 會不會升 addon](stg-upgrade-recipe-dev-sandbox.md#換-image-會不會升-addon2026-08-11-定案)。
>
> ⇒ **本檔的 Job 不是 deploy-stg 的必要步驟**，而是下列情境的工具：傳播沒發生（#665
> post-check gate 判紅且已排除「還在跑」與「migration 炸了」）、要單獨升某個模組、
> 或要在換 image **之前**先把 DB 升好。
>
> ⚠ 下方 2026-07/08 的實戰紀錄一律**原文保留**（含當時錯誤的前提），因為 Job 的操作細節
> 就寫在那些敘事裡，而且「當時為何這樣認為」本身是這份 recipe 最值得看見的部分。

stg（`dev-sandbox-stg` / DB `woow_stg`）要讓「某 PR 的新 seed/功能」（例：三型 MCP marketplace template）生效，~~**不是 deploy 換 image 就好**~~（**⚠ 這個前提已於 2026-08-11 被推翻，見上方定位說明**）——marketplace template 是 module seed data，需要跑 `-u woow_paas_platform`。實戰於 2026-07-20 把 stg 從 `.72` 升到 `.73`（PR #377 三型 MCP：Kubernetes MCP / Vibe Kanban MCP / Open Design MCP，`/api/cloud/templates` 端到端驗證 count 9）。**2026-07-21 再用同 recipe 把 stg `.73`→`.74`（#380 Open Design 完整應用 seed）端到端 PASS**：CI `stg-20260721.0906`（97s 快取 build，registry digest `481a3156` 異於 .73 → 確認真新 image、非 paths-filter 跳過）→ `stg-upgrade-74` job（clone `.73` job spec 只改 name/image/logfile，`limits.cpu=800m` quota-safe）→ deploy Recreate roll。DB 驗 `ir_module_module.latest_version=18.0.1.0.74`＋template `woow_paas_platform.cloud_app_opendesign`(id=45) 欄位全符（name=Open Design/slug=open-design/category=ai_llm/port=7457/mcp_enabled=false/has_admin_gui=true）；`/api/cloud/templates` count 10 含 `open-design`（與 MCP server `mcp-opendesign` 並存區分）。注意 build 耗時變異大（12-14s=paths-filter 跳過、88-97s=快取 build、5-6min=冷 build），單看 CI status=success 會被快 build 誤導 → 一律以 registry digest 交叉驗（[[feedback_never_fabricate_tool_output]]）。

**三大陷阱**：

1. **`deploy-stg --image` 用「已存在 image」可能拿不到新功能**：registry 最新 stg tag 可能落後 develop 版號（當時 registry 最新 `20260719.1101`=`.72`，但三型 MCP 需 `.73`）。要新版必須觸發新 CI build（`deploy-stg` 不加 `--image`，或自己 `tea api` 在 `woow-paas/paas-odoo-ci` 建 `stg-*` tag）。stg build 的 addon ref＝**develop**（`paas-odoo-ci/build-image.yml:77-79`：`stg-*` tag 預設 clone develop；`v*` 才是 HEAD/main）。

2. ~~**create.sh / deploy-stg 不自動跑 module upgrade**~~ **⚠ 2026-08-11 更正：這條的觀察對、因果錯。** `create.sh` 確實**不顯式**對 `woow_paas_platform` 下 `-u`（下面「DB 已存在時只 `helm upgrade` + 宣告式 config_params seed」的觀察至今成立），但它下的 `-u web` 會經 reverse-dependency 傳播升到 `woow_paas_platform` ⇒ **module upgrade 其實會跑**。「過去都是手動 apply job」是**當時的作業習慣**，不是「非手動不可」的證據——手動 job 存在只證明有人跑過它，不證明不跑就不會升。原文保留於後：DB 已存在時只 `helm upgrade` + 宣告式 config_params seed（`post-create.sh` 純 psql seed，不碰 module 版號）。過去 stg DB 升版（`.61`/`.68`）都是**手動 `kubectl apply` 一個 upgrade job** 跑 `-u`——證據：既有 `stg-upgrade-68` job 帶 `kubectl.kubernetes.io/last-applied-configuration` annotation、無 ownerReferences。（曾誤判「create.sh 會自動偵測版號差異跑 upgrade job」，執行前查 job spec 才糾正——[[feedback_never_fabricate_tool_output]]：pod `phase=Succeeded` 也不是成功證據，upgrade log 寫 `--logfile` 到 filestore 不在 stdout，要查 DB ground truth：`ir_module_module.latest_version` + 目標 table records。）

3. **upgrade job 撞 ResourceQuota**：`stg-odoo-dev-sandbox-quota` `limits.cpu=4`，已用 3200m（odoo 2 + postgres 1 + nginx 0.2），job 複製 `.68` spec 的 `limits.cpu=2` → 5.2>4 被 `FailedCreate` forbidden，job controller 無限重試建 pod（症狀：job Running 但 `get pods` 無 pod）。解：降 job `limits.cpu` 到 800m（quota 只限 limits 不限 requests；`-u` CPU 密集但慢一點無妨）。改 resources 需 delete job 重 apply（pod template immutable）。

**正確順序（避免額外 restart）**：
1. 觸發 `.73` CI build（`tea api ... /tags` 建 `stg-YYYYMMDD.HHMM`），輪詢 `actions/tasks` 端點（非 runs，見 [[reference_tea_actions_runs_list_completed_only]]）等 success
2. `kubectl apply` upgrade job（範本＝既有 `stg-upgrade-NN` spec，改 name/image/logfile；secret refs 全來自 `stg-odoo-dev-sandbox-secret`）跑 `odoo -u woow_paas_platform --stop-after-init -d woow_stg` → **先升 DB**
3. `deploy-stg --image <新tag> --slug stg --context woow-k3s` → 主 odoo pod roll 到新版（deployment `strategy=Recreate`：先刪舊 pod 釋放 2 core 再建新，**不 surge、不撞 quota**）；roll 後新 pod 直接 load 新 code + 新 DB registry，無需額外 restart
4. 端到端驗證：pod image/版號、`ir_module_module` 版號、目標 table records、`/api/cloud/templates`（回 `{success,data,count}`，auth=user，pod 內 python urllib + `/web/session/authenticate` admin/admin）

**2026-07-22 補：同版號（manifest 未 bump）的 seed 變更同樣要跑 upgrade job。**

> ❓ **2026-08-11 標記存疑（未推翻，也未證實——不要照這條下結論，也不要照這條刪它）**：
> 本段的理由寫的是「Odoo 啟動的版號比對不會觸發 re-seed，只有顯式 `-u` 會重載 XML」。
> 但 `button_upgrade()`（`ir_module.py:681-714`）**完全不比對版號**，是無條件
> `write({'state': 'to upgrade'})`；而 stg deployment 恆帶 `-u web`，每次 pod 啟動都會
> 走一次傳播。照這個機制推，同版號 seed 變更在換 image 後的 pod 重啟時**應該也會**重載
> XML，本段的結論就不成立。
>
> **但我沒有對「同版號」情境做過對照實驗**（2026-08-11 的實驗是 `.123`→`.124`，有 bump）。
> 當時的實測序是「先跑 job 才驗」，所以那次紀錄無法區分「job 讓它生效」與「換 image 本來
> 就會生效」。⇒ 本段暫留原狀。要用它之前，請先做同版號情境的對照實驗（**先換 image、
> 不跑 job，直接驗 seed 是否收斂**），再回來改這段。

stg `.74`(seed chart 0.1.1) → `.74`(seed chart 0.1.2)：#398–#401 全落在 `18.0.1.0.74` 內沒 bump 版號，新舊 image 版號相同 → **Odoo 啟動的版號比對不會觸發 re-seed**，只有顯式 `-u` 會重載 XML。job 命名沿用字母後綴慣例（`stg-upgrade-74` → `74b` → `74c`），clone 前一個 job 的 JSON 用 jq 改 `name`/`image`/`--logfile` 並 `del` 掉 runtime/auto 欄位（`.status`/`.metadata.{uid,resourceVersion,creationTimestamp,generation,managedFields,ownerReferences,labels,annotations}`/`.spec.selector`/`.spec.template.metadata`）再 apply——比手刻 YAML 安全（`imagePullSecrets`/env/volumes/securityContext 全自動保留）。實測 quota：`used.limits.cpu=3200m` + job `800m` = 4000m 剛好等於 `hard=4`（`≤` 允許，不會 FailedCreate）。**且升 DB 後務必接著換 image**，否則舊 image pod 重啟時的自動 `-u` 會把 seed 打回舊值。快 build（~107s 快取）別只看 `status=success`，用 registry manifest digest 對比新舊 tag 交叉驗（`curl -u $JCR_USER:$JCR_PASS -I .../v2/woow-paas-docker-local/paas-odoo-stg/manifests/<tag>` 取 `Docker-Content-Digest`；憑證在 `~/.sandbox/<project-key>/config.env`）。

**2026-07-28 補（`.79`→`.80`，第 4 個陷阱）：clone 的 upgrade job 少了 nodeSelector 會撞 filestore PVC 的 RWO Multi-Attach。** `stg-odoo-dev-sandbox-odoo-filestore` 是 `ReadWriteOnce`（SC `longhorn-delete`），已被 odoo pod attach 在其所在 node；job pod 若被 scheduler 排到**別的 node**，會卡 `ContainerCreating` 直到 timeout，event＝`FailedAttachVolume: Multi-Attach error for volume "pvc-..." Volume is already used by pod(s) stg-odoo-dev-sandbox-odoo-*`。**與 quota `FailedCreate` 症狀不同**（那個是 job Running 但 `get pods` 查無 pod；這個是 pod 存在但永遠 ContainerCreating），別誤診。過去 `.74`–`.79` 沒撞到純屬 scheduler 碰巧排同 node。解法：先 `kubectl get pod -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].spec.nodeName}'` 查 odoo pod 在哪，job JSON 補 `.spec.template.spec.nodeSelector = {"kubernetes.io/hostname": "<該 node>"}`（RWO 是 node-level 語意，**同 node 多 pod 可共掛**，不需停 odoo）。已 apply 的 job 要改得 delete 重 apply（pod template immutable）。實測補 nodeSelector 後 38s Complete。

相關：[[reference_deploy_stg_gotchas]]、[[reference_agent_runtime_respawn_mechanism]]（升版後既存 pod 不自動 roll 的同源現象）、[[reference_prod_odoo_bitnami_module_mgmt]]（prod 版 `-u` 不裝 uninstalled 等）、[[project_opendesign_cs_onboarding_380]]（既有 service 升 chart 的 reuse_values 陷阱）。

</details>
