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
         └─ + odoo addon selection   （initContainer + addons_path）
```

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

## reset-config 路徑的 operator 缺口

`_reset_admin_password` 走 reset-config endpoint。實查發現 operator 端
`ReleaseResetConfigRequest`（`extra/paas-operator/src/models/schemas.py`）**未定義
`reset_values` field**，reset-config handler 只把 `reuse_values` 轉給
`helm_service.upgrade()`。而 upgrade endpoint 的 `ReleaseUpgradeRequest` 有完整
`reset_values` plumbing。

因此 reset-config 路徑要走**顯式** `--reset-values`，需 operator 補 1 個 field +
接 1 行（A 案接通，不改任何 helm 邏輯）。**接通前的行為**：client 傳 `reset_values=True`
被 pydantic 忽略，但 `reuse_values=False` 生效 → operator 端 `helm upgrade` 走
**無-flag 預設**（chart `values.yaml` 為 base ≈ reset），已足以終結 reuse bug；
operator 接通後即升級為顯式 `--reset-values`。client payload 兩情況相同，已由
`test_reset_release_config_carries_reset_values` 鎖住契約。

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
  apiServerCidrs）寫進 helm values 的冪等函式。
- **組裝點（統一管線）**：`_assemble_deploy_values()`，install / upgrade / reset 前
  把所有非敏感 values 合併的單一位置，兼作盤點 gate 的真相源。
- **stored-only values**：只存在於 helm stored release values、不在平台 helper 產出的
  值。reset 前的盤點對象（見風險三分類）。
