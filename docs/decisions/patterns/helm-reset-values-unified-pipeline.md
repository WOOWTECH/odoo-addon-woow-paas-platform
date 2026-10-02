---
title: "既存 release 升級走全量 values 統一管線（reuse→reset 語意轉換）"
status: accepted
date: 2026-07-24
deciders: [platform, report-leader-agent-team]
tags: [paas-operator, helm, provisioning, reuse-values, reset-values, contract-test]
supersedes: []
related:
  - docs/decisions/patterns/spawn-install-values-missed-forward.md
  - docs/decisions/patterns/two-stage-remote-provision-idempotency.md
---

# 既存 release 升級走全量 values 統一管線（reuse→reset 語意轉換）

## Context

平台每次部署既存 release（redeploy / reconfigure / reset admin password）都以
`reuse_values=True` 呼叫 operator，operator 直接轉 helm CLI `--reuse-values`。

Helm `--reuse-values` 的語意是：render 時把**新 chart 的 `values.yaml` 換成舊
release 的 coalesced values**（helm 源碼 `chart.Values = oldVals`）。後果是
**chart 升版新增的預設值永遠不生效** —— 既存 release 的平台升級路徑會靜默漏掉
chart 每次改版帶進來的新預設，只能靠人工把值逐台顯式補進 `svc.helm_values`。

這是一個**反覆咬人的 bug class**（issue #411 / #413，代號 Gap 2），已咬或差點咬：

| # | 案例 | 症狀 |
| --- | --- | --- |
| 1 | `console.externalPath`（open-design chart） | 升級舊 release 時 `/console` 靜默不生效 |
| 2 | `networkPolicy.apiServerCidrs`（#404） | redeploy 不重跑 injector，既存 release 需顯式補 |
| 3 | `homeSeed`（#407 / chart 0.1.4 seed-home initContainer） | 差點中招——跨版升級時 initContainer 不出現 |
| 4 | HA chart 0.1.0→0.2.0→0.2.1（2026-07-23 實案） | 升級後 `nodeSelector: {}`（0.1.0 殘影）、`seedResources` 等 0.2.x 新預設全缺席；以 `reuse_values=false` 補 corrective upgrade 才修復 |

模式：**每次 chart 升版帶新預設值，既存 release 的平台升級路徑就會靜默漏掉。**

## Decision

**統一管線（A 案）**：升級既存 release 改走與 fresh install **同一條**全量 values
組裝管線，helm 改 **reset** 語意。

### 組裝管線：單一真相源 helper

`CloudService._assemble_deploy_values()`（`src/models/cloud_service.py`）是唯一的
values 組裝點，回傳 `{'values': ..., 'expose_config': ...}`：

```
values = svc.helm_values            # template 預設 + user config（create 時已 merge）
         ├─ + storage_class          injector（PVC 生命週期）
         ├─ + image_pull_secrets     injector（私有 registry）
         ├─ + allowed_origins        injector（open-design 瀏覽器 Origin 白名單，#380）
         ├─ + apiserver_cidrs        injector（open-design console egress，#404）
         ├─ + omnigent_server_url    injector（code-server／pi-agent／open-design 主容器內的
         │                            omnigent host：同工作區 running Omnigent 的叢集內
         │                            網址，只有網址、不帶 token；runtime-config-only）
         └─ + odoo addon selection   （initContainer + addons_path）
```

`omnigent_server_url` 是唯一**讀別的服務**的 injector：同工作區（且同 namespace）、
`state='running'` 的 Omnigent，多個時取 `create_date`／`id` 最小者並記 log；沒有就
no-op（chart 預設 `""`）。`helm_values` 裡已有非空的 `omnigentHost.serverUrl`（管理員
刻意設的，例如公開 https 網址）時尊重它、不覆寫（記 info log，不記網址）。

