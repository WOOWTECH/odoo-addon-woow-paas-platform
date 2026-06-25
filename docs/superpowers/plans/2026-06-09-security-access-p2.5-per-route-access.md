# Security Access P2.5 — Per-route Access Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** 把 P2 已 ship 的 **Service-wide** Access 模型（整個 SA 一個 Cloudflare Access app + 一套共用 policies/login/session）重構成 **Per-route** 模型：每條 Application Route = 一個 Cloudflare Access application，各自有 Protected 開關 + 自己的 policies + 自己的 login methods + 自己的 session duration。對齊使用者 canonical 設計與 mockup（「每個 Protected Application 各別設定」）。

**Architecture:** access 三件組（policies / login / session）+ Access app id 從 SA 層**下放到 route 子模型**。`route.is_protected` 成為每條 route 的 Access on/off（打開=為這條 route 建/維護它自己的 CF Access app，關閉=拆掉）。operator **不改**（已是 per-app；Odoo 改成「每條 protected route 各打一次 `sync_access_app`」，每個 app 的 domain/destinations 只放該 route 自己的 hostname）。

**Tech Stack:** Odoo 18（model/controller/OWL）、既有 FastAPI operator（`PUT /api/access/sync`，per-app，不動）。

---

## 已拍板的決策（2026-06-09，寫 code 前固化，避免 plan-gap）

1. **只做 Per-route，廢 Service-wide**：不做雙模版、不做切換器。單一拓樸。既有 P2 Service-wide 資料走**單向 migration** 搬到 per-route。
2. **login methods + session duration 全下放 per-route**：每條 route 各自選 Google/Email OTP + session；CF 每個 per-route app 各算自己的 `allowed_idps`。
3. **先做本 P2.5，P3 Domains 暫緩**（P3 plan 已寫好存著：`2026-06-08-security-access-p3-domains-cname.md`）。

## 向後相容 / migration 風險評估（關鍵：風險比想像低）

- **prod 無 P2 Access 資料**：prod operator 仍是 pre-P2 image（`20260525.0054`），prod 上**沒有任何 live SA Access 設定** → prod 無遷移風險。
- **stg 有 runtime 建的 test SA**（非 XML seed；`data/` 只有 template catalog，無 SA instance）→ migration 要處理的是 **DB runtime row**，量小、可控、可手動補。
- **CF 物件**：既有 SA 層單一 app（`SA.access_app_id`）涵蓋多個 destinations。per-route 後要拆成 N 個 app。migration **只搬 DB**（不直接打 operator）；既有舊 app 由 **一次性 stg 手動 teardown + per-route re-sync 重建**處理（見 Phase 5）。prod 不受影響。

## Model 重構對照（understand 盤點，行號為現況）

| 維度 | 現況（Service-wide） | 目標（Per-route） |
| --- | --- | --- |
| access_policy 綁定 | `security_access_id`（security_access_access_policy.py:22） | **改綁 `route_id`**（M2O → security_access_route, required, ondelete cascade）；移除 security_access_id |
| access on/off | SA `access_enabled`（security_access.py:134） | route `is_protected`（security_access.py:1026，已存在）= 唯一開關；**移除 SA access_enabled** |
| login methods | SA `login_google`/`login_email_otp`（:148-149） | **搬到 route 子模型** |
| session | SA `session_duration`（:139） | **搬到 route 子模型** |
| CF app id 回填 | SA `access_app_id`（:150） | **搬到 route `access_app_id`** |
| fail-open IdP 訊號 | SA `access_unresolved_idp_types`（:159） | **搬到 route `access_unresolved_idp_types`** |
| policies O2M | SA `access_policy_ids`（:154） | **搬到 route `access_policy_ids`**（route_id inverse） |
| payload 組裝 | SA `_build_access_payload` 聚合所有 protected route 成單一 app（:668） | route 層 method，每 protected route 各組一個 app payload |
| sync | SA `_sync_access_to_operator` 一次 call（:其後） | route 層 method，每 protected route 各 call 一次 |
| operator | `PUT /api/access/sync` per-app（不改） | **不改**；Odoo per-route 各打一次 |

---

## Phase 1 — Model 重構（route access 三件組 + 廢 SA 層 + migration）

**Files:** Modify `src/models/security_access.py`（route 子模型加欄位 + SA 移除 access 欄位 + payload/sync 改 route 層）、`src/models/security_access_access_policy.py`（route_id）；Create `src/migrations/{NEW_VERSION}/post-migrate.py`；Modify `src/tests/test_security_access_access_policy.py` + `test_security_access.py`。

