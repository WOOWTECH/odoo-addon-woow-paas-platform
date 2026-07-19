# Decision: Security Access 裸 Tunnel（零 Route / 零 DNS）+ 建立即自動 Provision

**Date**: 2026-06-11
**Status**: 軸1 accepted + shipped（commit `8633026`，PR #179 merged）；軸2 **accepted**（D1 / D3 / D4 已於 2026-06-11 拍板，見 §Open Decisions）
**Branch**: `fix/remove-extra-application-route`（軸1）→ `feat/sa-bare-tunnel-operator`（軸2 起）
**Components**: `woow_paas_platform.security_access`、`woow_paas_platform.security_access_route`、`action_provision`、`extra/paas-operator`（`schemas.py` / `api/tunnels.py` / `services/cloudflare.py`）、`CreateSecurityAccessModal`、`subscription_lifecycle_cron`、`sh.subscription.plan` SA quota、`src/migrations/`

> 本文件記錄一個**超出已簽核 P2.5 plan 範圍**的架構擴張的取捨與影響面。撰寫動機正是：軸2 不在任何 design doc 的授權範圍內，動工前需要把「為什麼這樣改、改了會動到什麼、剩哪些決策」攤開供團隊 review。

> **⚠️ Decision Log（D3 / D4 + Migration 段）已與 shipped code 分歧**（軸2 後續已全數出貨；現行 manifest `18.0.1.0.40`，`is_primary` 已從 code 與 DB 完全移除）。分歧點：
> - **D4（兩階段 DROP）**：原拍板「`.38` 只改值（raw SQL `is_primary=false`）+ 保留欄位宣告、`.39+` 再 DROP 欄位」。**現況**：`.38` migration（`src/migrations/18.0.1.0.38/post-migrate.py:46-60`）一步到位 DELETE primary route + `DROP COLUMN is_primary`，**無兩階段 deferral**；且 `.39` 是無關的 P-rule policy 遷移，不是原承諾的「`.39+` DROP 欄位」。
> - **D3（quota grandfather）**：原拍板「`.38` 對既有 SA `sa_max_routes` +1 補回 primary 名額」。**現況**：`.38` 明文「No quota grandfather is needed」（`:21-22`）—— 既無既有 route、降級也無對象，故未實作補配。
> - **Migration「方案 A in-place 降級」**：原採用「保留既有 primary route 的 DNS + ingress + policy」。**現況**：實際走 DELETE primary route + DROP COLUMN（近原列為「不採用」的方案 B），因 ship 時 product owner 確認**無任何租戶在用 SA**（`.38` docstring `:8-10`），方案 A 賴以成立的「既有 SA 帶已解析 DNS」前提不成立。
>
> 軸2 行為本體（裸 tunnel provision、`active_no_route` state、移除 `is_primary` guard）皆已落地：`src/models/security_access.py:79`（state Selection 加 `active_no_route`）、`:362-398`（bare tunnel provision）、`:1083`（is_primary guard 已移除）；`_platform_default_hostname` 亦已成 dead code 移除。
> 以下 Decision Log / Migration 文字保留為**當時拍板的歷史紀錄**，不修改；對齊現況請以本 callout + 上述 code 行號為準。

---

## Context

### 觸發問題

建立 Security Access（SA）後，Application tab 會自動出現一條 hostname 形如 `paas-sa-{ws_hash}-{sa_hash}.woowtech.io` 的 application route，且**無法刪除**（截圖回報）。

調查確認：這條是 SA 的 **primary route**，由 `action_provision` 自動建立（`security_access.py:420-433` 首次佈署、`:309-317` retry/self-heal），其 `subdomain_prefix` 固定等於 SA 的不可變身份錨點 `canonical_name`（`src/services/naming.py:29-31` 的 `make_security_access_subdomain` → `paas-sa-{ws_hash}-{sa_hash}`）。「不可刪」由 model 層硬性 guard 強制（`security_access.py:1130-1136`，`is_primary` route 在 `unlink()` 直接 `raise UserError`，僅 `sa_force_delete_primary` context 可繞）；前端只是鏡像隱藏刪除鈕。

### 設計意圖落差（本決策的核心）