**它是執行期設定，不是 pod template 的一部分**——這是它能和其他 injector 一樣在
**每一條**路徑（含 reset 密碼那條 `helm upgrade --atomic --timeout 15s`）重算的前提：
woow-paas-charts `shared/omnigent-host` 套件把 `omnigentHost.serverUrl` 只 render 進
`<name>-runtime` ConfigMap（key `server-url`），以目錄掛載、**不入** checksum
annotation；supervisor 與 `omnigent-host-status` 執行期讀檔。所以 Omnigent 晚於本服務
開通、被刪、或正在 upgrading 時，網址在下一次 deploy／reconfigure／reset 跟著出現或消失，
**pod 不重啟**，不會踩 F-461-2（reset 夾帶 pod template 變動 → 15 秒等不到新 pod →
回退斷線；見 `reset-async-budget-ladder.md`）。chart 側由套件的
`kit_assert_runtime_server_url` 守線；平台側 `test_reset_after_late_omnigent_carries_the_url`
釘住 reset 與 deploy 走同一條組裝。**日後任何 chart 若把這個值放進 env 或 checksum，
就會讓 reset 路徑重新變成 F-461-2**——那是 chart 的 bug，不要在平台這邊繞。

網址只是登入提示與「還沒登入過」的備援：host 實際連的永遠是租戶最後一次
`omnigent login` 的 server（WOOW 預設以 Omnigent 的 admin 帳號登入三台 host）。
連不連得到由各 chart 的 egress 規則決定（租戶 baseline NetworkPolicy
`paas-baseline-allow-same-ns` 只管 Ingress）：code-server 靠
`networkPolicy.sameNamespaceEgress`／`allowAllEgress`、pi-agent 靠 `allowAllEgress`、
open-design 靠 `networkPolicy.omnigentEgress`，而那條規則同時取決於 port 與 pod
selector：只對 Omnigent pod 的 targetPort 8000 開洞，selector 是
`app.kubernetes.io/name=omnigent`＋`component=server`，網址主機是 `<X>-omnigent` 形態時
再釘 `app.kubernetes.io/instance=<X>`。所以 Omnigent 的 `service.targetPort` 不是 8000、
release 名本來就含 `omnigent`（例 `my-omnigent` ⇒ OD 釘 instance=`my`，pod 卻是
`my-omnigent`）、`fullnameOverride` 成別人的 `<X>-omnigent`、或設了 `nameOverride` 時，
OD 都連不到；平台照常注入、只記 warning（`_omnigent_matches_od_egress_selector`），OD
那台只能改用公開網址登入。正式站 release 都是 `svc-xxxxxxxx`、沒有 override，不會觸發。

`audit_411_reset_values.py` 把 `omnigentHost.serverUrl` 與其他整條路徑的 injector 鍵
登記在 `_INJECTOR_VALUE_PATHS`（(a) 自癒類；例如 Omnigent 刪除後 stored release 殘留
舊網址不該被叫去 backfill），`test_audit_411_classifier.TestInjectorPathsLockstep`
以 AST 讀 `cloud_service.py` 的 value-path 表對帳，新增 injector 沒登記會紅。

- **純唯讀、不寫 DB、不呼叫 operator** —— 盤點腳本可安全 dry-run 呼叫，取 `['values']`
  比對 `helm get values <rel>`（stored、非敏感）。這是「盤點 gate 與部署路徑共用同一
  真相源」的關鍵：盤點看到的就是部署會送的。
- `sensitive_values`（admin_password）**不在** helper：它是敏感值、且 stored release
  values 本就不含它，由各呼叫點自組（reset 路徑帶新密碼）。
- `expose_config`（Cloudflare tunnel ingress）由 helper 一併重算，取代原本散在
  create 時 `deploy_params` 與 redeploy fresh 分支的雙寫。**upgrade endpoint 無
  expose 參數**——ingress 首裝建立後升級不重設，故升級路徑忽略此鍵。

### helm reset 語意

三個既存 release 升級呼叫點一律傳 `reset_values=True`（client 端 `reuse_values`
危險預設已由 `True` 翻成 `False`）：

| 呼叫點 | 位置 | operator endpoint |
| --- | --- | --- |
| `action_redeploy()` | `cloud_service.py` | upgrade（PATCH `/api/releases/...`）|
| `_reset_admin_password()` | `cloud_service.py` | reset-config（POST `.../reset-config`）|
| 租戶 reconfigure | `controllers/paas.py` | upgrade（背景執行緒）|

reset 語意下 chart 自身 `values.yaml` 為 render base，helper 組的全量 values 疊上去
—— chart 升版新增的預設自然 coalesce 生效。因為 reset 不再靠 `--reuse-values` 保留
舊值，平台管理的所有值（尤其 4 個 injector）**每次都必須重新組齊**，這正是統一管線
的職責。