> NEW_VERSION = 當前 manifest version +1（寫 plan 時 develop 為 18.0.1.0.36 → 用 `18.0.1.0.37`；執行時以實際 manifest 為準，bump 到下一個）。

### Task 1.1 access_policy 改綁 route_id（TDD）
- [ ] **Test**：`test_policy_requires_route` — create access_policy 帶 `route_id` 成功；`policy.security_access_id` 可由 `route_id.security_access_id` 推導（related, readonly）或移除。
- [ ] **Implement** `security_access_access_policy.py`：
  - `route_id = fields.Many2one('woow_paas_platform.security_access_route', required=True, ondelete='cascade', index=True)`
  - `security_access_id = fields.Many2one(related='route_id.security_access_id', store=True, index=True)`（保留為 related，ir.rule/quota 仍可 walk，但真實綁定是 route）
  - quota `_check_policy_quota`：改成 per-SA 總量仍用 `security_access_id`（related）計數（plan sa_max_access_policies 是 SA 級配額，跨 route 加總）— 維持配額語義。
  - create/write/unlink sync trigger：改成 sync `self.route_id`（呼 `route._sync_access_to_operator()`），不再 sync 整個 SA。
  - to_dict 加 `route_id`。
- [ ] **Run** → PASS。Commit `feat: bind access policy to route (per-route)`。

### Task 1.2 route 子模型加 access 三件組（TDD）
- [ ] **Test**：route 有 login_google/login_email_otp/session_duration/access_app_id/access_unresolved_idp_types/access_policy_ids 欄位；is_protected 預設 False。
- [ ] **Implement** route 子模型（security_access.py:975+）加：
  - `login_google = fields.Boolean(default=False)`、`login_email_otp = fields.Boolean(default=False)`
  - `session_duration = fields.Selection([...30m/1h/8h/24h/168h...], default='24h')`（複用 SA 原 selection）
  - `access_app_id = fields.Char(copy=False)`、`access_unresolved_idp_types = fields.Char()`
  - `access_policy_ids = fields.One2many('...access_policy', 'route_id')`
  - route `to_dict()` 加 is_protected（已有）+ login_google/login_email_otp/session_duration/access_app_id/access_unresolved_idp_types/access_policies(=[p.to_dict()])。
- [ ] **Run** → PASS。Commit `feat: add per-route access fields (login/session/policies/app id)`。

### Task 1.3 per-route _build/_sync（TDD）
- [ ] **Test**：`route._build_access_payload()` → primary_hostname=protected_hostnames=該 route hostname、policies=route.access_policy_ids、login=route.login_*、session=route.session_duration、existing_app_id=route.access_app_id。`route._sync_access_to_operator()`：is_protected=True → sync 回填 route.access_app_id + policy cf ids；is_protected=False → teardown（existing_app_id + existing_policy_ids）+ 清空 route.access_app_id。mock 雙 namespace。
- [ ] **Implement**：
  - 把 SA `_build_access_payload`/`_sync_access_to_operator`/`_build_existing_policy_ids` **搬到 route 子模型**，改成單 route 範圍。liveness gate 改 `route.security_access_id.state in ('active','error') and route.security_access_id.tunnel_id`。
  - route create/write/unlink trigger：`is_protected`/`login_*`/`session_duration` 變動 → sync 該 route（savepoint 包，鏡像 P2）。`access_app_id` 寫回用 context escape（避免不可變 guard，沿用 P2 pattern）。
  - orphaned_policy_ids → log warning（per-route，鏡像 P2）。
- [ ] **Run** → PASS。Commit `feat: per-route access sync to operator`。

### Task 1.4 移除 SA 層 Service-wide access（TDD）
- [ ] **Test**：`test_no_sa_level_access_fields` — SA `_fields` 不含 access_enabled/login_google/login_email_otp/session_duration/access_app_id/access_unresolved_idp_types/access_policy_ids（鏡像 primary_hostname 移除測試）。
- [ ] **Implement**：移除 SA 層那 7 個欄位 + SA 的 access write() trigger（security_access.py:304-305 gated 區）+ SA.to_dict 的 access 區。`_cleanup_cloudflare` 的 Access teardown 改成 per-route（loop is_protected routes 各 teardown）。
- [ ] **Run** → PASS。Commit `refactor: drop SA-level service-wide access fields`。

