---
title: Odoo Service Config Enhancement — Architecture Decisions
category: decisions
status: active
created: 2026-05-19T05:54:44Z
updated: 2026-05-19T05:54:44Z
source: .claude/epics/archived/odoo-service-config-enhancement/epic.md
promoted-at: 2026-05-19T05:54:44Z
related:
  - .claude/prds/odoo-service-config-enhancement.md
  - docs/reference/features/odoo-service-config.md
---

# Odoo Service Config Enhancement — Architecture Decisions

> 此檔由 `/docs:promote ccpm:odoo-service-config-enhancement` 從 ccpm epic 升級而來。原 epic 為 `backlog` 狀態，本決策可作為未來實作參考。

針對 Cloud Service 部署 Odoo 時的 admin 帳密 bug 與 SMTP 設定缺失，這份文件凍結三個架構決策（含 trade-off）。

## D1：SMTP 設定直接使用 Bitnami 原生 Helm Values

**決定**：在 `helm_value_specs` 新增 `smtpHost`/`smtpPort`/`smtpUser`/`smtpPassword`/`smtpProtocol` 五個 top-level 欄位。

**選項**：

| 選項 | 做法 | 結果 |
|------|------|------|
| **A（採用）** | 直接用 Bitnami chart 原生支援的 SMTP top-level values | 零客製、Bitnami 自動處理 Secret 儲存與 outgoing mail server 配置 |
| B | 自己用 initContainer 修改 `odoo.conf` 寫入 SMTP | 與 D2 對稱但多餘，Bitnami 已封裝 |
| C | 用 `extraEnvVars` 透過 ENV 注入 | 仍需自寫 entrypoint hook，比 A 多一層 |

**Why A**：Bitnami Odoo chart 對 SMTP 已有完整封裝，自寫 initContainer 是重複造輪子，且失去 Bitnami 對密碼欄位的 Secret 處理。

## D2：系統層 odoo.conf 透過 initContainer 注入

**決定**：在 `helm_default_values` 配置 initContainer 修改 `/opt/bitnami/odoo/conf/odoo.conf`，注入 `proxy_mode=True` 與 `list_db=False`。這兩個值**不**在 `helm_value_specs` 中，租戶無法覆寫。

**選項**：

| 選項 | 做法 | 結果 |
|------|------|------|
| **A（採用）** | helm_default_values 內 initContainer 寫 odoo.conf，租戶不可覆寫 | 系統強制設定，租戶 UI 看不到 |
| B | 暴露到 `helm_value_specs` 作為「進階設定」 | 違反「系統 vs 租戶」分離原則；`list_db=False` 是安全考量不該開放 |
| C | 用 ConfigMap mount 取代 initContainer | Bitnami chart 不直接支援 odoo.conf ConfigMap，要 patch chart |

**Why A**：系統層設定（`proxy_mode`、`list_db`）涉及網路代理與安全性，必須由平台強制；initContainer 沿用既有 pattern，無需改 chart。

**Invariant**：
- `helm_default_values` 內的 initContainer 設定值，**禁止**也出現在 `helm_value_specs` 內（否則使用者填寫等於覆寫系統值）。
- 任何新增的「系統強制設定」走同一條 initContainer，不在 UI 暴露。

## D3：前端 HelmValueForm 不需變更

**決定**：sanity check 後確認 `HelmValueForm` 組件已完整支援 `select` type（JS: `isSelectField`、`onSelectChange`、`getSelectOptions`；XML: select 渲染模板），SMTP Protocol dropdown 可直接使用。

**選項**：

| 選項 | 做法 | 結果 |
|------|------|------|
| **A（採用）** | 不改前端，假定 bug 不在前端→後端 value 傳遞鏈 | 省一個 Phase；前端 audit 已通過 |
| B | 同步擴充 `HelmValueForm` 確保 select 完整 | 多餘工作 — 既有實作經 review 邏輯正確 |

**Why A**：完整 audit `AppConfigurationPage.js:148/231` + `paas.py:1202/1342` + `releases.py` 證明 value 傳遞鏈正確，bug 根因更可能在 Bitnami chart 行為（如 admin user 機制）而非前端。

**Caveat**：D3 是「假定 bug 不在 FE」，實作 task 6 要先驗證再進 Phase 2。若 task 6 找到根因是前端，D3 需推翻並補前端修法。

## 連動關係

- D1 與 D2 互補：D1 用 Bitnami 原生封裝、D2 走 initContainer 補 Bitnami 沒封裝的部分（odoo.conf 系統層）
- D3 是 D1/D2 的前置假設：前端 value 傳遞正確才能談「在 helm_value_specs 加新欄位即可」

## 後續

- 若 D3 假設被推翻（task 6 確認 bug 在 FE），D1 設計仍成立但實作需多一個 Phase 修 FE
- D2 的 initContainer pattern 可推廣到其他需要系統層 config 注入的 service（n8n template 已有類似做法 — 待整理為通用 pattern doc）
