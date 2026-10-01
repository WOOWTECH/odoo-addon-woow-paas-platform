---
title: Portal PaaS 存取修復驗證與發布檢查
category: plans
status: verified
created: 2026-10-01
updated: 2026-10-01
---

# Portal PaaS 存取修復驗證與發布檢查

## 範圍

- 基準：`origin/develop` 的 `17b2816d`；一般修復 PR 不修改 manifest 版號。
- 需求及授權邊界：[設計](2026-10-01-portal-paas-access-design.md)。
- 修復前正式環境：Odoo DB `woow_prod`，模組 `18.0.1.0.193`。
- 只在 `woow-k3s/dev-sandbox-portal-access-20261001` 修改測試資源；沒有修改 prod 使用者、群組、設定、程式或 deployment。

## 隔離

使用者批准建立專屬沙盒。使用全新 `portal_access` DB；沒有 prod DB clone、filestore 或 runtime 憑證注入。應用 DB role 不可建立資料庫／角色，也不是 PostgreSQL superuser。

兩個非 root Pod、無 service-account token、唯讀 root filesystem、丟棄 capabilities。NetworkPolicy 只開放同 namespace 的 Odoo→PostgreSQL TCP5432；沒有 internet／DNS／operator／郵件／金流 egress 或公共 ingress。僅使用父程序的 localhost port-forward。cron threads=0，郵件及付款 provider 關閉，operator 指向 loopback 黑洞。

沿用的 Odoo image：
`sha256:22b158e4868ef307495875fe25edfd21851d18b100587dc929fae1d1e3071a2b`。

沙盒以 bare Pod/emptyDir 建立，刻意不跑會發現真實 operator key 的既有 post-create hook，也不沿用預設允許 internet 的 sandbox chart policy。沒有叢集級角色、PVC 或排程清理權限。

## 修復前證據

1. 正式既有 Portal 會員的 server-side session_info 唯讀探針：缺少 `user_companies`；workspace_access 讀取得到 AccessError。沒有保存或冒用瀏覽器 session。
2. 沙盒原始版本＋真實密碼登入的 Chromium：Portal `/woow` HTTP200，但 body 文字長度0；console 為 `allowed_companies` TypeError。內部帳號陽性對照能顯示。
3. 原始版本＋新增 product journey：**4 failed、0 errors／5 tests**。workspace API 回 `portal_account`，商品目錄 AccessError；ERP 建立權限拒絕仍有效。
4. Workspace raw unlink：權限拒絕前已呼叫子資源 cleanup 的 regression **1 failed→0 failed／1 test**；修法為副作用前 `check_access('unlink')`。
5. 受控 API 初稿的專案／MCP 邊界 regression：修正前21個負向斷言失敗。修復後保留原有分享契約，不將 workspace 成員資格當作 private project 授權。
6. 邀請 Email 的 `%`／`_` 被 `=ilike` 當萬用字元，實測會邀錯帳號：新增兩條測試均先失敗，再以 `escape_psql` 改為大小寫不敏感的文字比對，保留 internal-first 同 email 策略。

## 驗證結果

- 本機：4 項 asset 契約測試、5 項 JavaScript 控制流測試通過；新實作／主要改動檔的 Ruff F、XML/Python 解析、repo-lint、test collection、diff whitespace 檢查通過。`subscription_lifecycle_mixin.py` 與 `test_sh_provision_tenant_rpc_739.py` 的3個 unused-import 警告與 HEAD 逐項相同，未引入新警告，也未為此重構舊檔。
- Odoo API：**169 tests，0 failed、0 errors**；涵蓋33項 Portal／邀請回歸與136項既有內部功能／隔離回歸。最終 log：2026-10-01 05:30:32 UTC。
- Chromium：Portal（繁中）、internal；正式 minified 與 debug-assets；真實 session、不補造 company/internal 欄位；畫面掛載、工作區空狀態、UI 建立、重新載入、另一帳號不可見、320/360px 無水平溢位已實走。
- 故障回復：實際 Owl lifecycle 拒絕後顯示安全文案並可恢復；另在獨立 browser context 故意中止 main JS bundle，伺服器端 fallback 仍可見，實際點擊 retry 後回到 ready，通過。
- 正常流程沒有 page error、console error 或 HTTP error；刻意注入的 `PRIVATE_BOOT_SENTINEL` Owl 錯誤另外記錄，不把預期故障當成正常頁面的零錯誤宣稱。

### 實際執行的逐檔範圍

| 測試檔（`src/tests/`） | 執行方法數 |
|---|---:|
| `test_cs_access_endpoints_745.py` | 22 |
| `test_portal_paas_capability_20261001.py` | 16 |
| `test_portal_paas_workspace_journey.py` | 8 |
| `test_portal_user_workspace_1261.py` | 9 |
| `test_sa_provision_tenant_rpc_805.py` | 12 |
| `test_security_access_endpoints.py` | 46 |
| `test_sh_access_endpoints_768.py` | 19 |
| `test_sh_provision_tenant_rpc_739.py` | 12 |
| `test_workspace_authz_658.py` | 25 |
| **合計** | **169** |