P2.5 per-route Access 重構（`docs/plans/2026-Q2/2026-06-09-security-access-p2.5-per-route-access.md`）廢除的是 **user-facing「Primary」概念**：Primary badge（commit `8e33ece` 已移除）+ SA 層 `primary_hostname` 殘留欄位。follow-ups（2026-06-10）起因明載「使用者 review stg 發現 Primary badge / primary_hostname 殘留」。

**但 plan 從未要求廢除「provision 自動建一條 route」這個內部機制** —— 相反地，P2.5 migration（`post-migrate.py:158`）還**主動使用** `is_primary` 當錨點把 legacy policy re-home 過去。換言之：截圖那條 route 不是「plan 漏清的殘留」，而是 plan 刻意保留、承載 tunnel 可達性的內部 anchor。**使用者想拔掉內部 auto-route 機制，比 plan 授權的範圍更激進。**

### 關鍵技術事實（已上網查證）

Cloudflare 建 tunnel **不需要任何 route**：`POST /cfd_tunnel` 只需 `{name, ...}`；ingress（public hostname）是獨立的 `PUT .../configurations` 步驟；一條零 public hostname 的 tunnel 是合法的（對所有請求回 catch-all 404）。本 repo 底層 client 也印證此事 —— `cloudflare.py:742` 的 `create_tunnel(name)` 只送 `{name, tunnel_secret}`，不含任何 ingress；無 config 時的預設值就是 `{"config":{"ingress":[{"service":"http_status:404"}]}}`（`cloudflare.py` 約 :501）。

**結論**：auto primary route 是平台設計選擇，**不是 Cloudflare 的限制**。移除它在 Cloudflare 層站得住腳。

---

## Decision

把「建立 SA 的體驗」與「tunnel 的 route 模型」拆成兩個可獨立執行的軸：

### 軸1 — 建立即自動 Provision（accepted + shipped）

SA 的 create modal 內部自動串接 `create → provision`（完全比照 `CreateSmartHomeModal.js:12,157-181` 的 step machine：create 成功 → `step='provisioning'` → 呼 provision service → 進度條 → `done`），使用者不再手動按 Provision 按鈕。**backend `action_provision` 不動。**

> 釐清：smart home 與 SA backend **都有** `action_provision()` + pending/provisioning/active 狀態機（controller 註解 `security_access.py:398` 明寫 SA mirrors smart_home）。差別只在前端 —— smart home modal 自動串接、SA modal 過去把使用者丟到 detail page 手動按鈕。所以「provision 不需要」指的是**手動那一步**不需要，不是刪掉建 tunnel 的邏輯。

**狀態**：已實作於 commit `8633026`（`CreateSecurityAccessModal.js` / `.xml`，前端 only），並以 docker-desktop dev sandbox + playwright headed 驗證通過（見下方「驗證證據」）。

### 軸2 — 裸 Tunnel / 零 Route / 零 DNS（proposed）

`action_provision` 改成建立**裸 tunnel**：無 hostname、無 application route、ingress 只有 catch-all 404、**零 DNS**。`canonical_name` 只當 tunnel 名稱/identity，**不建 CNAME**。使用者**自己加第一條 route 時才建第一筆 DNS**。**完全移除 auto primary route / `is_primary` 概念**，SA 允許「零 application route」為合法狀態。

---

## 軸2 移除前必須安置的職責（primary route 現在扛什麼）

對抗式覆核後保留的真風險（已剔除 reserved-prefix 豁免、`create_tunnel` hostname 變空、HA API 依賴等查無實據的假風險）：

