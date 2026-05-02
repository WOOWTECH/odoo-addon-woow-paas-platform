# PaaS 程式 vs 會議待辦完成度對照表

> **評估範圍**：`develop` branch（最新 commit: `ba1e92c`，version `18.0.1.0.17`）
> **資料來源**：會議記錄 12 場 (2026-03-03 ~ 04-23) + codebase 實作分析
> **產出時間**：2026-04-29（P12 完成更新：2026-04-30；P13 重估移除：2026-05-01；P9 重新驗證：2026-05-01；P9 完成：2026-05-02）

---

## 一、PaaS 開發

| # | 待辦 | 完成度 | 關鍵程式位置 / 說明 |
|---|------|--------|---------------------|
| **P1** | Odoo Service 線上產生問題（環境分流） | 🟢 **100%** | 透過 PaaS Operator 端 `NAMESPACE_PREFIX` 環境變數區分（正式 `paas-ws-`、stg `paas-ws-stg-`）。Odoo 端透過設定不同 PaaS Operator URL 連到對應實例（`src/models/res_config_settings.py:28-33` 有 `woow_paas_namespace_prefix` 欄位輔助）。線上錯誤屬部署/運維層問題，非 addon 程式邏輯缺陷 |
| **P2** | Odoo 部署 chart 統一化 | ⚪ **N/A** | `charts/odoo-dev-sandbox/` 是**開發環境**用 chart（hostPath mount addon 便於即時編輯）；PaaS 發布的 Odoo Service 走另一條路徑（STG/PROD 模式，Bitnami image，addon 包進 image 後從 `/opt/bitnami/odoo/custom-addons/` 載入）。**兩者用途不同，不需統一使用 Bitnami chart** |
| **P3** | Addon 清單機制（Image 內建 + 啟動清單啟用） | 🟢 **100%** | `src/models/cloud_odoo_addon.py:33-177` catalog 模型 + `sync_from_catalog()`；`src/models/cloud_service.py:518-589` `_build_odoo_addon_helm_values()` 經 initContainer 將選中 addon 複製至 `/mnt/selected-addons/` 並改寫 odoo.conf 的 `addons_path`；`charts/.../values.yaml:38` `updateModules` 用 `-u` flag 啟用 |
| **P4** | Add-on Graceful 移除 | 🟢 **100%** | `src/models/res_config_settings.py:70-75` `grace_period_days` 預設 7 天可調；`src/models/subscription_lifecycle_mixin.py` `_is_in_grace_period()` / `_is_past_grace_period()` 邏輯完整；`feat/addon-deselection-safe-removal` 已 merge develop；helm uninstall 流程在 `cloud_service.py:1116-1141` `_do_cleanup()` |
| **P5** | PaaS + Subscription 合併 | 🟢 **100%** | `__manifest__.py:14-17` depends `sh_subscription` + `sh_subscription_wallet`；`src/models/subscription_lifecycle_mixin.py` 共 457 行涵蓋 trial 啟動、skip-trial（`_skip_trial()` L336-417）、to-paid 同步、grace period、cleanup；`cloud_service.py:44` 繼承 mixin |
| **P7** | sh_subscription 整合（含非同步） | 🟢 **100%** | `src/models/sh_subscription_fix.py` 修 auto-invoice bug；`src/models/subscription_lifecycle_cron.py` 用 ir.cron 實現非同步處理（trial 結束、發票結算、grace period 推進、清理）。**未使用 OCA queue_job**（manifest 無相關依賴），純 ir.cron 模式即足以滿足需求 |
| **P8** | Wallet → Billing 分頁 | 🟢 **100%** | 前端 `src/static/src/paas/pages/billing/` 6 個頁面（`BillingOverviewPage`、`InvoiceList`、`TopupStore`、`SubscriptionList`、`TransactionList` 等）；`src/controllers/billing.py` 完整 endpoint；`src/models/wallet_topup.py` + `wallet_transaction.py` model + state machine（draft → pending → done/failed）+ bonus 機制全到位 |
| **P9** | Subscription Plan 存取路徑與權限（新註冊用戶預設權限） | 🟢 **100%** | **設計決策**：本 addon 不重複實作上游 ACL，採用「上游 sh_subscription 提供 base.group_portal ACL + portal record rule（filter `sh_partner_id`）」+「PaaS controller `auth="user"` + `.sudo()`」的雙層保護<br>**驗證證據**（2026-05-02 跑於 dev-sandbox-stg）：<br>✅ 上游 ACL 靜態分析：`sh_subscription_security.xml` 已給 `base.group_portal` plan 全可讀 + subscription `sh_partner_id = user.partner_id` record rule<br>✅ 4 條 unit test (`src/tests/test_acl_p9.py`) 0 failed：portal 只看自己 sub / portal 可讀全 plan / internal 無 role 被擋 / signup template user 只含 base.group_portal<br>✅ Web 煙霧 Smoke 1：admin RPC 驗 `base.template_portal_user_id=5` (portaltemplate, active=False)，groups 全 portal-derived 無 base.group_user<br>✅ Web 煙霧 Smoke 2：`/web/signup` 完整流程註冊新 user → 導向 `/my`，admin 視角驗證 `share=true, hasInternalUser=false, hasPortal=true`<br>✅ 文件：`docs/deployment/admin-roles.md` 說明 PaaS admin 啟用 `sh_subscription.group_manager_sh_subscription` 步驟<br>**設計原則**：portal user **不疊加** sh_subscription functional group（會破壞 portal/internal 邊界）；PaaS admin **手動加入** `group_manager_sh_subscription` 才能後台跨租戶管理 |
| **P10** | 綠界（ECPay）金流串接（綁卡 + 加值） | 🟢 **100%** | `src/controllers/billing.py:47-98` ECPG 綁卡完整；`src/models/payment_token.py:11-79` token charge（含 3DS 挑戰）；`src/models/wallet_topup.py:66-116` topup callback + `_classify_failure` 失敗分類；發票流程拆分至 P12 追蹤 |
| **P12** | Odoo Invoice 與 ECPay Invoice 模組關聯 | 🟢 **100%** | **完整 e-invoice 整合**（commits `ca9e158` ~ `d2317a7`）：<br>✅ `__manifest__.py:18` depends `ecpay_invoice_tw`<br>✅ `account_move_invoice_status.py` 加 `woow_invoice_issue_state` (pending/issuing/issued/failed/error) + retry 追蹤<br>✅ `wallet_topup.py` `_ensure_topup_invoice()` 在 `_on_payment_done` 後自動建 out_invoice + 載具映射<br>✅ `sh_subscription_fix.py:140-147` cycle invoice 帶入 partner 偏好 + `pending` 標記<br>✅ `res_partner_invoice.py` 加 6 欄發票偏好（email/mobile/citizen/triplicate/paper/donation）<br>✅ `ecpay_invoice_cron.py` 異步 cron + 指數退避 (5/30/120 min) + 達 3 次失敗建 mail.activity<br>✅ `account_move_views.xml` 手動「重試開立」按鈕 (groups: account_manager)<br>✅ Profile UI `/woow#/settings/profile` 設定發票偏好（OWL）<br>✅ 27 個 mock unit tests + 2 個 integration tests 全綠<br>✅ stg 真實開立成功（ECPay sandbox 回 `InvoiceNo=GS24029834`） |
| **P13** | Cloud Service 參數設定 + 刪除（正式 3 月 / 測試 7 天） | 🟢 **100%** | ✅ `grace_period_days=7` （測試 7 天）<br>✅ `cleanup_months=6`（產品決策上「能留長先留一下」，刻意調高於會議原訂 3 個月）<br>✅ Odoo cloud service admin password 透過 Bitnami chart 的 `helm_value_specs.required.odooPassword` 由用戶部署時填入，`customPostInitScripts.fix-admin-credentials.sh` 以 pbkdf2_sha512 hash 後寫入 `res_users`（per-instance、不硬寫） |
| **P15** | 服務備份與還原機制 | 🔴 **0%** | **完全未實作**。會議要求每日自動備份，**新增需求補充**：<br>① 不同 service type 各自的 backup/restore 策略（Odoo: PG dump、AnythingLLM/n8n: volume snapshot 等）<br>② 可設定備份頻率（daily/weekly）與保留份數<br>③ Service list view 加入 actions：「Backup Now」、「Delete a Backup」、「Restore from a Backup」<br>④ 現狀 `charts/.../templates/ttl-cronjob.yaml:37-116` 僅 TTL 清理，無備份相關 cron / 腳本 / 儲存 |
| **P16** | Workspace 帳密資訊傳遞給 OpenCode | 🔴 **0%** | 完全未實作。OAuth 模組（`oauth_client.py` / `oauth_token.py`）僅處理 Odoo 內部認證；無 OpenCode 整合、無 secret injection、無 workspace credential 管理機制 |