### Task 1.5 migration（既有 SA 層 access → per-route）
- [ ] **Implement** `src/migrations/{NEW_VERSION}/post-migrate.py`：
  - 對每個既有 SA（DB row）：把 SA 層 login_google/login_email_otp/session_duration **複製到該 SA 的每個 is_protected route**；把既有 access_policy（security_access_id=該 SA）的 `route_id` 設為**該 SA 的 primary route**（is_primary），若該 SA 有多個 protected route 則**複製** policies 到每個 protected route（行為等價：原共用 → 每 route 各一份）。
  - 清空 SA.access_app_id（舊單一 app id；per-route re-sync 會建新的）。記錄舊 app id 到 log（供 stg 手動 teardown 對照）。
  - **冪等**：migration 可重入（先查 route_id 已設則 skip）。
  - 註解標明：prod 無 P2 Access 資料（此 migration 對 prod 為 no-op）；stg test SA 由此搬遷 + Phase 5 手動 teardown 舊 CF app。
- [ ] **Test**：`test_migration_moves_sa_access_to_routes`（odoo-shell 風格或 TransactionCase 模擬既有 SA-層 policy → migrate → 斷言 route_id 已設、login/session 複製）。
- [ ] Commit `feat: migrate service-wide access to per-route (post-migrate)`。

**Phase 1 mini-review**：migration 冪等 + 既有資料不失保護、per-route sync liveness gate、savepoint、mock 雙 namespace、quota 語義（SA 級總量 via related）、su-env guard（access_app_id 寫回）。

---

## Phase 2 — ACL（access_policy ir.rule walk 改 route）

**Files:** Modify `src/security/ir_rules.xml`（access_policy 3 條 rule）；Modify `src/tests/test_security_access_access_policy.py`（TestAccessPolicyAcl）。

### Task 2.1 ir.rule walk
- [ ] **Test**：member 可讀 own-workspace route 的 policy；非 owner 不可改；跨 workspace deny（鏡像現有 TestAccessPolicyAcl，但 domain 經 route walk）。
- [ ] **Implement**：access_policy 3 條 rule 的 domain 從 `security_access_id.workspace_id.access_ids...` 改成 `route_id.security_access_id.workspace_id.access_ids...`（或因 security_access_id 為 store=True related，可維持原 walk — 驗證 related store 的 ir.rule domain 可用；若可用則**不改 xml**，僅加測試確認）。
- [ ] **Run** → PASS。Commit `feat: access policy ACL walks via route (or confirm related-store walk)`。

**Phase 2 mini-review**：two-layer-acl-lockstep（ir.rule + controller per-route carve-out 同步）、related-store domain 正確性。

---

## Phase 3 — BFF（per-route policy CRUD + per-route access config）

**Files:** Modify `src/controllers/security_access.py`；Modify `src/tests/test_security_access_endpoints.py` + `test_security_access_subscription_flow.py`。

### Task 3.1 policy CRUD 帶 route_id
- [ ] **Test**：`_create_policy` 白名單 — client 可寫 name/decision/rule_type/rule_value/sequence + **route_id**（pin 到 gated SA 底下的 route，跨 SA 的 route_id 要 deny）；cf_policy_id/security_access_id 不可客戶寫；quota（SA 級總量）。
- [ ] **Implement** `api_sa_access_policies` + `_create_policy`/`_update_policy`/`_delete_policy`：帶 route_id，驗證 route 屬於 gated SA（`route_id in sa.route_ids`）；白名單嚴格。controller 不 `.sudo()` 繞 ir.rule。
- [ ] **Run** → PASS。Commit `feat: per-route access policy BFF`。

### Task 3.2 per-route access config endpoint
- [ ] **Test**：`_update_route_access`（route 的 is_protected/login_google/login_email_otp/session_duration）白名單；server-owned（access_app_id/access_unresolved_idp_types）不可客戶寫；HttpCase role=user member negative。
- [ ] **Implement**：新增/改 endpoint 設 route 層 access config（取代 P2 的 SA 層 api_sa_access_config）；移除 SA 層 access config endpoint。
- [ ] **Run** → PASS。Commit `feat: per-route access config BFF + endpoint negatives`。

**Phase 3 mini-review**：white-list（route_id 跨 SA pin、cf 欄位不可寫）、ORM 綠 ≠ endpoint secure（HttpCase 對等）、no sudo bypass。

---

## Phase 4 — 前端（Per-route Protected Applications）