> **reconfigure 的 injector 陷阱**：原本 reconfigure 送 operator 的 `values` 只有
> `merged_values`（user config + addon），**完全不含 4 injector**——它一直靠
> `reuse_values=True` 讓 operator 用舊 release values 保留首裝注入的 injector。改
> reset 語意後若不重組全量，injector 會從 rendered manifest 全部消失。故 reconfigure
> 必須在**主執行緒**先呼叫 `_assemble_deploy_values()` 算好塞進 `upgrade_kwargs`
> （背景執行緒無 ORM，不能碰 `service` / `env` / `ir.config_parameter`）。

## 為何折衷案（B）不可行

B 案：redeploy 前把 template 當前 `helm_default_values` 重新 merge 進 `svc.helm_values`
（等同自動化「逐台顯式補」手法）。**否決**，因為：

1. **修不掉 chart 側新增、template 未鏡像的鍵**（homeSeed 型）：新預設只存在 chart
   的 `values.yaml`，template 的 `helm_default_values` 從沒有它，re-merge 補不出來。
   只有 helm reset 語意（chart values.yaml 為 base）能自然補位。
2. **把雙寫負擔制度化**：要求 chart 每加一個預設就同步鏡像進 template seed，是持續的
   drift 來源，違反單一真相源。

## stored-only values 風險分類與盤點 gate

改 reset 前，唯讀腳本（teammate B）比對每 release 的 `helm get values <rel>`（stored）
vs 平台 dry-run 全量 values（呼叫**同一** `_assemble_deploy_values()`，單一真相源），
**同鍵不同值也要抓**（image pin 型殺手）。差異三分類：

| 類 | 意義 | 處置 |
| --- | --- | --- |
| (a) injector 類 | 平台每次重注入，reset 後會自癒 | 無須動作 |
| (b) 平台該管但漏寫 | 應在 helper 產出、但歷史 release 只存在 stored | backfill 進 `svc.helm_values` |
| (c) 無法解釋 out-of-band | 手動 `helm upgrade`、其他系統寫入等 | 逐筆人工裁決（Eugene 拍板）|

**prod 翻 flag 前 (c) 必須清零。** 機械比對時路徑用 tuple 而非 dot-join
（`kubernetes.io/arch` 這種含點鍵名 dot-join 會假陽性）。

## reset-config 路徑的 operator 缺口（#449 已接通）

`_reset_admin_password` 走 reset-config endpoint。原本 operator 端
`ReleaseResetConfigRequest`（`extra/paas-operator/src/models/schemas.py`）**未定義
`reset_values` field**，reset-config handler 只把 `reuse_values` 轉給
`helm_service.upgrade()`；而 upgrade endpoint 的 `ReleaseUpgradeRequest` 早有完整
`reset_values` plumbing。

**#449 已補上該 field 並接進 handler**（A 案接通，不改任何 helm 邏輯），reset-config
現在走**顯式** `--reset-values`。**接通前的行為**（仍是舊 operator 部署的實況）：
client 傳 `reset_values=True` 被 pydantic 忽略，但 `reuse_values=False` 生效 →
operator 端 `helm upgrade` 走**無-flag 預設**（chart `values.yaml` 為 base ≈ reset），
已足以終結 reuse bug。helm 3 的無-flag 預設與 `--reset-values` 語意相同，故接通對既有
呼叫端是行為中性的向後相容改動。client payload 兩情況相同，已由
`test_reset_release_config_carries_reset_values` 鎖住契約。

## 密碼載體分流（#449）：全量 values 管線的另一半

同一條 reset 路徑另有一個與 values 無關、但同樣「對 mcp-server 成立、對別的 chart
不成立」的寫死假設：operator 直接 `kubectl exec` 進 pod patch `/data/config.json`。
hermes-agent（PVC 掛 `/opt/data`）與 open-design（`/app/.od`）根本沒有那個檔，密碼由
chart 自己的 Secret 承載 → patch 炸 → 備份未建成 → 補償 `os.replace` 也炸 → 每次 reset
必 500（fail-safe 有效不鎖死，但唯一憑證換不掉）。

