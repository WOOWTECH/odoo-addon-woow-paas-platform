---
name: provision-e2e-green-on-chart-defaults
description: provision e2e 綠但 release user-supplied values 空——驗證整段跑在 chart 預設上，平台 pin 傳導鏈從未被驗；07-13 HA provision「PASS」即此假綠，直到 chart 0.2.x fail-loud 佔位 tag 才逼現形
metadata:
  type: pitfall
---

# Pitfall: provision e2e 假綠 — 跑在 chart 預設上，而非平台 pin 上

> 來源：2026-07-23 HA PaaS 切 woowtech-ha image + chart 0.2.1 後的 stg 平台全路徑 e2e。
> `POST /api/ha-paas/provision` → 202 → pod `Init:ErrImagePull`，拉的是 chart 佔位 tag
> `woowtech-ha:00000000.0000`。追查發現 **07-13 那次 provision e2e 的「PASS」是假綠**：
> 當時 chart 0.1.0 的預設值（官方 image + tag `2026.7.2`）自足可部署，e2e 綠燈證明的是
> 「chart 預設能開起來」，而非「平台 seed 的 pin 有傳導」。傳導鏈其實從第一天就是斷的。

## Symptom

- provision / deploy 類 e2e 綠燈，服務也真的 Running。
- 但 `helm get values <release> -n <ns>`（user-supplied）只有平台注入值
  （`imagePullSecrets`／`persistence.storageClass`），**沒有 template 的
  `helm_default_values` 內容**（image pin、haProxy、resources…）。
- DB 實查：`cloud_service.helm_values` 為 NULL。

## Root cause

兩層疊加：

1. **建立路徑漏寫**：`src/controllers/ha_paas.py:_create_ha_paas_service` 是 marketplace
   `src/controllers/paas.py:_create_service` 的手工精簡複本，漏掉
   `'helm_values': json.dumps(merged_values)` 這行（docstring 還自述「無 user helm
   values」，把 template 值一併略過）。`_execute_deploy` 從 `{}` 起手。
2. **chart 預設自足遮蔽**：0.1.0 時代 chart 預設恰好完整可部署，seed values 幾乎是
   重複品——傳導斷裂**零症狀**。e2e 驗「開得起來」全綠。

直到 chart 0.2.x 刻意把 `image.tag` 設為 fail-loud 佔位 `00000000.0000`（templates
明確移除 `default .Chart.AppVersion` fallback），斷鏈才第一次可觀測。

## Fix

PR #414（`915ce55f`，隨 18.0.1.0.78 出貨）：create vals 補
`'helm_values': json.dumps(json.loads(tpl.helm_default_values or '{}'))`——
`loads→dumps` 兼作 JSON 驗證，seed 壞 JSON 在 create 當下 fail-loud。

平行建立路徑的結構性收斂（`allocated_*` 同款漏鏡射亦已實證）→ issue #415。

## How to avoid（class 級驗法）

驗「值傳導」的 e2e，**斷言必須釘在只有平台側值才會產生的差異點**，不能拿
「chart 預設也能達成的結果」當證據：

1. e2e 必查 `helm get values <release>`（user-supplied）**非空且含平台獨有值**
   （最強的釘＝image 的 `@sha256:` digest pin——chart 預設永遠不會有）。
2. DB 斷言 `cloud_service.helm_values` 非 NULL 且等於 template 值
   （HA 已機械化：`src/tests/test_ha_paas_provision.py::test_provision_writes_template_helm_values`）。
3. chart 端配合 fail-loud：平台必須覆寫的鍵，chart 預設給**會炸的佔位值**而非
   碰巧能動的 fallback（本案 `00000000.0000` 的設計正是抓到 bug 的功臣）。

## 守線

- HA 路徑：`test_provision_writes_template_helm_values`（隨 .78 出貨）。
- class 級（其他 template／未來新建立路徑）：未機械化——新增 provision 類端點時，
  比照上述三點設計 e2e；結構性解法見 #415（抽共用 `_build_service_vals`）。