---

### 跨項目補充

**試用期 7 天**（決議 §Subscription）：🟢 **100%**
- `src/models/subscription_lifecycle_mixin.py:168-184` 強制 7 日 trial（`sh_trial_duration=7, sh_trial_unit=day`）
- `src/data/subscription_products.xml` 全 plan 配置 `sh_trial=True, sh_trial_duration=7`
- `_skip_trial()` API（L336-417）支援自動轉正式訂閱

**Wallet 整體**：🟡 **75%**
- ✅ Model 完整：`woow.wallet.topup`、`wallet.transaction` 擴展、`partner.wallet_balance`
- ✅ Topup state machine（draft → pending → done/failed）
- ✅ `account_move._pay_from_wallet()` 先扣 wallet、不足時觸發 `_attempt_auto_topup()`
- ✅ Bonus 機制（`base_amount` / `bonus_amount` 分解）
- ⚠️ **Auto-topup 限「結算發票時 inline 觸發」**（`sh_subscription_fix.py:380+` `_attempt_auto_topup`、`_try_settle_invoice` L143-168 呼叫）
- ❌ **缺主動觸發 cron**（餘額低於門檻時主動加值）
- ❌ **缺前端設定 UI**（用戶自定「餘額低於 X 時自動加值 Y」）

---