| # | 職責 | 證據 | 移除後誰接手 |
|---|------|------|------------|
| R1 | tunnel 可達性：至少一條 ingress + 對應 DNS | `_build_route_payload` `security_access.py:566-584` 空 route → `[]`；operator reconcile delete-loop `cloudflare.py:439-487` | **改為「零 route = 零 DNS」by design**：裸 tunnel 不建 DNS，使用者加 route 才建 → 無「孤兒 ingress 對不上 route」問題 |
| R2 | `canonical_name` 的 DNS 具現（provision 時） | `security_access.py:372` `hostname=_platform_default_hostname()` → `create_tunnel`；operator `schemas.py` `hostname` 必填 | **不再於 provision 建 DNS**；`_platform_default_hostname()`（`:159-173`）裸 tunnel 後唯一 caller 消失 → 成 dead code，一併移除 |
| R3 | provision retry/self-heal 冪等錨點 | `:309-317`、`:420-430` 用 `filtered('is_primary')` 判缺失重建 | **改 anchor 到 `tunnel_id`**（`:298` 已是 `if self.tunnel_id:`，與 state 原子寫入無 partial window）。見 [[two-stage-remote-provision-idempotency]] |
| R4 | route 刪除保護 | `unlink()` guard `:1130-1136` | **移除**（所有 route 可刪到零）。注意 SA 自身 `unlink()`（`:559`）用 `sa_force_delete_primary` context 穿透此 guard 做 cascade —— 移除 guard 後此 context 也一併清掉 |
| R5 | quota baseline（primary 免費、其餘計入） | 雙層 `security_access.py:1018,1025` + controller `:524`；help 明文 `sh_subscription_plan_sa_quota.py` | **語義決策（D3）**：移除 `is_primary` 後第一條 user route 開始計入 → 每 plan 有效容量隱性 -1，或既有滿配 SA 超配 |
| R6 | P2.5 migration re-home 錨點 | `post-migrate.py:158`（`filtered('is_primary')[:1] or routes[:1]`） | 已出貨於 `.37`、逐版升級不 re-run。真風險是**跳版 / DROP 欄位順序**（見 Migration 段） |

---

## ⚠️ 地雷拆除確認

軸2 設計把先前查到的**最危險地雷拆除**：

> **舊隱患**：若 SA 變成 zero-route 但仍保留 `canonical_name` 的 DNS，`subscription_lifecycle_cron`（`subscription_lifecycle_cron.py:80/97/109` 每日對每個 subscription-bound SA 跑 `_do_resume`/`_do_suspend`）→ SA `_do_suspend:685` / `_do_resume:721` 都呼叫 `_sync_routes_to_operator` → 零 route 送空清單 → operator declarative reconcile（`cloudflare.py:439-487`）**24h 內刪光 DNS**，不可逆、無錯誤訊息、UI 仍顯示 active。

**在「零 route = 零 DNS」設計下此地雷消失**：裸 tunnel 從沒建 DNS → reconcile 的 `existing_by_sub` 由 `_list_dns_records_by_content` 查本 tunnel DNS → 回 `{}` → delete-loop 真 no-op。已逐行驗證 `reconcile_tunnel_dns` 機制。

**殘留守則（review 時要守住的 invariant）**：no-op 安全性僅成立於「裸路徑全程不 stamp 任何 DNS」。任何 code path 在裸 tunnel 上 out-of-band 建了 DNS，後續空清單 reconcile 就會刪它。

---

## 既有 stg 資料 Migration

既有 SA 都有一條 `is_primary` primary route + 已解析的 DNS + 可能掛 P2.5 policy（policy 綁 `route_id`）。

**方案 A（採用）— in-place 降級 `is_primary=False`**：既有 primary route 變普通可刪 route，**保留其 DNS + ingress + policy 不破壞既有部署**。新 SA 才從零 route 起。

方案 B（不採用）— 刪除既有 primary route：會拆既有 DNS、留 CF 孤兒、disrupt 既有使用者。

**版本與順序風險**：
- 新 migration 為 `18.0.1.0.38`（目前 manifest `18.0.1.0.37`）。
- **DROP `is_primary` 欄位的順序 hazard**：`.37` migration（`post-migrate.py:158`）仍以 ORM `filtered('is_primary')` 引用該欄位。若 `.38` 的 model code 移除欄位宣告，所有 migration 共用同一份已載入的 ORM → 任何經過 `.37` 邊界的升級會在 `.37` 撞 `AttributeError` → 中止整個模組升級。fresh install 不跑 `.37`，install-mode 測試會遮蔽此洞（參 [[seed-noupdate-sticky-template-links]] 的「fresh install 綠 ≠ 升級路徑綠」）。
- **解法（建議 D4(a)）**：把欄位 DROP 與行為改動解耦成兩個 release —— `.38` 只改值（raw SQL `UPDATE ... SET is_primary=false`）+ code 停止讀 `is_primary`；`.39+` 再 DROP 欄位 + 移除宣告 + 同步改 `_order`(`:882`)/`@api.constrains`(`:1002`)/`to_dict`(`:1337`)。
- **quota 反咬**：既有 SA（primary 免費 + N route）降級後變 N+1 全計 → 可能超 `sa_max_routes`。`@api.constrains` 不因 code 改 re-fire（潛伏到下次加 route 才爆）→ 需 D3 一併處理。
- **驗證要求**：必測「先裝 `.37` 後升 `.38`」DB 路徑（非 fresh install），驗 SA state / quota / DNS continuity。