拍板「1+3 合體」：**平台 template 帶策略、operator 依策略分流**（chart annotation 契約
因需動 woow-paas-charts 全部 chart 並逐一升版而不採）。

| 載體 | 誰 | operator 行為 |
| --- | --- | --- |
| `config_json`（預設） | mcp-server 家族六筆 template | 既有 #357 補償 saga（helm upgrade → exec patch → rollout → 刪備份）|
| `helm_secret` | hermes-agent、open-design | helm upgrade（atomic、全量 values＋sensitive_values）→ **全部**同 instance label 的 Deployment rollout restart → 等 ready；不碰 config.json |

三個必記的實作前提（都在 chart 側查證過，不是推論）：

1. **供給面兩者相同**：`sensitive_values` 一律是 `config.sensitive.admin_password`
   （`_build_mcp_sensitive_values`），兩個 chart 的 `secret.yaml` 都吃這個鍵。載體只
   影響 reset，不影響建立服務時的注入。
2. **helm upgrade 之後一定要 rollout restart**：兩個 chart 的 Deployment 只有
   `checksum/config`／`checksum/authproxy-config`，**沒有 `checksum/secret`**。改
   Secret 不改 pod template ⇒ helm 不滾 pod，而密碼是啟動期消費的（nginx htpasswd 由
   initContainer 從 Secret 算、webui/dashboard 走 env）。少了 restart 會「upgrade 成功
   但密碼靜默不生效」——比 500 更糟。
3. **必須是複數 Deployment**：兩個 chart 都渲染 daemon + console 兩顆 Deployment 共用
   同一個 `app.kubernetes.io/instance`，單數版 `get_deployment_name` 撞多筆會 raise
   `Ambiguous selector` → 500。故新增 `get_deployment_names`（複數），單數版維持
   fail-loud 契約不放寬。

**發版順序**：`admin_password_carrier` 在 operator 端預設 `config_json`（＝既有行為），
舊平台不帶本欄即照舊 → **operator 先發版、平台後發版**永遠安全。反向（新平台 + 舊
operator）不會更糟：舊 operator 忽略該欄、hermes-agent reset 仍 500，與修復前同。

**選錯載體的後果不對稱**：`helm_secret` 誤標 `config_json` ＝ reset 必 500（大聲）；
`config_json` 誤標 `helm_secret` ＝ helm upgrade 成功但 config.json 沒動、密碼靜默不
生效（假裝成功）。新 template 上架的判準是「**chart 有沒有把 Secret 落成 PVC 上的設定
檔**」，不要照 slug 或 chart 名猜。守線＝`TestAdminPasswordCarrierSeed` 的
chart↔載體結構不變式（mcp-server chart ⟺ config_json）。

## Consequences

- chart 升版新增預設，既存 release 升級路徑會自然補位，不再需要逐台人工補。
- 4 injector 的重注入責任從「隱式靠 reuse 保留」變成「每次顯式重組」，由統一 helper 保證。
- 新增 injector / 上游欄位時只改 helper 一處，三呼叫點與盤點腳本同步受惠。
- 平台 code 新增 `reuse_values=True` 由 `scripts/audit/repo-lint.py` gate 攔（見下）。

## 術語

- **reuse_values（`--reuse-values`）**：helm 用**舊 release 的 coalesced values** 當
  render base，chart 新預設不生效。本次要根除的危險路徑。
- **reset_values（`--reset-values`）**：helm 用**新 chart 的 `values.yaml`** 當 base，
  傳入的 values 疊上去。本次改用的語意。
- **無-flag 預設**：helm upgrade 不帶 `--reset-values` / `--reuse-values` 時的行為
  ——用 chart values.yaml + 這次傳入的 override，不保留舊 release override（≈ reset）。
- **injector**：把平台側值（storageClass / imagePullSecrets / allowedOrigins /
  apiServerCidrs / omnigentHost.serverUrl）寫進 helm values 的冪等函式。
- **組裝點（統一管線）**：`_assemble_deploy_values()`，install / upgrade / reset 前
  把所有非敏感 values 合併的單一位置，兼作盤點 gate 的真相源。
- **stored-only values**：只存在於 helm stored release values、不在平台 helper 產出的
  值。reset 前的盤點對象（見風險三分類）。