## 二、Odoo 整合

| # | 待辦 | 完成度 | 說明 |
|---|------|--------|------|
| O1 | Odoo K3S 部署 chart 選型 | 🟢 **方向已定** | 確定方向：**有社群版（Bitnami）優先用，沒有才自己開發**。目前 `charts/odoo-dev-sandbox/` 是開發用自製 chart；PaaS 發布走 Bitnami chart |
| O2 | OpenClaw GitHub Repo（K3S branch）部署架構 | ⚪ **保留追蹤** | 需檢視外部 repo，非此 addon 範疇 |
| O8 | Odoo 多 DB 切換機制（一套 Odoo 多家小客戶共享方案） | 🔴 **0%** | 未實作，會議標示「未來規劃」 |
| O9 | Superbase 作為 PaaS Service 的可行性（DB as a Service） | 🔴 **0%** | 未實作，待排 |

---

## 三、其他

| # | 待辦 | 完成度 | 說明 |
|---|------|--------|------|
| X1 | 開新 Branch 開發 AI Data Source 新 Type（Vector Database） | 🟡 部分 | `epic/rag-documents` branch 存在，未 merge develop |
| X2 | 研究 sh_document_management 套件 | ⚪ 研究類 | 非 codebase 工作 |
| X3 | 研究 Embedding 開源方案 | ⚪ 研究類 | 已分享截圖給 Elmo |
| X4 | Per File 的 RAG 功能 | 🟡 部分 | `ai_base_gt` 已 depends；穩定後再推 Per Folder 排程 |
| X5 | ai_base_gt_ext 擴充模組（Skill + Memory） | ⚪ 低優先 | 未動，等 Elmo 測試完現有 AI 套件 |