---

## 決策結論（Decision Log — 已於 2026-06-11 拍板）

| ID | 問題 | **結論** | 對實作的影響 |
|----|------|---------|------------|
| **D1** | 零 route SA 的 state 語意 | **(b) 新增 distinct state**（如 `active_no_route`）明確區分「已佈署但無路由」 | PR #3 擴大：SA `state` Selection 加新值；裸 tunnel provision → `active_no_route`；加第一條 route → `active`、刪到零 route → 退回 `active_no_route`；前端 badge + monitoring 健康判斷需認得新狀態 |
| **D3** | quota 計費語義 + 既有訂閱溯及 | **(a) 既有既得權 + 新規則** | PR #4 migration 對既有 SA 把 `sa_max_routes` +1（補回被降級的 primary 名額），新建 SA 起算全 route 計入 → 既有客戶零感知、不超配 |
| **D4** | `is_primary` 欄位 DROP vs 留 dead-marker | **(a) `.38` 只改值、`.39` 再 DROP** | `.38` 用 raw SQL 把 `is_primary` 降為 False + code 停止讀它（保留欄位宣告）；`.39+` 才 DROP 欄位 + 移除宣告 + 改 `_order`/`@api.constrains`/`to_dict`。不可逆動作與行為改動解耦 |

**編號說明**：上表保留 D2 空號 —— D2 是「operator / Odoo 部署順序」，已有明確預設、不需 sign-off，故併入下方技術預設段不列入待決表（保留空號避免與先前評估的編號漂移）。

技術類已有預設無懸念：

- **D2（部署順序）**：operator 必須**先**部署（接受 optional hostname + `routes=[]`），Odoo 才能開始送 `None`/`[]`，否則 rollout window 內 422。
- P2.5 SA-level Access bridge（`_sa_access_route` `:707-711`，F-SA-6 已標待刪）隨 backend 一起移除。

---

## 實作路線圖（Phase / PR）

```
PR #1  軸1 前端串接              LOW・完全可逆・✅ 已 commit 8633026 + headed 驗證通過
                                  ↓（此後 backend 仍建 primary route，尚非零 route）
PR #2  operator 契約 + rebuild   MEDIUM・必先於 backend 部署
        schemas.py hostname/routes 放寬（request + response + routes min_length）、
        api/tunnels.py 裸分支、Odoo client；Gitea CI 自動 build operator image
        ★ no-return #1：image 上線（但 backward-compat，舊 Odoo 仍可跑 → 可回滾）
PR #3  backend 拔 is_primary      HIGH・lock-step 原子改・必在 #2 部署後
        action_provision 裸 tunnel、刪 _platform_default_hostname、unlink guard、
        quota、_order/@api.constrains/to_dict co-change、_sa_access_route bridge
        前端 detail page 刪除鈕 t-if、Application tab empty-state
        （is_primary 欄位「宣告」此階段先保留，為 D4(a)）
PR #4  .38 migration              HIGH・★ no-return #2：降級不可逆・最後 land
        raw SQL 降級 is_primary=False、D3 quota constraint、驗「先裝後升」
```

**決策 gate（明文標出，勿與決策並行 land）**：PR #3（backend 行為改）依賴 **D1** 拍板（zero-route state 語意決定 empty-state UX）；PR #4（migration，point-of-no-return）必須 **D1 + D3 + D4 三案全簽** 才 land。路線圖排序已隱含此 gate，此處明文化避免誤以為實作可與 sign-off 並行。

