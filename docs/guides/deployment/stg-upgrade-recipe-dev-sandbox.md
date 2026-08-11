# reference stg upgrade recipe dev sandbox

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_stg_upgrade_recipe_dev_sandbox`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：stg（dev-sandbox-stg）升版三步 recipe——build stg-* tag→upgrade job（nodeSelector 釘同 node 避 RWO Multi-Attach）→helm 換 tag；含 log 位置與硬驗版號的三方證據

> 🔴 **本檔的定位（2026-08-11 修正）**：這是**手動** upgrade Job 的 recipe，**不是
> `/woow:deploy-stg` 的必要步驟**。標準 deploy-stg 換 image 就會經 Odoo 的
> reverse-dependency 傳播升 `woow_paas_platform`（機制見下方
> [§換 image 會不會升 addon](#換-image-會不會升-addon2026-08-11-定案)）。本檔的 Job
> 在「傳播沒發生」「要單獨升某個模組」「要在換 image **之前**先把 DB 升好」這些情境仍然
>有用——但它不是每次部署都要跑的東西。

stg 的 Odoo 在 **`dev-sandbox-stg`**（woow-k3s context），不是 `paas-stg-system`：deploy
`stg-odoo-dev-sandbox-odoo`、pg `stg-odoo-dev-sandbox-postgres-0`、DB `woow_stg`、helm
release `stg`（chart `odoo-dev-sandbox`）。**addon src 烤在 image 裡**，所以**當你選擇走
本檔這條手動路線時**順序不可顛倒：build image → 跑 `-u` job → 換 deploy image。顛倒就是
拿舊 code 跑新 seed。

**1. build**：`tea api -X POST -r woow-paas/paas-odoo-ci "repos/{owner}/{repo}/tags"
-f tag_name=stg-YYYYMMDD.HHMM -f target=<main SHA>`。`stg-*` tag 的 workflow **預設 clone
addon repo 的 `develop`**（`build-image.yml` 的 `Fetch custom addons`），所以要 build
develop HEAD 就不必傳 `addon_ref`；image name 自動變 `paas-odoo-stg`，tag ＝ 去掉 `stg-`。
進度看 **runner log**（`kubectl -n gitea logs act-runner-runner-0 -c runner`）——tag-push
的 run 常常查不到 API，`Job succeeded` ＋ `<tag>: digest: sha256:…` 才是 ground truth。

**2. upgrade job**：`kubectl -n dev-sandbox-stg get job stg-upgrade-449 -o yaml` 是現成
樣板（env 8 顆 secret 全從 `stg-odoo-dev-sandbox-secret` 取、volumes ＝ configMap
`stg-odoo-dev-sandbox-odoo-config` ＋ PVC `stg-odoo-dev-sandbox-odoo-filestore`、
`backoffLimit: 0`）。
⚠ **Multi-Attach 的解法不是「先縮 deploy」**，而是 `nodeSelector:
kubernetes.io/hostname: <running odoo pod 的 node>`——filestore PVC 是 **RWO**
（longhorn-delete），RWO 在**同一個 node** 上允許多 pod 掛載。跑前先
`kubectl get pod <odoo pod> -o jsonpath='{.spec.nodeName}'` 對齊。實測 39s 完成、零卡頓。
⚠ job 帶 `--logfile=`，**log 不在 stdout**：`kubectl logs` 會是空的，要從 odoo pod
`cat /root/.local/share/Odoo/upgrade-<n>.log`（同一顆 PVC）。

**3. 換 image**：`helm upgrade stg <plugin>/skills/k8s-dev-sandbox/charts/odoo-dev-sandbox
--reuse-values --set odoo.image.tag=<tag>`（values 鍵是 `odoo.image.tag`）。用 helm 而非
`kubectl set image`，否則 helm state 與實際漂移。chart 版本不變時 `--reuse-values` 安全
（新增預設會被丟掉的風險見 [[reference_helm_reuse_values_new_defaults_dropped]]）。

**硬驗版號要三方一致**（只信 tag 會被同名重推騙過）：CI log 的 push digest ＝ pod 的
`.status.containerStatuses[0].imageID` ＝ pod 內 `__manifest__.py` 的 version，再加 DB 的
`ir_module_module.latest_version`。

**UI 驗證**：`/woow` 的 marketplace 路由是 **`#/workspace/<wsId>/services/marketplace`**、
configure 是 `#/workspace/<wsId>/services/configure/<templateId>`（沒有頂層
`#/cloud-services`，打了是 404）。admin 密碼在 secret 的 `odoo-admin-password`。用
agent-browser，勿用 Maestri portal（[[reference_maestri_portal_woow_spa_wedge]]）。