---

## 整體完成度視覺化

```
🔴 上線前必完成（高優先）
P1  Odoo Service 環境分流   [██████████████] 100% ✓ (paas-operator 端)
P5  PaaS+Subscription 合併  [██████████████] 100% ✓
P7  sh_subscription 整合    [██████████████] 100% ✓
P8  Wallet→Billing          [██████████████] 100% ✓
P10 ECPay 綁卡+加值         [██████████████] 100% ✓
P12 Invoice 串接 ECPay      [██████████████] 100% ✓ 真實開立驗證
P13 Cloud Service 設定+刪除 [██████████████] 100% ✓ cleanup=6 為產品決策

🟡 近期推進
P3  Addon 清單機制           [██████████████] 100% ✓
P4  Graceful 移除            [██████████████] 100% ✓
P15 服務備份/還原             [░░░░░░░░░░░░░░] 0%   ⚠️ 完全沒做

🟢 規劃中
P9  Subscription Plan 權限   [██████████████] 100% ✓ (上游 ACL + 雙層保護驗證完成)
P16 OpenCode 帳密            [░░░░░░░░░░░░░░] 0%

跨項目
試用期 7 天                  [██████████████] 100% ✓
Wallet 整體                  [███████████░░░] 75%  (auto-topup cron 缺)
```

---

## 關鍵風險點

### 🟡 維運能力缺口

1. **P15 備份/還原完全沒做**
   目前無任何 backup cron、無 service-type-specific 策略、list view 無 backup/restore actions。一旦資料事故無補救手段（參考 §六-5 Elmo 已遭遇 2 個 Odoo DB 被清除事件）。

2. **Wallet auto-topup 觸發單一**
   僅在「結算發票時 inline 觸發」，無主動 cron、無前端設定，餘額用盡前才加值的體驗待強化。

### ⚪ 規劃層

3. **P16 OpenCode 帳密整合 0%**
   尚在規劃階段，無立即上線壓力，但未來 OpenCode workspace 整合需設計 secret injection 與資源限制（會議 §六-8 提及 pgvector 等需 ≥ 4G RAM 限制）。

### ✅ 已從風險表移除

- ~~P12 ECPay Invoice 流程零實裝~~ → 已 100% 完成，stg 真實開立驗證通過 (`d2317a7`)
- ~~P13 cleanup_months 錯誤~~ → `cleanup_months=6` 為產品決策（保留期能長則長），不是 bug
- ~~P13 admin_password 未 per-instance~~ → 之前判斷錯誤；Bitnami Odoo chart 經 `helm_value_specs.required.odooPassword` 由用戶部署時填入並 hash 寫入 res_users，per-instance 已正確
- ~~P9 Subscription Plan RPC 直接存取邊界未驗證~~ → 2026-05-02 完成驗證，4 unit tests + 2 web smoke 全綠；上游已 cover portal record rule，PaaS controller 雙層保護有效

---

## 已完成、可宣告 ready 的項目

- ✅ Wallet model + Billing UI（P8）
- ✅ ECPay 綁卡 + token charge（P10）
- ✅ **ECPay 電子發票完整整合（P12）** — Topup + 訂閱、自動 + 手動、retry cron、Profile UI、stg 真實開立驗證
- ✅ 7 天試用機制
- ✅ Subscription lifecycle 整合（P5、P7）
- ✅ Addon 目錄與啟動清單（P3）
- ✅ Add-on Graceful 移除（P4）
- ✅ 環境 namespace 分流（P1，paas-operator 端）
- ✅ Cloud Service 參數設定 + 生命週期（P13，admin password per-instance、grace 7d / cleanup 6m）
- ✅ **Subscription Plan 存取邊界（P9）** — 上游 sh_subscription portal record rule + PaaS controller 雙層保護，4 unit tests + 2 web smoke 驗證