沒有把這個數量冒稱為 #991 提議的「既有 internal fixture 全部批次翻成 Portal」實驗：方案 B 刻意保持 Portal raw ORM 關閉，直接翻轉用 ORM 建 workspace 的 fixture 會停在 setup。此處以 Portal 專用真 HTTP 正負向矩陣驗產品能力，並保留上述 internal 回歸作對照；修復前的逐項失敗見前節與原始 logs。

### 兩個不能用靜態測試取代的發現

- Odoo 啟動後將身分欄位從 `session` 搬到 `@web/core/user`；browser assertions 必須讀 `user.isInternalUser`，不是已被刪除的 `session.is_internal_user`。
- 現行 Bootstrap 5.3 需要 variables→variables-dark→maps→Bootstrap。缺 maps 時，Odoo **回 HTTP200 並沿用舊 CSS**，shell 可見且舊 smoke 仍綠；實際截圖才顯露紅色編譯錯誤提示。已補前置檔案，並要求 browser fetch 真正的 bundle CSS、拒絕 error-fallback 標記與 body/html pseudo 錯誤提示。曾清除沙盒內五筆 PaaS generated assets，確保不是靠舊 CSS 過關。

### 已覆蓋的授權斷言

- 新舊 Portal、可驗證登入但無 Portal 群組的外部帳號；不取得 internal/system/accounting 群組。
- workspace own create/list/update、owner/administrator/member/guest、匿名／inactive／revoked／archived 拒絕。
- CloudService、SmartHome、SecurityAccess 自己資源可讀，別人資源不可見／不可寫。
- route/policy/rule 的 parent anchor、跨資源替換、context-default 偽造、owner 保護。
- 原始 ORM 的 read/create/write/unlink／public mutator 拒絕，拒絕時不得啟動外部操作。
- 真實 order/subscription DB 流程、workspace owner 計費歸屬、取消訂閱後拒絕異動；外部 operator 及排程啟動被 mock。
- 新舊 Portal 可被合法邀請，不變更群組；Email 萬用字元及 underscore 不得誤邀其他帳號。
- private project／專案 MCP 不因 workspace 角色被放寬；HA machine/OAuth 舊授權維持。

## 複跑入口

```bash
python3 tests/test_portal_paas_asset_contract_20261001.py
node --test tests/frontend/test_portal_paas_boot_20261001.mjs
python3 scripts/audit/test_collection_audit.py
python3 scripts/audit/gen-docs-index.py --check
python3.13 scripts/audit/repo-lint.py
```

Odoo 測試只在隔離環境執行，例如：

```bash
odoo -c /etc/odoo/odoo.conf -d portal_access \
  -u woow_paas_platform --test-enable --stop-after-init --max-cron-threads=0 \
  --test-tags /woow_paas_platform:TestPortalPaasWorkspaceJourney,/woow_paas_platform:TestPortalPaasCapability20261001,/woow_paas_platform:TestPortalUserWorkspace1261
```

實際擴大測試還包含 Workspace #658、CS Access #745、SH Access #768、SA endpoints、SA #805、SH #739 共20個 class。完整 selector／log／截圖保留於本次執行環境 `research/woow-portal-blank-20261001/` 的 `run_affected_tests.py`、`affected-tests-green.log`、`browser_probe.py` 與 browser JSON/PNG。

Chromium 在沙盒外經 localhost port-forward 執行，套用已提交 HttpCase 的 browser assertion bodies。正常流程沒有假造 app session、替換 app bundle 或 mock product API；負向測試刻意注入 Owl 拒絕及中止 main JS 請求，驗證 recovery，而非假造成功。沙盒 Pod 沒有 Chromium，因此未把這一段宣稱為原生 Odoo `browser_js` runner 已執行。

## 未驗證／限制

- 未使用正式顧客密碼或真實 Google 帳號完成 OAuth consent/callback；已登入後流程以沙盒真實 Odoo 帳號驗證。Google OAuth 本身未改動。
- 未執行真實付款、寄信、Cloudflare 或 Kubernetes 租戶部署；相關外部副作用隔離／mock。這不是完整金流或基礎設施 E2E。
- 這是受影響回歸矩陣，不是全部 addon 測試或第三方商用安全認證。
- 既有部分 exception handler 仍回傳 `str(e)`；審查未發現本次新增具體洩漏來源，列為後續 hardening，而非宣稱不存在風險。
- SH 跨 workspace 請求改回結構化 `success:false`，不再暴露原始 ORM AccessError；仍拒絕且無 operator／受害資料異動。
- disabled Projects UI、原有 project/AI 分享、HA machine/OAuth 不在本次額外開放範圍。

## 正式發布檢查

1. 人工覆核並合併修復 PR；agent 不代 merge。
2. 依 repo release 流程產生新版號與映像；prod build `/approve` 由授權者親自完成。
3. 必須更新 `woow_paas_platform` 的 QWeb view／語系與 assets；不能只熱換 JS 或重啟同版本而保留舊 `<body/>`。
4. 正式登入的新舊 Portal、internal 各抽測 `/woow`；檢查新 bundle、無 CSS fallback、無 internal 群組升級。
5. 以受授權 Google 測試帳號補正式 OAuth→導回→首次 workspace 流程；不在對話交換密碼／token。

**本次沒有執行 prod rollout、merge 或 build 核可。**
