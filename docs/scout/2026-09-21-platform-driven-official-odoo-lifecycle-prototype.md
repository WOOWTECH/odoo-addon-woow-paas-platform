---
title: 用平台自己的 UI／API 驅動官方 odoo:18.0 lineage 的 release 走完整生命週期
category: scout
status: draft
created: 2026-09-21
related:
  - docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md
  - docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md
---

# 用平台自己的 UI／API 驅動官方 `odoo:18.0` lineage 的 release 走完整生命週期

> Wayfinder 票 [#1154](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1154)（map [#1134](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1134)）的 prototype 產出。**只量測、不修。**

## 0. 環境

| 項目 | 值 |
|---|---|
| 平台 | dev sandbox `dev-sandbox-odoo1154`（woow-k3s，`--mode stg`），image `paas-odoo-stg:20260906.0543`，addon `18.0.1.0.177`。⚠ 比 develop 舊約兩週；本文行號一律對 `origin/develop` `07cbf100` |
| operator | 叢集上既有的 **stg operator**（`paas-stg-system/paas-operator-stg`，image `166a318`），`NAMESPACE_PREFIX=paas-ws-stg-` |
| 租戶 namespace | `paas-ws-stg-db6cd1ba`（workspace `proto1154`） |
| chart | Bitnami odoo `28.2.10`（沒有新 chart；template 的 chart 座標不動） |
| 候選 image | `jcr-prod.woowtech.io/woow-paas-docker-local/cloud-odoo-proto:1154`（`sha256:c26ff0e9…`），叢集內 kaniko 建置；`FROM odoo:18.0`（實跑 `18.0-20260908`）＋ `/all-addons`（取自 `cloud-odoo:20260814.0737`）＋ pip ＋ chown，無 entrypoint |
| template values | #1136 三個 values 檔合併 ＋ 候選 image（[`assets/1154/helm-default-values.json`](assets/1154/helm-default-values.json)），以 [`assets/1154/switch-template.py`](assets/1154/switch-template.py) 寫進沙盒 DB 的 `cloud_app_odoo` |
| 驅動方式 | 平台 JSON-RPC API（[`assets/1154/drive.sh`](assets/1154/drive.sh)），逐次 request／response 留在 `assets/1154/run-log.jsonl` |

### 0.0 結論的複核

報告定稿前，對 9 條關鍵結論各派一支唯讀 agent 以「試著推翻」的立場逐條核對 develop 的程式碼與行號：6 條 confirmed（就緒誤判、兩條 init 路徑併發、換 plan 無 helm 動作、redeploy 讀服務自己的 values、operator 對同名重裝零防護、回收 CronJob 只看 label）；3 條「機制正確、推論過頭」，已依反證修正本文——addon 整鍵覆寫在今天的 template 下無害（§1.3 適用範圍）、租戶有 rollback 這條出路（§1.3，並補了實測）、reset 15 秒是條件式風險（§2.4）。

### 0.1 量測用的介入（不是修正）

1. 服務 init 失敗轉 `error` 後，步驟 3／4／5 的入口全被擋 ⇒ 在沙盒 DB 把 `state` 撥回 `running`（`init_retries=0`），才量得到下游管線。
2. 步驟 3 把第一顆服務弄壞後，另建第二顆服務（不勾 addon）量步驟 4～8。

### 0.2 建候選 image 時的附帶發現（給 paas-odoo-ci#133）

官方 image 的部分 python 套件是 Debian 裝的（沒有 RECORD）。`pip install --break-system-packages` 需要升版它們時直接失敗：
`Cannot uninstall typing_extensions 4.10.0, RECORD file not found. Hint: The package was installed by debian.`
實測撞到兩顆：`typing_extensions`、`idna`（都由 `openai>=1.0.0` 的相依帶出）。prototype 以 `--ignore-installed` 逐顆蓋過，第 3 輪通過。正式 image 要決定走 venv 或明列 `--ignore-installed` 清單。

## 1. 十個步驟

| # | 步驟 | 結果 | 壞在哪 |
|---|---|---|---|
| 1 | 建立 | ✅ 通過（有 3 個靜默失效） | 見 §1.1 |
| 2 | post-deploy init | ❌ 必壞，10 分 45 秒後轉 `error` | 見 §1.2 |
| 3 | 勾一顆 addon | ❌ **整站掛掉**；取消勾選被擋，rollback 救得回來但下一次 reconfigure 又壞 | 見 §1.3 |
| 4 | `_assess_addon_removal` 預檢 | ❌ 1 秒內 fail-closed，永遠 `can_assess=False` | 見 §1.4 |
| 5 | reconfigure（只改 SMTP） | ⚠ helm 層面乾淨，但設定靜默無效 ＋ 停機約 30 秒 | 見 §1.5 |
| 6 | 換 plan | ❌ **完全沒有 helm 動作**，付了錢拿不到資源 | 見 §1.6 |
| 7 | 停權／復權 | ✅ 通過（冷啟動 62 秒） | 見 §2.4 |
| 8 | 換 image tag 後 redeploy | ⚠ 租戶路徑不可達；後台路徑可行但不升模組、不重算 plan | 見 §1.8 |
| 9 | uninstall | ✅ 通過 | 見 §1.9 |
| 10 | 同名重裝 | 平台路徑下**不可達**；operator API 直打 ⇒ **靜默接回舊資料，且 PVC 帶著 7 天後會被刪的孤兒標記** | 見 §2.3 |

### 1.1 步驟 1：建立

時間軸（UTC）：07:59:12 API create → 07:59:15 namespace 建立 → 07:59:30 helm deployed（rev 1）→ 07:59:41 Cloudflare route 建立 → 08:00:04～08:01:35 bootstrap initContainer（`-i base`，91 秒）→ 08:01:42 主容器啟動 → **08:02:03 pod Ready**（install 到 Ready 共 2 分 33 秒）。

- plan overlay 的**頂層鍵有生效**：`persistence.size=20Gi`、`resources` requests＝limits＝2 CPU／2Gi（plan 11：`cs_vcpu=2`、`cs_ram_gb=2.0`、`cs_storage_gb=20`）。
- ⚠ **workers overlay 靜默跳過**：`--workers=2` 來自 template 的 `args`，不是 plan。新 values 沒有 `customPostInitScripts` ⇒ `src/models/cloud_service.py:4772` 的 `if plan.cs_odoo_workers and scripts:` 為 False（盤點 C015 在執行期成立）。plan 11 剛好也是 2，所以看不出來；要到步驟 6 才會現形。
- ⚠ **租戶填的 admin 帳密沒人消費**：`odooEmail`／`odooPassword` 照樣被送進 release values，但官方 image 不讀。實測 admin 的 login 是 `admin`（不是租戶填的 email），密碼是 values 裡寫死的那組 ⇒ UI 顯示的憑證登不進去（盤點 C014a）。
- ⚠ **平台比 pod Ready 早 100 秒就判定就緒**：08:00:23 就開始呼叫 `init/odoo`，當時 odoo pod 還在跑 bootstrap initContainer。根因在 `src/models/cloud_service.py:2658` `_is_container_ready`：`active_pods` 只取 `phase == 'Running'`，而卡在 initContainer 的 pod 是 `Pending` 被**排除在外**，剩下 postgres 一顆 `1/1` ⇒ `all(...)` 為 True。
  **這推翻了盤點 §3.3「bootstrap 放 initContainer ⇒ Ready 就代表 DB 已 init，`_is_container_ready` 的語意自動成立」的假設。** Bitnami 是在主容器內 bootstrap（phase Running、not ready），所以從未現形。
- `global.imagePullSecrets=[paas-registry]` 有生效，私有 JCR 上的候選 image 拉得下來；兩顆 PVC 都落在 `longhorn-delete`。

### 1.2 步驟 2：post-deploy init

- operator 每次 exec 都是 `/opt/bitnami/odoo/venv/bin/python3`（`extra/paas-operator/src/services/odoo_init.py:178`），官方 image 沒有 ⇒ `OCI runtime exec failed … no such file`。
- 例外被 `_wait_for_ready` 的 `except KubectlException`（`odoo_init.py:202`）當成「還沒 ready」吞掉（只記 `logger.debug`，stg operator 的 log level 看不到）⇒ 空等滿 150 秒後回 `wait_ready: Odoo not ready after 150s`（`odoo_init.py:226`）。operator log 共 8 次。
- 平台端先到：`src/services/paas_operator.py:18` `HELM_OPERATION_TIMEOUT = 120` ⇒ 5 次全部是 `Read timed out. (read timeout=120)`（#1152 實錘）。租戶只看得到 `Odoo initialization failed: Request to PaaS Operator timed out`，**真因（直譯器路徑）在兩端的 log 都不可見**。
- **兩條 init 路徑同時在跑**：controller 輪詢（`src/controllers/paas.py:4200` `_run_post_deploy_init`）與 cron（`src/models/cloud_service.py:3531` `_model_run_post_deploy_init`）各自呼叫 `init_odoo`，operator 端同時有兩個 150 秒迴圈；兩邊共用 `init_retries`，平台 log 出現 3 次 `bad query: UPDATE "woow_paas_platform_cloud_service" SET … "init_retries"`（併發寫入失敗）。08:00:23 第一次呼叫 → 08:11:08 第 5 次失敗轉 `error`，共 10 分 45 秒。
- `error` 之後：reconfigure／勾 addon 回 `Cannot update service in error state`；移除預檢回 `Service must be running to check module states (current: error)`。

### 1.3 步驟 3：勾一顆 addon（`mcp_server`）

08:13:46 API update → 08:14:00 helm rev 2。

| 項目 | rev 1 | rev 2 |
|---|---|---|
| `initContainers` | `bootstrap` | `addon-selector`（**bootstrap 消失**） |
| 主容器 volumeMounts | `/bitnami/odoo`、secrets、**`/var/lib/odoo`** | `/bitnami/odoo`、secrets、`/docker-entrypoint-init.d`、`/opt/bitnami/odoo/selected-addons`（**`/var/lib/odoo` 消失**） |

- 根因：`_build_odoo_addon_helm_values` 對 `initContainers`（`src/models/cloud_service.py:1695`）與 `extraVolumeMounts`（`:1727`）是**整鍵覆寫**，不是 append。
- 後果：`/var/lib/odoo` 變回 image 層 ⇒ 每個 request 都 `PermissionError: [Errno 13] Permission denied: '/var/lib/odoo/sessions'` ⇒ readiness probe 500 ⇒ pod 永遠 `0/1`。`updateStrategy: Recreate` 已先砍掉舊 pod ⇒ **整站中斷**。filestore 留在 PVC 的 `/bitnami/odoo/filestore`，Odoo 看不到。
- **被蓋掉的值已寫回服務自己的 `helm_values`**（DB 實查：stored `initContainers=['addon-selector']`）⇒ template 的 bootstrap 與 filestore 掛載對這顆服務**永久遺失**；`_remove_addon_helm_values`（`:1744`）只按名稱拔除，取消勾選也救不回來。
- **取消勾選這條路走不通**：被拒（`Service must be running to check module states (current: upgrading)`）——移除預檢是 fail-closed，而站壞了就永遠到不了 `running`；`error` 狀態則連 `_update_service` 都進不去（`src/controllers/paas.py:3610`）。
- **平台要 30 分鐘才發現**：pod 一直 `0/1`，08:13:46 送出 → 約 08:44 才被 age timeout 轉 `error`（`Deployment timed out after 30 minutes: pods not ready — pods are Running but never became ready`）。這 30 分鐘站是掛的、UI 顯示 upgrading。
- **rollback 端點救得回來，但只是暫時的**（複核時補測）：`POST /api/workspaces/<ws>/services/<id>/rollback {"revision":1}` 在 `error`／`upgrading` 都放行（`src/controllers/paas.py:3912` 只擋 pending／deleting／suspended），helm rev 3，25 秒 Ready，bootstrap 與 `/var/lib/odoo` 都回來了。但 rollback 全程不碰 DB：`helm_values` 仍是壞的那份、`odoo_addon_ids` 仍勾著 `mcp_server`（UI 顯示已安裝、實際沒部署）。**接著只改一個 `smtpHost`，helm rev 4 又回到 `addon-selector`＋沒有 `/var/lib/odoo`，站再掛一次**。
- 適用範圍：今天的 Bitnami template 在 `helm_default_values` 裡**沒有**用到 `initContainers`／`extraVolumes`／`extraVolumeMounts`，所以這個整鍵覆寫在現行 prod 形狀下無害（也是換選 addon 時的冪等語意）。它是在 template 自己需要這三個鍵的那一刻才變成致命的——而官方 lineage 在 Bitnami chart 上必然需要（bootstrap、`/var/lib/odoo`）。對自建 chart 的要求因此是：這三個鍵由 chart 自己擁有，平台只填 `addons.selected`。
- addon 本身也沒生效：`addon-selector` 有把 `mcp_server` 複製進 emptyDir，但 conf 的 `sed`（`/bitnami/odoo/conf/odoo.conf`）與 `customPostInitScripts['00-addons-path.sh']` 在官方 image 下都是 no-op ⇒ 行程參數沒有 `--addons-path`，Odoo 看不到這顆 addon。

## 2. 四件要量的事

### 2.1 operator exec 路徑在官方 image 內跑不跑得起來

| 檢查 | 結果 |
|---|---|
| container 名 `<release>-odoo`（`odoo_init.py:80`） | ✅ 仍成立（chart 沒換） |
| `/opt/bitnami/odoo/venv/bin/python3` | ❌ 不存在，exec 直接失敗 |
| uid 101 下系統 `python3` 能否 `import odoo` | ✅ `/usr/bin/python3`，`odoo 18.0-20260908` |
| operator 的 ready 探測（xmlrpc `localhost:8069`）改用系統 python3 | ✅ 回 `server_version 18.0-20260908` |
| `parse_config(['-c', '/etc/odoo/odoo.conf'])` 讀不讀得到 DB 設定 | ❌ `db_name=False`、`db_host=False`。官方 image 的 conf 只有 `addons_path`／`data_dir`，DB 參數是 `/entrypoint.sh` 把 env 轉成 CLI 旗標 ⇒ 只改 conf 路徑常數**不夠**，operator 腳本會回 `No db_name found in odoo.conf` |
| `odoo shell` 經 kubectl exec（uid 101） | ⚠ 直接跑 ❌（`local user with ID 101 does not exist`，退回 local socket）；經 `/entrypoint.sh odoo shell -d <db> --no-http` ✅ 2 秒 |
| `/all-addons/catalog.json` | ✅ 存在（3 顆） |

結論：#1129 不是「換三個常數」就結束。要嘛 chart render 一份含 DB 設定的真 conf（建議，Secret 掛載），要嘛 operator 一律經 entrypoint 進去。另：官方 entrypoint 把 DB 密碼放在行程參數上（`--db_password …`），容器內 `/proc` 可見——改用 conf 檔可一併解掉。

### 2.2 三條 create-only 管線的實際行為

| 管線 | 靜態 | 執行期實測 |
|---|---|---|
| `helm_default_values` 只在 create 讀（`src/controllers/paas.py:3353`） | create 當下 merge 定案寫進服務自己的 `helm_values` | ✅ 成立。步驟 8 的 `action_redeploy` 讀的是服務自己的 `helm_values`（`src/models/cloud_service.py:945`）；template 之後怎麼改都不會回流到既有服務 ⇒ **新 chart 的任何 values 修正對既有服務都是 no-op**，只能逐顆改 DB |
| `_apply_plan_spec_overlay` 只在 `src/controllers/paas.py:3419` 呼叫 | 唯一非測試呼叫點 | ✅ 成立，而且比預期更徹底：換 plan（11→13：2G／20G／workers 2 → 4G／30G／workers 3，月費 4500）之後**沒有任何 helm 動作**（operator log 零筆），pod 仍是 2Gi、`--workers=2`、PVC 20Gi；連帳面欄位 `allocated_vcpu／ram_gb／storage_gb` 都沒更新（仍是 2／2.0／20）。之後的 reconfigure、redeploy 也都不重算 ⇒ **目前沒有任何路徑能讓既有服務拿到新 plan 的規格** |
| reconfigure 的 `upgrade_kwargs`（`src/controllers/paas.py:3762`）不送 `sensitive_values` | 六鍵，無 `sensitive_values` | ✅ 成立。operator 收到的是 `helm upgrade … --version 28.2.10 --reset-values --values <tmp>`，rev 1→2 的 values 差異只有 `+smtpHost`。本次 values 裡的密碼是寫在 `helm_values` 內（含明文 `odooPassword`），所以沒掉；一旦密碼改走 `sensitive_values`（#1141 裁決 2／3），`--reset-values` ＋ 不送 sensitive ⇒ 每次 reconfigure 都會把密碼洗掉 |

### 2.3 post-renderer 蓋 `keep` 之後，uninstall → 同名重裝

- **平台路徑下不可達**：release name＝`svc-<uuid4 前 8 碼>`（`src/controllers/paas.py:3335-3345`），每次 create 都是新的；同 workspace 會落回同一個 namespace，但 release 永遠不同名 ⇒ 舊 PVC 只會累積、不會被接回。
- uninstall（步驟 9）：release、Deployment、StatefulSet、Service、Secret、Cloudflare route（含 `-mcp`）全清；兩顆 PVC 留著，都被打上 `paas.woowtech.io/orphaned-at=<epoch>`、`grace-period-days=7`、`release-name`。`keep` annotation 只在 chart 自己 render 的 `svc-*-odoo` 上；postgres 那顆來自 volumeClaimTemplate，本來就不會被 helm 刪。
- **operator API 直打同 namespace＋同 release name**（`POST /api/releases`，無任何防護）：`Install complete`，零警告。
  - 兩顆 PVC 是**同一顆舊的**（建立時間、volume 都沒變）：`svc-*-odoo` 因為還帶著 `meta.helm.sh/release-name` 被 helm 收編；postgres 那顆被新 StatefulSet 依名稱接回。
  - bootstrap 印「DB 已 init，略過 `-i base`」；刪除前寫進 DB 的標記 `proto1154.marker=OLD-DATA-svc-e125ec98` 還在，`database.create_date` 是舊的 ⇒ **舊站原地復活**，新填的 admin 帳密完全不生效。密碼這次對得上純粹是因為 template 把 DB 密碼寫死；改成隨機產生就會變成連不上 DB。
  - 🔴 **孤兒標記沒有被清掉**：重裝後兩顆 PVC 仍帶著 `orphaned-at`。而叢集上的回收 CronJob 是真的存在的（`paas-stg-system/paas-operator-stg-pvc-cleanup`，每日 02:00；prod 同款），它的邏輯只看 label 與到期時間，**不檢查 PVC 是否正被使用、release 是否存在** ⇒ 7 天後會對一個活著的站下 `kubectl delete pvc --wait=false`；pvc-protection 讓它先卡在 Terminating，下一次 pod 重建（任何 reconfigure 都是 Recreate）資料就沒了（`longhorn-delete`）。
- 與 #1136 的差異：#1136 是手動 helm、沒有 post-renderer，觀察到的是 PVC 被刪、重裝時卡 Terminating；經過平台管線則是「留著、靜默接回、帶著定時炸彈」。
- 對下游的意義：任何「還原舊站＝用同名重裝」的 SOP（harumi rollback、#1128 的既存實例處置）都必須先清 `paas.woowtech.io/*` 三個 label；operator 的 install 也該在看到帶孤兒標記的同名 PVC 時 fail-loud。

### 2.4 停權／復權後，bootstrap 在冷 DB 上的耗時

停權（`_do_suspend`，`src/models/cloud_service.py:4056`；沒有租戶 route，由每日 cron 觸發，本次從 odoo shell 直呼同一支 method）：operator 依 `app.kubernetes.io/instance` 一網打盡，`deployment/svc-*-odoo` 與 `statefulset/svc-*-postgresql` **一起** scale 到 0。

復權（`_do_resume`，`:4109`）時間軸：+10 秒 scale API 回來（**平台此時就把 state 寫成 `running`**，pod 一顆都還沒起來）→ +38 秒 postgres Ready → bootstrap initContainer 17 秒（其中 16 秒是 8 次 `pg_isready` 等 DB）→ **+62 秒 odoo Ready**。復權不會觸發 post-deploy init。

| 要校準的預算 | 實測 | 判讀 |
|---|---|---|
| deploy timeout 30 分（`src/models/cloud_service.py:355`） | 首次建立 install→Ready 2 分 33 秒（`-i base` 91 秒）；冷啟動 62 秒；暖重建約 30 秒 | 綽綽有餘；harumi 的 121 模組還原站另計 |
| `init_retries=5` | 5 次＝10 分 45 秒（兩條路徑併發，實際是時間預算不是次數） | 見 §1.2；#1152 的第 3 點成立 |
| reset 路徑 `--atomic --timeout 15s`（`extra/paas-operator/src/api/releases.py:49`） | 這顆 chart 只要 pod template 有變，重建到 Ready 最快約 30 秒（Recreate ＋ initContainer ＋ readiness `initialDelaySeconds: 20`） | **條件式風險，本次未實測**。15 秒的量測基礎是「只改 Secret、不動 pod template ⇒ `--wait` 立即通過」（`extra/paas-operator/src/services/helm.py:27-35`、`releases.py:873-882`）。今天 odoo 不走這條（`has_admin_gui=False`）；#1141 裁決 3 要把 odoo 接上 #357 鏈時，新 chart 必須保證「重設密碼不會改到 pod template」（例如不要有 `checksum/secret` annotation、密碼不進 env），否則 15 秒必逾時並被 `--atomic` rollback |

## 3. 其餘步驟的細節

### 1.4 步驟 4：移除預檢

state 撥回 `running` 後呼叫 `check-addon-removal`：1 秒內回 `Cannot verify module states: [query_modules] Failed to execute module state query: kubectl exec failed: kubectl command failed with code 1`。根因同 §1.2（`extra/paas-operator/src/services/odoo_module.py:170` 的直譯器、`:138` 的 conf 路徑），這條沒有重試迴圈所以快，但訊息一樣沒帶出真因。fail-closed 的設計是對的；後果是**官方 image 下任何 addon 都勾得上、拿不掉**，再疊上 §1.3 就是勾了就壞、壞了拿不掉。

### 1.5 步驟 5：reconfigure 只改 `smtpHost`

helm 層面乾淨（見 §2.2），bootstrap 與掛載都完好，平台 state 回到 `running`、不會再觸發 init。但：
- `updateStrategy: Recreate` ⇒ 改一個欄位就重建 pod，實測停機約 30 秒。
- **設定靜默無效**：chart 把 `smtpHost` 轉成 `ODOO_SMTP_HOST` env，官方 image 不讀 ⇒ 行程參數沒有任何 `--smtp*`、`odoo.tools.config['smtp_server']` 仍是 `localhost`、`ir.mail_server` 0 筆（盤點 C014b 在執行期成立）。UI 顯示成功。

### 1.6 步驟 6：換 plan

見 §2.2。API 回 `success`、訂閱換成 plan 13、月費 4500，其他什麼都沒發生。

### 1.8 步驟 8：換 image tag 後 redeploy

- 租戶路徑：`Unauthorized configuration keys: image.tag`（`src/controllers/paas.py:3663`；`image.tag` 不在 `helm_value_specs` 白名單，create 時同樣被擋）。也沒有租戶可打的 redeploy route。
- 後台路徑（`base.group_system` 直接改 `cloud_service.helm_values` → `action_redeploy`，`src/models/cloud_service.py:887`）：helm rev 3，image 換成 `:1154b`，約 30 秒後 Ready。
  - **不會升模組**：bootstrap 只印「DB 已 init，略過」。Bitnami 每次重啟 `--update=all` 的語意消失（盤點 S2 在執行期成立）⇒ #1141 裁決 1 的升級閘是必要的。
  - state 回到 `deploying` ⇒ 又走一次 post-deploy init ⇒ 又是 10 分鐘後 `error`。
  - plan 13 的規格依然沒套上（2Gi／workers=2）。

### 1.9 步驟 9：uninstall

平台 API `delete` → operator uninstall，20 秒內乾淨（見 §2.3）。小瑕疵：平台對 operator 送了**兩次** `DELETE`，第二次 404（不影響結果）。per-workspace namespace 依設計保留。

## 4. 給下游票的輸入

| 對象 | 輸入 |
|---|---|
| odoo chart 的共用設計（#1141） | ① bootstrap 放 initContainer **不會**讓平台的就緒語意自動成立——`_is_container_ready` 看不到 `Pending` 的 pod（§1.1），要嘛改平台判準（看 workload 的期望 pod 數），要嘛 bootstrap 不放 initContainer。② chart 必須自己擁有 `initContainers`／`extraVolumeMounts`，addon 走單一 values 子樹（`addons.selected`），平台不得再整鍵覆寫（§1.3）。③ conf 要是一份含 DB 設定的真檔（§2.1）。④ reset 路徑的 15 秒預算只在「重設密碼不動 pod template」時成立，chart 要守住這個前提（§2.4）。⑤ plan overlay 要搬進每次 deploy／upgrade 都重算的那一層，並補上「換 plan → 觸發 upgrade」（§2.2）。⑥ 升級閘必要（§1.8） |
| #1129 operator odoo exec | 不是換三個常數：conf 沒有 DB 設定（§2.1）；`_wait_for_ready` 要分辨「exec 本身失敗」與「還沒 ready」（§1.2）；錯誤訊息要帶出 kubectl 的 stderr（§1.4） |
| #1152 init 逾時 | 實錘；另有兩條 init 路徑併發、互搶 `init_retries`（§1.2） |
| #1128 既存實例處置 | 同名重裝會靜默接回舊資料並留下孤兒標記（§2.3）；既存服務的 `helm_values` 不會跟著 template 走（§2.2） |
| #1130 template 切換 | `odooEmail`／`odooPassword`／`smtp*` 全部沒人消費（§1.1、§1.5）；`image.tag` 沒有租戶或正規的後台升版路徑（§1.8） |
| paas-odoo-ci#133 | Debian 套件衝突（§0.2） |