相關：[[project_494_hermes_pricing_landing]]、[[reference_deploy_stg_gotchas]]、
[[reference_stg_db_manual_upgrade_recipe]]

## 換 image 會不會升 addon？（2026-08-11 定案）

**會。** `woow_paas_platform` 是 `web` 的 dependent，`-u web` 經 Odoo 的
reverse-dependency 傳播連帶升它。

| 環節 | 憑據（Odoo 18 原始碼，本機樹 `AREA-odoo/odoo-18.0/`） |
|---|---|
| CLI `-u` 走的就是 `button_upgrade()` | `odoo/modules/loading.py:458-462` |
| `button_upgrade()` 把**依賴被升模組的模組**一併標 `to upgrade` | `odoo/addons/base/models/ir_module.py:706-714`（查 `dependency.name == 當前模組名` → 取 `dep.module_id`） |
| 而且是**遞移**的（`while` 迴圈取閉包，非只一層） | 同上 `:699-712` |
| `woow_paas_platform` 是 `web` 的 dependent | `src/__manifest__.py` 的 `depends` 直接含 `web`；**且**經 `project` / `bus` 兩條間接鏈同樣到 `web` |

直接鏈與間接鏈同時成立 ⇒ 這個結論不是靠單一一行 manifest 撐著的。

**對照實驗（2026-08-11）**：起點 DB `installed | .123`（**無**殘留 `to upgrade` 可被撿）→
換 image 到含 `.124` 的 image → deployment args 仍只有 `["-u","web",...]` → **零人為介入**
（事後 `get jobs` 只有 116/118/119/121）→ 結果 DB `installed | .124`。

⇒ 升級 Job 是**選用**，不是每次部署的必要步驟。真正的驗收在
`.claude/skills/deploy-stg/scripts/verify-addon-version.sh`（#665 post-check gate）：
它驗**終態**（版號 + `state=installed`），因為「傳播被觸發」≠「升級收斂完成」。

<details>
<summary>⚠ 2026-08-03 這裡寫的是相反的結論（保留原文，因為這個坑值得看得見）</summary>

原文：

> ## 2026-08-03 實跑版（.88 → .97，全程成功，含一條會讓舊 recipe 失效的更正）
>
> 🔴 **前提更正**：本檔舊版與 [[reference_deploy_stg_gotchas]] 都寫過「stg entrypoint 自帶 `-u woow_paas_platform`，換 image 就會自動 migrate」。**2026-08-03 實查已不成立**：
>
> ```
> $ kubectl -n dev-sandbox-stg get deploy stg-odoo-dev-sandbox-odoo -o json | ...
>   command: ["odoo"]   args: ["-u","web","-c","/etc/odoo/odoo.conf"]     ← 只有 web
> ```
>
> ~~⇒ **換 image 不會升 addon**。升級 Job 從「加速用的最佳化」變成**必要步驟**，少了它 DB 永遠停在舊版且**沒有任何錯誤訊號**。~~

⚠ **錯在哪一層很重要**：上面那個 `kubectl get deploy` 的觀察（args 只有 `-u web`、不含
`woow_paas_platform`）**本身是對的，至今仍成立**。錯的是從它推出的**因果**——把「沒人顯式
對該模組下 `-u`」等同於「不會升」，漏掉 reverse-dependency 傳播這一環。所以這次修正保留
了觀察、只換掉推論。

📌 **這個說法在本 repo 已被講反三次**（2026-06-02 F-DT-4 原報 → 同日 8 分鐘後自己撤回；
2026-08-03 本檔與 deploy-stg SKILL.md；2026-08-11 早上 issue #665 與 gate 腳本檔頭）。
要再寫「換 image 不會升 addon」之前，請先跑一次上表的原始碼查證——它們指向的是**框架語意**
（Odoo 18 原始碼），不是會過期的環境狀態。

</details>

### 走手動路線時的三階段，順序不可顛倒

（下列順序只在你**選擇**走本檔的手動 Job 時適用；標準 deploy-stg 不需要 Phase 2。）

