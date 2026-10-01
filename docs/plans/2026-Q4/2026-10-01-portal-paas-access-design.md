---
title: Portal 會員完整 PaaS 存取修復
category: plans
status: implemented
created: 2026-10-01
updated: 2026-10-01
---

# Portal 會員完整 PaaS 存取修復

Refs [#991](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/991)、#1261、#1216。採 #991 的 B 路徑，正式合併／發布仍待人工覆核。

## 使用者已確認的需求

所有新註冊與既有、已登入的 Portal 外部會員皆可使用 `/woow` PaaS；沒有工作區時顯示建立引導，而非白屏。會員保留 Portal 身分，不取得 ERP 內部使用者或管理員權限。原有工作區角色、方案、付款、配額及功能開關維持有效。

本文件將上述已確認需求轉成修復邊界；具體授權清單須經安全檢視與沙盒測試驗證，不把「頁面能開」當作全功能已完成。

## 已驗證的失敗點

正式 `18.0.1.0.193`：Portal 的 `ir.http.session_info()` 缺少 `user_companies`。`assets_paas` 包含 `web.assets_backend`，其中 company service 在 App 掛載前讀取該欄位而拋出 TypeError。以正式原始碼隔離重現，App 尚未建構便失敗。

同一正式 Portal 身分讀取 `workspace_access` 得到 AccessError；目前自有 model 的租戶 ACL/rules 綁定 internal group，與註冊產生的 Portal 身分不相容。

最新 develop 另有 #1261 的刻意 fail-closed 行為：workspace API、邀請成員、workspace.check_user_access、SmartHome/SecurityAccess/HA helpers 直接拒絕 `user.share`。因此本次不只是補 ACL，還要以使用者現在確認的產品契約取代該「所有外部帳號均拒絕」前提。既有測試不能直接刪除：保留不自動升 internal、非成員拒絕、跨 workspace 拒絕等安全斷言，將「合法 Portal 成員必須拒絕」改為正向功能測試。

## 方案比較與推薦

1. **最小獨立前端 + 受控 Portal API 能力層（安全審查後採用）**：移除 PaaS 不需要的後台 bundle。Portal 保持無直接 PaaS ORM CRUD 權限，由單一 private capability 模組先驗證真實呼叫者、active workspace 成員／角色、資源父層歸屬，再向既有 BFF 業務流程提供窄範圍 recordset。不得將整個 request.env 切換為 sudo；內部使用者繼續走既有 ORM/rules。
2. **複製 internal ACL/rules 給 Portal（初稿曾推薦，安全審查後否決）**：row rules 不能限制所有欄位與 public method，會開放原始部署資料、狀態 write、及可能繞過 BFF 訂閱檢查的操作。隱含群組也無法解決這個問題。
3. **補造後台 session 欄位（不採用）**：改動小，但保留整包後台與第三方 ERP services 的初始化假設；不得偽造 internal 身分。

方案 1 是對一般 ORM/rule 守線的明確 Portal 例外，不是把每個 AccessError 改成 sudo。直接 ORM 入口仍拒絕 Portal；API 能力層必須有對等的 HTTP 功能／隔離測試。既有原生 Portal、ERP 及 HA machine/OAuth 的授權邊界不因此放寬。

## 實作切片

### A. 啟動與故障畫面

使用 Odoo standalone OWL 所需的 core/helpers/bootstrap 依賴，保留 PaaS 自身資源與現有樣式。依現行 Bootstrap 5.3 明確提供 variables-dark／maps，並驗證不是沿用失敗前的舊 CSS。不能連帶載入 website public root 或完整 ERP webclient。保留真實 `is_internal_user=false`。

啟動失敗應有本地、無敏感技術訊息的 fallback 與重試入口。資源載入前有 loading/no-JS 說明。測试必須跑實際 bundle，不只以 stub 補欄位測試。

### B. 外部租戶授權

逐一建立 PaaS 前台使用的 endpoint/action/role/parent-anchor 矩陣。Portal 原始 PaaS ORM 入口保持拒絕，不增加 model ACL、不引入 internal implied group。

capability 在 sudo 前固定呼叫者 uid，只用伺服器取得的 active membership 和資源歸屬授權；不接受 caller-controlled model/domain，不能把未驗證的 id 直接 sudo browse。sudo recordset 的 context 不繼承可偽造的 default_*、owner 或身份參數。所有 write/create 操作仍先經既有 role、subscription、quota 與 lifecycle gate，輸入維持明確欄位白名單。

workspace owner 由伺服器決定；成員角色使用既有 workspace_access 機制。受控 API 可邀請已註冊的 active Portal 使用者，不修改其群組；以 `escape_psql` 保證 Email 是大小寫不敏感的文字比對（不是 SQL 萬用字元），保留同 email 時 internal-first 的既有相容策略。不能新增第二位 owner、不能跨 workspace 操作 membership/route/policy。

一般 DTO 不可多帶 helm_values、token、部署密鑰或 raw model read 資料。已存在且必要的專用憑證取得 endpoint，仍須原有 workspace 管理角色、訂閱、claim 等完整驗證，不能以全域遮罩破壞其合法用途。

ERP 標準 model、訂閱／發票／金流、token／secret／維運模型不取得通用 CRUD。既有 account/partner-scoped billing/profile facade 保持原邊界；HA machine/OAuth 不在本次 /woow 授權擴充內。

### C. 升級與驗證

新舊已登入的外部會員都立即適用，不依賴手動授權或群組回填，不修改 signup template。能力層不需要修改 ACL/rules data；若後續實測發現 schema 或 noupdate 變更必要，另行設計 migration，遵守版號規則。

## 回歸矩陣

- 新註冊與既有 Portal、內部帳號、匿名四類入口。
- 實際 Chromium 完整 bundle 掛載、dashboard/marketplace/profile/billing。
- 無 workspace → 建立自有 workspace → 重新登入仍可見。
- 兩個互不相關會員；workspace owner/admin/user/guest 四角色；已驗證登入但未掛 Portal 群組的非 internal、非 public 外部帳號。
- 正向（自己的受控 API 操作可成功）與負向（他人資源不可枚舉／讀寫）的 HTTP 測試。
- Portal 原始 PaaS ORM RPC 對自己和他人資源都拒絕；內部使用者仍受原有 ORM/rules 約束。
- 禁止 owner/context/role 偽造、直接 ORM 繞過付費或開通流程；被拒絕時 operator 無呼叫。
- Portal 無 ERP/system group，不能修改任意 users/company/config/secret。
- 空狀態、拒絕、逾時與初始化失敗均非白屏。
- 真實 Google 登入另需受授權測試帳號；不將沙盒密碼登入或隔離啟動測試冒稱 Google E2E。

## 發布與非目標

在 `fix/portal-paas-access-20261001` 獨立 worktree 實作。只使用新建、專屬的隔離測試資源，不覆寫其他 sandbox 或 clone 未中和的 prod DB。測試不得呼叫真實付款、寄信或部署租戶資源。

不熱改 prod；PR merge 及 prod build `/approve` 保留人工 gate。未完成主要流程及隔離測試前，不宣稱「全部 Portal 已可商用」。