**拆 PR 原則**：PR #1 已獨立 merge-ready（純前端 UX、零契約風險）。PR #2 backward-compat、先部署。PR #3/#4 因 `is_primary` lock-step 觸點必須原子改，建議合審或嚴格 #4 不早於 #3。operator 改動**必走真 CF E2E**（本 repo 有 mock 綠≠live 綠前例，見 `operator-cf-access-delete-202-mock-contract.md`）。

---

## 驗證證據（PR #1）

docker-desktop dev sandbox（local mode 掛工作區 src/、`woow_paas_platform` + 6 第三方 deps 全 `installed`）+ playwright headed 實跑：

- ✅ form / provisioning / done / error fallback 四個 render 狀態皆正常（done 以 mock provision 成功補驗，DB 確認 0 真實資源）
- ✅ create→provision 端到端串接；provision 失敗（docker-desktop operator 不穩）→ 導向 detail page error state + Provision 重試鈕，無白屏
- ✅ 全程 console 零 OWL/JS error（僅既有 favicon 404）
- ✅ **compile-safety**：OWL 首次 render 編譯整個 template，form step 成功 render 即證明 provisioning/done 分支也編譯通過 → 排除 [[owl-template-crash-class-syntax]] 那類白屏 bug
- ✅ 刪除路徑：SA 移除、tunnel teardown 成功、無孤兒
- ✅ PR#1 範圍邊界：backend 仍建 1 primary route（`routes=1, primary=1`），符合「零 route 屬 PR#3/#4」

---

## Alternatives Considered

1. **只做 Phase 0（廢 user-facing Primary 概念，保留內部 auto route）** —— plan 已授權、零風險，但**不滿足使用者訴求**（截圖那條 route 仍在、仍不可刪）。被本決策的軸2 取代。
2. **保留 auto primary route 但改成可刪/可改 prefix** —— 仍需拆 unlink guard + 處理 declarative DNS reconcile 連動，複雜度與軸2 相當卻保留了一個語義模糊的「特殊 route」概念，不如直接拔除乾淨。
3. **軸1 用後端自動 provision（create() 內呼 provision）取代前端串接** —— create 交易中同步打外部 operator API，超時/rollback 難處理、與 [[two-stage-remote-provision-idempotency]] 冪等模型衝突，且 smart home 也未這樣做。已否決，採前端串接。

---

## References

- 命名：`src/services/naming.py:4-11,29-31`
- provision / primary route：`src/models/security_access.py:266-500`（`:298,309-317,372-381,420-437`）
- 不可刪 guard：`src/models/security_access.py:1130-1136`；SA cascade `:559`
- `_platform_default_hostname`：`src/models/security_access.py:159-173`
- quota：`src/models/security_access.py:1018,1025`、controller `src/controllers/security_access.py:524`
- P2.5 access bridge：`src/controllers/security_access.py:707-711`
- declarative route payload / DNS reconcile：`src/models/security_access.py:566-584`、`extra/paas-operator/src/services/cloudflare.py:439-487`
- operator tunnel 建立契約：`extra/paas-operator/src/models/schemas.py`（`TunnelCreateRequest.hostname` / `TunnelCreateResponse.hostname` / `TunnelRoutesSyncRequest.routes` `min_length`）、`extra/paas-operator/src/api/tunnels.py`、`extra/paas-operator/src/services/cloudflare.py:742`
- cron 生命週期：`src/models/subscription_lifecycle_cron.py:80,97,109`、SA `_do_suspend/_do_resume` `security_access.py:669,699`
- 前端範本：`src/static/src/paas/components/smart-home/CreateSmartHomeModal.js:12,157-181`
- PR #1 實作：commit `8633026`（`CreateSecurityAccessModal.js` / `.xml`）
- 相關 plan：`docs/plans/2026-Q2/2026-06-09-security-access-p2.5-per-route-access.md`、`docs/plans/2026-Q2/2026-06-06-security-access-p1-mainline.md`
- 相關 pattern / pitfall：[[two-stage-remote-provision-idempotency]]、[[seed-noupdate-sticky-template-links]]、[[owl-template-crash-class-syntax]]、`operator-cf-access-delete-202-mock-contract.md`