**Files:** Modify `src/static/src/paas/services/security_access.js`、`pages/security-access/SecurityAccessDetailPage.{js,xml,scss}`、`components/modal/AddPolicyModal.{js,xml}`（policy 帶 route）。

### Task 4.1 Application tab
- [ ] **Implement**：移除 Protected 欄（th + checkbox td + toggleRouteProtected + hint）。表格只剩 Hostname / Service / Host header / 刪除。（Primary badge 已於 8e33ece 移除。）
- [ ] Commit `feat: drop Protected column from Application tab`。

### Task 4.2 Access tab → per-route Protected Applications
- [ ] **Implement** 對齊 mockup：
  - 移除 SA 層 master toggle / SA 層 Login Methods / SA 層 Policies / SA 層 session（Service-wide UI 全移）。
  - 「Protected Applications」列表：每條 route 一個 card —— hostname + **Protected toggle**（route.is_protected，呼 per-route config）+ 展開（is_protected=True 時）顯示該 route 自己的：Login Methods（Google/Email OTP toggle）、Access Policies（list + Add Policy，policy 帶 route_id）、Session Duration select。未 protected 的 route 顯示「Public · anyone can reach it」。
  - 頂部說明卡（取代切換器）：「Each Application Route is its own Access application with its own policies and login methods.」（**無** Per-route/Service-wide 切換器，只 Per-route）。
  - fail-open IdP 警示改 per-route（讀 route.access_unresolved_idp_types）。
  - **OWL 陷阱自檢**（[[owl-template-crash-class-syntax]]）：component 不用 t-att-、按鍵 handler 讀 ev.key。`.o_woow_sa_*` 前綴。
- [ ] **service.js**：方法改 per-route（updateRouteAccess(routeId,...)、createPolicy 帶 routeId…）。
- [ ] **Headed 驗證**（playwright-cli/agent-browser）：Access tab per-route render、toggle、展開設定、Add Policy、console 零錯誤。
- [ ] Commit `feat: per-route Protected Applications UI`。

**Phase 4 acceptance**：headed render 無 console error；OWL 陷阱 grep 乾淨。

---

## Phase 5 — 回歸 + 對抗式審查 + stg 遷移驗證 + 收尾

- [ ] 全模組回歸（docker-desktop sandbox `test.sh feat-security-access --module woow_paas_platform`，注意：local 模式只能 docker-desktop，woow-k3s hostPath 無效）0 failed/0 error；operator `pytest -m "not integration"` 全綠（operator 未改，應自然綠）。
- [ ] **5-lens loop-until-dry 對抗式審查** × 3 票 × fix loop until 2 dry。守線：per-route sync 冪等/孤兒、migration 不失保護+冪等、ACL related-store walk、white-list（route_id pin）、quota SA 級語義、login/session per-route 寫回、su-env guard、HttpCase 對等、OWL。
- [ ] **stg 遷移驗證**（merge 後 deploy-stg 帶新版）：既有 test SA 的 policies/login/session 平滑搬到 per-route；**手動 teardown 舊 SA 層 CF app**（migration log 記的舊 app id）→ 每個 protected route per-route re-sync 重建自己的 app；CF 零孤兒。
- [ ] **知識持久化**：spec 狀態改 "P1 + P2(Per-route Access) implemented"；follow-ups 記 P2.5 重構 + stg 遷移步驟。
- [ ] **manifest version bump**。
- [ ] P2.5 PR → develop。

## 驗收標準

- 每條 route 各自一個 CF Access app（domain=該 route hostname）、各自 policies/login/session；is_protected toggle 控制建/拆該 route 的 app。
- Application tab 無 Protected 欄、無 Primary badge。Access tab = per-route Protected Applications（無 Service-wide 切換器）。
- Odoo 全模組 0 failed；operator 全綠（未改）；headed Access tab 無 console error。
- 既有 stg test SA 遷移後保護不中斷、CF 零孤兒。

## Self-review check
- 已對照 understand 三份盤點（model/operator/前端）+ canonical 設計 + 3 個拍板決策。
- operator 不改（per-app 已支援，Odoo per-route 各打一次）→ 仍 image-only（甚至不用重建 operator image）。
- 型別一致：route._build_access_payload ↔ operator AccessSyncRequest（單 hostname）↔ route.to_dict ↔ 前端 service。
- migration：prod no-op（無 P2 資料）、stg DB 搬遷 + 手動 CF teardown，冪等。
- quota 維持 SA 級總量（via security_access_id related-store）。
