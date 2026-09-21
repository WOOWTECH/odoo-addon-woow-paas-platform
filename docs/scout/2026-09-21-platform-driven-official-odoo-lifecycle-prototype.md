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
> ⚠ 草稿：步驟 1～3 與第 1 件要量的事已完成，其餘進行中。

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
| 3 | 勾一顆 addon | ❌ **整站掛掉且租戶無法自救** | 見 §1.3 |
| 4 | `_assess_addon_removal` 預檢 | （進行中） | |
| 5 | reconfigure（只改 SMTP） | （進行中） | |
| 6 | 換 plan | （進行中） | |
| 7 | 停權／復權 | （進行中） | |
| 8 | 換 image tag 後 redeploy | （進行中） | |
| 9 | uninstall | （進行中） | |
| 10 | 同名重裝 | 平台路徑下不可達；改用 operator API 直打（進行中） | `src/controllers/paas.py:3335-3345` |

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
- **租戶無法自救**：取消勾選被拒（`Service must be running to check module states (current: upgrading)`）——移除預檢是 fail-closed，而站壞了就永遠到不了 `running`。
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

### 2.2～2.4

（進行中）