1. **build image**（`paas-odoo-ci` 建 `stg-<UTC時戳>` tag → image tag = 去掉 `stg-` 前綴）。零前台影響。
2. **升 DB**（Job）——**必須在換 image 之前**。顛倒＝新程式碼跑在舊 schema 上。
3. **換 image**（`helm upgrade` stg release，`--set odoo.image.tag=`）。**Recreate ⇒ 前台不可用 1–2 分鐘**。

⚠ Phase 2→3 之間是「DB 已新、pod 還舊碼」的半升級空窗，**要連著做**，別停下來等人。

### 升級 Job 的做法：clone 既有 Job 改三處，不要手刻 YAML

以上一次的 `stg-upgrade-<舊版>` 為樣板 `kubectl get job -o json`，用 python 清掉 runtime 欄位（`.status`／`.metadata.{uid,resourceVersion,creationTimestamp,generation,managedFields,ownerReferences,labels,annotations}`／`.spec.selector`／`.spec.template.metadata.labels`），**只改**：`metadata.name`、`containers[0].image`、`args` 的 `--logfile=`。手刻會漏 `imagePullSecrets`／env／volumes／securityContext。

Job 內容：`odoo -u woow_paas_platform --stop-after-init -c /etc/odoo/odoo.conf -d woow_stg --logfile=... --log-level=info`；掛既有 filestore PVC（**不建新 PVC**）；`backoffLimit: 0` + `restartPolicy: Never`。歷史耗時 27–39s。

### 三個會咬人的地方

1. **RWO Multi-Attach**：filestore PVC 是 RWO、已被 odoo pod attach。RWO 是 **node-level** 語意 ⇒ 同 node 可共掛。樣板 Job 的 `nodeSelector` 要與**當下** odoo pod 所在 node 一致，不需停 odoo。症狀＝pod 卡 `ContainerCreating` + `FailedAttachVolume`。
2. **ResourceQuota（ns 級）**：quota 只限 limits 不限 requests。2026-08-03 實測 `used 3200m + job 800m = 4000m = hard 4`——**剛好到頂零餘裕**。症狀＝`job Running 但 get pods 查無 pod` + event `FailedCreate`。⚠ **這兩種症狀不同，別誤診**。
3. 🔴 **Job 帶 `--logfile=` ⇒ `kubectl logs` 是空的**。空輸出**不是**通過。log 在 filestore PVC 上，要從 odoo pod 讀同一顆磁碟：`kubectl exec <odoo-pod> -- tail -60 /root/.local/share/Odoo/upgrade-<ver>.log`。pod `phase=Succeeded` 也不是升級成功的證據——**ground truth 是 DB 查詢**。

### 驗收（DB 為準，非 pod label）

```sql
select latest_version, state from ir_module_module where name='woow_paas_platform';   -- 應為新版 | installed
select slug, helm_chart_version from woow_paas_platform_cloud_app_template where slug='<x>';  -- seed 收斂
```

⚠ DB 名是 **`woow_stg`**（同 pg pod 內另有 `odoo`／`sandbox_stg`，查 `-d odoo` 會報錯——那是**查錯 DB**不是「查無此模組」）。

⚠ **不要用 pod 的 `app.kubernetes.io/version` label 驗 chart 版本**——那是 chart 的 appVersion，升級前後可能相同，看不出是否被打回。用 `helm list` 的 CHART 欄。

### 明確不做

`create.sh`／`deploy-stg.sh`（它內部呼叫 `create.sh`，會動 NetworkPolicy／quota／config params／agent-runtime secret，遠超「升級」範圍）／`destroy`／`kubectl set image`（會搶走 helm 的 SSA 擁有權，給下次部署埋衝突）／任何 PVC 操作。

### 附帶：租戶 ns 的既有 release 不受影響

同時段 `paas-ws-stg-*` 內其他 release（open-design／n8n／haos／hermes-agent）REVISION 與 Running 狀態全程不變，ResourceQuota 未被重算。但**手動 helm 升過的 CS chart 版本不會回寫進 Odoo DB 的 `cloud_service.helm_chart_version`**（那欄只在平台自己發起部署時寫）⇒ DB 與叢集分歧永不收斂，症狀是 reset 時 log 印一行看似「chart 要被動」的訊息。詳見 [[project_530_gateway_landing_542_baseline]]。
