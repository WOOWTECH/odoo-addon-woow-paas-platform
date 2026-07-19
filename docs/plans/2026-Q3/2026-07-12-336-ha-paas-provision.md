---
title: "HA PaaS 0 元隱藏訂閱＋開通 API 實作 Plan（#336）"
category: plans
status: done
created: 2026-07-12
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-12-336-ha-paas-provision.md
promoted-at: 2026-07-19
---

# HA PaaS 0 元隱藏訂閱＋開通 API 實作 Plan（#336）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 HA App 以 OAuth Bearer token 一鍵在 PaaS 開通一個「0 元、每帳號限一、全前台隱藏」的雲端 Home Assistant 服務，並提供 App 端輪詢用的開通/狀態 API（依契約 v1.1 §7）。

**Architecture:** 新增 `ha:provision` OAuth scope（含自足 App public client seed ＋ idempotent healer）；新控制器 `ha_paas.py` 以 Bearer token 保護 `POST /api/ha-paas/provision`＋`GET /api/ha-paas/status`，冪等以 `partner_id` 為鍵、`pg_advisory_xact_lock`＋partial unique index 雙層防並發，統一走既有 deploy cron；新增 0 元 `sh.subscription.plan`＋`product.product`＋隱藏的 `cloud_app_template`（HA PaaS），在 `cloud_app_template` 加 `is_hidden` 旗標並以 DRY helper 從 marketplace／workspace／dashboard／billing 全鏈過濾。工作項 5（Elmo 輕量 HA image）blocked，故 HA PaaS template 以 `is_active=False`＋佔位 chart seed，provision 端點在 template 未 deployable 時回 503 且不建 service。

**Tech Stack:** Odoo 18 addon（Python 4-space、OWL/XML 2-space）、自寫 OAuth 2.0 provider（`src/controllers/oauth2.py`）、上游 `sh_subscription` / `sh_subscription_wallet`、PaaS Operator（Helm CLI wrapper）、PostgreSQL advisory locks、`TransactionCase` / `HttpCase` 測試。

---

## Global Constraints

- **版號 bump**：`src/__manifest__.py` `18.0.1.0.53` → `18.0.1.0.54`（現況 .53，下一號 .54）。
- **契約 v1.1 為 single source of truth**：`docs/reference/api/ha-app-oauth-provisioning.md §7`。任何回應 shape / scope 名 / 狀態枚舉變更都要同步更新 §7 並知會 App 側（Elmo）。契約枚舉（**v1.1，Q4 拍板擴充**）：`provision` 回 `202 {"status":"provisioning"}` / `200 {"status":"ready","ha_url":...}` / `409 {"status":"suspended"|"deleting","error":…}`；`status` 回 `200 {"status":"none"|"provisioning"|"ready"|"error"|"suspended"|"deleting","ha_url":…,"error":…}`（`ha_url` 僅 ready、`error` 僅 error；`suspended`/`deleting` 直映 `cloud_service.state`）；`401`=token 無效、`403`=缺 `ha:provision` scope。契約 §7 的 v1.1 修訂與端點實作同 PR 原子交付（Task 6 Step 7）。
- **scope 名固定**：`ha:provision`（契約 §7.1，App 主流程唯一必要 scope）。
- **不可複用 `woow-ha-smart-home` client 給 App**（契約 §9 風險 1）——本 plan 新建 `oauth_client_ha_app`。
- **seed noupdate 規則**：新 xmlid 在 `-u` 一律建立（noupdate 只 gate 既有 xmlid 的 UPDATE）；含 M2M 寫入 template 的 record **絕不可放 noupdate=1 檔**（sticky skip 事故）。
- **controller 層不得 `.sudo()` 繞 ir.rule**（repo-lint baseline gate）；新 controller 的每個 `.sudo()` 需有正當理由並更新 baseline。
- **PR 用 `Refs #336`（勿 `Closes`）**——工作項 5（Elmo image）未完，issue 不關。
- **原始碼、註解、commit message 一律繁體中文（zh-tw）**；commit prefix 用 `feat:`/`test:`/`docs:`/`chore:` 等。
- **⚠️ 環境註記**：本 plan 撰寫當下 session 因 macOS 檔案存取封鎖無法複讀 `src/` 既有原始碼，下列所有 `file:line` 為五份獨立 primary-source 審查（各附 file:line）交叉佐證後的最佳定位。**實作每一步務必以編輯當下實檔重新核對行號與函式簽名**（用 grep 定位，不要盲信本 plan 的行號）。

---

## 設計決策（對抗式 plan-review 前的多視角壓測裁決）

本 plan 的設計經 5 視角獨立審查（idempotency-concurrency / billing-0yuan / hidden-chain / oauth-scope-security / seed-migration-guard）＋仲裁綜合。關鍵裁決：

### D1. OAuth healer target → 走「自足 seed App client」（岔路 1a）
`#336` 直接 seed App 專用 public client `oauth_client_ha_app`，healer 以 `env.ref` 冪等補 `ha:provision` scope。理由：`client_id` 非 secret（RFC 6749 §2.2；既有 `woow-ha-smart-home` 早已明碼 seed），故此路無保密代價卻換來 self-contained ＋ `env.ref` 可端到端測 ＋ fail-loud-by-construction。契約 §9 只禁「複用」既有 client、不禁「新建」。**含意：#340 由「ops 建 client」重界定為「stg device-flow 驗證＋憑證交付 Elmo＋prod 微調」——此為跨團隊職責邊界，需 Eugene 拍板（見 Open Questions Q1）。**

### D2. Provision workspace 策略 → 每帳號建專屬 hidden workspace（岔路 2a）
HA PaaS service 住在 provision 當下自動建立的專屬 workspace（→ 專屬 K8s namespace，獨立 quota/NetworkPolicy 封套）。理由：把 0 元 hidden 服務塞進使用者付費 workspace 會吃掉其付費 quota（公平性 bug），且對「尚無 workspace 的新用戶」複用策略會洩漏 workspace 概念（違契約 §7.5）。**SSOT 仍只有 `template.is_hidden`**——不新增 `workspace.is_hidden` 欄位，`_hidden_workspace_ids` 由 hidden service 的 `workspace_id` 反推。**不變式（provision 須以 code 保證）**：hidden-template service 只住其專屬 workspace 且該 workspace 不含其他 service。

### D3. 每帳號限一 enforcement → `partner_id` 鍵 ＋ advisory lock ＋ partial unique index
- **鍵 = `partner_id`**（`token.user_id.partner_id`，對齊 subscription 層 `sh_partner_id`），正確去重「同 partner 多 user／多 workspace」。入口守衛 partner 存在且非 public/OdooBot/system partner。
- **denormalize**：HA PaaS 的 `cloud_service` 列填 `provision_partner_id`（僅此類服務填值）。
- **應用層序列化**：provision 與 error→redeploy 分支進入點，**在 search 之前**取 `pg_advisory_xact_lock(_HA_PROVISION_LOCK_NS, partner_id)`（比照既有 `project_project.py:393` 的 reach-lock；xact 版隨 commit/rollback 自動釋放）。**不可用 `SELECT ... FOR UPDATE`**（目標列尚不存在，phantom 鎖不到）。
- **DB backstop**：`.54` migration 建 partial unique index `... ON <cloud_service table>(provision_partner_id) WHERE provision_partner_id IS NOT NULL`。並發第二筆 INSERT commit 撞 IntegrityError → controller catch → 回 existing。**不加 `WHERE state != 'deleting'`**（否則放行 delete/create race）。
- **狀態機冪等**：provision **不 inline 呼叫 `action_redeploy`**（它同步打 operator HTTP，鎖內持有會阻塞 round-trip）；鎖內只翻 state（error→pending）＋觸發 cron，回 202。

### D4. Hidden 鏈 → template 層 SSOT ＋ DRY helper 全鏈過濾（呈現層，非 ACL）
`is_hidden` 定性為 /woow 呈現層過濾（租戶知 id 仍可打 JSON 端點；Odoo 原生 `/my` portal 與 `/report` PDF 完全繞過本層——見 Open Questions Q2/Q3）。完整曝光點與 helper 見 Task 3。

### D5. 0 元 seed → 無需 creation migration
新 xmlid（`plan_ha_paas`/`product_ha_paas`/`cloud_app_ha_paas`）在 `-u` 一律建立，**不需** `.52`-式 delegate migration（那是為 reprice 既有 sticky 記錄）。唯一需 `.54/post-migrate.py` 的是 D3 的 partial unique index（schema，非 seed；delegate 到 model method 供單測，比照 .52 慣例）。

### D6. 工作項 5 blocked → provision 對「image 未備」回 503、不建 service
deploy cron `_cron_process_pending_deploys` 以 `search([('state','=','pending')])` 撈件、無 `is_active`/`ingress_enabled` 過濾——任何 pending service 都會被 `_execute_deploy` 拉佔位 chart 而失敗。故 HA PaaS template seed `is_active=False`＋佔位 chart；provision 在建 service 前檢查 deployability，未備回 503 且**不建 cloud_service、不留 pending 件**。工作項 5 落地時只需在 `cloud_app_templates.xml`（noupdate=0）填真 image＋`is_active=True`＋`-u`，無 migration。

---

## Open Questions（2026-07-12 Eugene 已拍板；以下為權威結論）

> Q1–Q5 於 2026-07-12 由 Eugene 拍板，結論如下（每項標「✅ 拍板」）。本 plan 已依結論對齊。

- **Q1（#340 邊界）**：D1 在 #336 直接 seed App client，需 ops 同意把 #340 重界定為「驗證＋憑證交付＋prod 微調」而非「建 client」。跨團隊職責，非技術可自決。
  - **✅ 拍板＝(a)**：照 plan D1，#336 直接 seed 自足 App public client（`oauth_client_ha_app`）。#340 已重界定為「stg device-flow 驗證＋憑證交付 Elmo＋prod 微調」，不在本 PR 範圍。
- **Q2（by-id 端點：呈現過濾 vs fail-closed 隔離）**：只擋 list、by-id 不擋時，租戶知 `service_id`/`sub_id`/`invoice_id` 仍能操作隱藏物件。本 plan 採「by-id 端點在 `exists()` 後補 `template_id.is_hidden → 回 not found`」（閉合最完整、代價低），但這是「呈現層 vs ACL 級隔離」的層級決策。
  - **✅ 拍板＝(a)**：照 plan——by-id 端點在 `exists()` 後補 hidden guard 回 not found（曝光點 2/9/10/13 等，見 Task 3）。
- **Q3（0 元發票：源頭抑制 vs filter）**：billing cron 每期建的 0 元 `out_invoice` 是真記錄，Odoo 原生 `/my/invoices` 與 `/report/pdf` 完全繞過 /woow helper。是否讓 hidden 0 元 sub **不產生可見 posted out_invoice**（cron 推進 next_payment 但不留可見發票）？本 plan 採 filter 路線（不動 #324 計費路徑）；源頭抑制需先確認「cron 推進 next_payment 不依賴發票 posted 狀態」。
  - **✅ 拍板＝(a)**：照 plan——採 filter 路線（不動 #324 計費路徑），hidden 0 元發票以 helper 從 billing 前台過濾。
- **Q4（status 契約枚舉擴充）**：契約 v1 只有 `none/provisioning/ready/error` 4 值，但 `cloud_service.state` 有 8 值。`suspended`（蓄意停用）映三者皆勉強；`deleting` 映 `none` 會撞正在進行的 delete。本 plan（原）採「保守映射＋fail-closed 不洩漏 ha_url」，但是否擴充枚舉是契約 v1 修訂決策。
  - **✅ 拍板＝擴充枚舉修契約（推翻原保守映射）**：契約 §7 升 **v1.1**——status 枚舉擴為 `none/provisioning/ready/error/suspended/deleting`（`ha_url` 僅 `ready` 給）。App 指引：`suspended`＝服務暫停，聯繫支援；`deleting`＝刪除中，稍後可重新開通。Task 6 改為 **v1.1 直映**（`suspended→suspended`、`deleting→deleting`），並於同 PR 原子修訂契約文件 §7（見 Task 6 新增 step「契約 §7 修訂 v1.1」）。
- **Q5（ECPay 0 元 confirm 行為）**：若 `company.auto_invoice='automatic'`，0 元發票 post 時可能嘗試開 ECPay（0 元最易被拒）→ UserError 回滾 confirm。需依 company 實際設定壓測（見 Task 4 測試）；可能需產品決策。
  - **✅ 拍板**：照 plan 壓測（Task 4），把結果寫進 PR／回報；**先不做產品決策**（僅坐實現況行為，若壓測揭露阻塞再另議）。

---

## File Structure

**新增檔案**
- `src/controllers/ha_paas.py` — provision/status 端點（Bearer + `ha:provision`）＋ hidden-chain DRY helper mixin（或放共用 base，見 Task 3）。
- `src/data/oauth_ha_provision_scope_update.xml` — `<function>` healer wiring（每 `-u` 跑），比照 `oauth_scope_updates.xml`。
- `src/migrations/18.0.1.0.54/post-migrate.py` — 呼叫 model method 建 partial unique index（delegate 供單測，比照 .52）。
- `src/tests/test_ha_paas_provision.py` — provision/status 端點行為（HttpCase/TransactionCase）。
- `src/tests/test_ha_paas_zero_price_billing.py` — 0 元 × #324 payment gate 邊界坐實。
- `src/tests/test_hidden_template_chain.py` — is_hidden 全鏈過濾。
- `src/tests/test_ha_provision_scope_healer.py` — scope seed ＋ healer 冪等。

**修改檔案**
- `src/models/cloud_app_template.py` — 加 `is_hidden` Boolean（default False）。
- `src/models/cloud_service.py` — 加 `provision_partner_id` Many2one（僅 HA PaaS 填）＋ `_ensure_ha_provision_unique_index` model method（供 migration delegate）；`_HA_PROVISION_LOCK_NS` 常數。
- `src/models/oauth_client.py` — 加 `_ensure_ha_app_provision_scope` healer（比照 `_ensure_ha_smart_home_sa_scopes`）。
- `src/controllers/oauth2.py` — `scope_descriptions` dict 加 `ha:provision`；**並定位 device consent 頁的 scope 渲染處補上描述**（見 Task 1 F1）。
- `src/controllers/paas.py` — hidden 過濾套進 templates/services/by-id 端點；generic create 拒 hidden template。
- `src/controllers/billing.py` — hidden 過濾套進 subscriptions/detail/invoices/renewals。
- `src/controllers/dashboard.py` — hidden 過濾套進 service count / active subs / workspace list。
- `src/data/oauth_clients.xml` — seed `oauth_client_ha_app`。
- `src/data/subscription_products.xml` — seed `plan_ha_paas` ＋ `product_ha_paas`（noupdate=1，新 xmlid 自建）。
- `src/data/cloud_app_templates.xml` — seed `cloud_app_ha_paas`（noupdate=0、`is_hidden=True`、`is_active=False`、佔位 chart）。
- `src/data/subscription_template_links.xml` — `cloud_app_ha_paas` ↔ `plan_ha_paas` M2M link（noupdate=0）。
- `src/hooks.py` — `post_init_hook` 呼叫新 healer ＋ unique index method。
- `src/__manifest__.py` — 版號 .54；確認新 data XML 已在 `data` 清單（照既有順序：`oauth_clients` 早於端點、`cloud_app_templates` 早於 `subscription_products` 早於 `subscription_template_links`）。

---

## Task 1：`ha:provision` scope ＋ 自足 App client seed ＋ idempotent healer（工作項 1）

**Files:**
- Modify: `src/controllers/oauth2.py`（`scope_descriptions` dict；device consent 頁 scope 渲染）
- Modify: `src/models/oauth_client.py`（新 healer method）
- Modify: `src/hooks.py`（post_init wiring）
- Create: `src/data/oauth_ha_provision_scope_update.xml`（`<function>` wiring）
- Modify: `src/data/oauth_clients.xml`（seed `oauth_client_ha_app`）
- Test: `src/tests/test_ha_provision_scope_healer.py`

**Interfaces:**
- Produces：xmlid `woow_paas_platform.oauth_client_ha_app`（public client、含 `ha:provision` scope）；`OAuthClient._ensure_ha_app_provision_scope()`（`@api.model`，冪等）；scope 字串常數 `ha:provision`。
- Consumes：既有 `_ensure_ha_smart_home_sa_scopes`（`oauth_client.py:290` 附近）為 pattern 藍本；`get_allowed_scopes()`；`verify_oauth_token`（`oauth2.py:82`）。

> **pattern-guard 命中**：`src/hooks.py` → class `seed-default-template`；`src/data/*.xml` → class `seed-data`。編輯前照 hook 列出的 docs 讀完再重試（含 `docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md`）。

- [ ] **Step 1: 寫 healer 冪等測試（先失敗）**

`src/tests/test_ha_provision_scope_healer.py`：
```python
from odoo.tests.common import TransactionCase


class TestHAProvisionScopeHealer(TransactionCase):
    """ha:provision scope healer 冪等性（比照 _ensure_ha_smart_home_sa_scopes）。"""

    def test_seeded_app_client_has_provision_scope(self):
        client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.assertIn('ha:provision', client.get_allowed_scopes())
        self.assertEqual(client.client_type, 'public')
        self.assertFalse(client.client_secret_hash, 'public client 不得有 secret')

    def test_healer_grants_missing_scope_idempotently(self):
        client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        client.scopes = 'workspace:read smarthome:read'
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
        self.assertIn('ha:provision', client.get_allowed_scopes())
        before = client.scopes
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
        self.assertEqual(client.scopes, before)

    def test_healer_noop_when_client_absent(self):
        # method 對缺 client 安全（比照先例 if not client: return）
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestHAProvisionScopeHealer --stop-after-init`（或走 k8s-dev-sandbox `test.sh`）
Expected: FAIL — `ExternalId not found: woow_paas_platform.oauth_client_ha_app` / `_ensure_ha_app_provision_scope` 不存在。

- [ ] **Step 3: seed App public client**

`src/data/oauth_clients.xml`（在 `<data noupdate="1">` 內，既有 `oauth_client_ha_smart_home` 之後追加）：
```xml
<!-- HA App（Elmo 手機原生 App）專用 public client。與 HA component 的
     oauth_client_ha_smart_home 是不同 consumer（App→平台開通新 instance，
     見契約 §0/§9）。client_id 硬編在 App，readonly、不可旋轉。走 device
     flow 主線（無 redirect）；redirect_uris 為 model 必填欄位的佔位值，
     device flow 下不參與比對。scopes 含 ha:provision（開通寫入 scope）。 -->
<record id="oauth_client_ha_app" model="woow_paas_platform.oauth_client">
    <field name="name">Home Assistant App（Provisioning）</field>
    <field name="client_id">woow-ha-app</field>
    <field name="client_type">public</field>
    <field name="require_pkce" eval="True"/>
    <field name="redirect_uri_match_mode">exact</field>
    <field name="redirect_uris">urn:ietf:wg:oauth:2.0:oob</field>
    <field name="grant_types">urn:ietf:params:oauth:grant-type:device_code,refresh_token</field>
    <field name="scopes">workspace:read smarthome:read ha:provision</field>
    <field name="is_active" eval="True"/>
</record>
```
> ⚠️ 落地時先 grep 確認 `oauth_client` model 的 `redirect_uris` 是否 `required`（契約 §9 風險 1 指其 required）。若 `exact` mode 對 `urn:...oob` 佔位有 `_check` 阻擋，改用 loopback `http://127.0.0.1/` 佔位並在 record 註解說明。`client_secret_hash` 留空（public client `_check_client_type_consistency` 強制）。

- [ ] **Step 4: 寫 healer method（比照先例）**

`src/models/oauth_client.py`（緊接 `_ensure_ha_smart_home_sa_scopes` 之後）：
```python
@api.model
def _ensure_ha_app_provision_scope(self) -> None:
    """冪等把 ha:provision 併進 seed 的 HA App public client。

    oauth_clients.xml 是 noupdate=1，改 seed XML 到不了既有 DB。本 method
    比照 _ensure_ha_smart_home_sa_scopes，從 post_init（新裝）與
    data/oauth_ha_provision_scope_update.xml <function>（每次 -u）兩處呼叫，
    覆蓋新裝與升級兩條路徑。缺 client 時 no-op（fail-safe）。
    """
    required = ('ha:provision',)
    client = self.env.ref(
        'woow_paas_platform.oauth_client_ha_app',
        raise_if_not_found=False,
    )
    if not client:
        return
    granted = set((client.scopes or '').split())
    missing = [s for s in required if s not in granted]
    if missing:
        client.scopes = ' '.join(sorted(granted | set(missing)))
```

- [ ] **Step 5: wire healer 進 post_init ＋ `<function>`**

`src/data/oauth_ha_provision_scope_update.xml`（比照 `oauth_scope_updates.xml`）：
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- noupdate=0（default）：<function> 每次 -u 重跑，與 seed record 的
         noupdate 無關。oauth_clients.xml 是 noupdate=1，改 seed 只到 fresh
         install；既有 DB（sandbox/stg/prod 都跑 -u）靠本 function 補
         ha:provision。healer 冪等，重跑安全。post_init_hook 走 fresh install。 -->
    <function
        model="woow_paas_platform.oauth_client"
        name="_ensure_ha_app_provision_scope"/>
</odoo>
```
`src/hooks.py` `post_init_hook`（在既有 `env['...oauth_client']._ensure_ha_smart_home_device_grant()` 一行之後追加）：
```python
    env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
```
`src/__manifest__.py` `data` 清單：把 `data/oauth_ha_provision_scope_update.xml` 加在 `data/oauth_scope_updates.xml` 附近（healer function 需在 client seed 載入後執行）。

- [ ] **Step 6: scope 描述（authorize 頁 ＋ device 頁）**

`src/controllers/oauth2.py` `scope_descriptions` dict（約 `:224-233`）加一條：
```python
            'ha:provision': 'Create and manage your cloud Home Assistant instance',
```
> **F1（審查發現，關鍵）**：`scope_descriptions` 只被 `authorize_get`（Auth Code 流程）消費，`authorize` 模板渲染 `s['description']`。**App 主線走 device flow**，其 `/device` 同意頁（`oauth2.py:392/400`、scopes 來自 `device.client_id.get_allowed_scopes()` 約 `:447`）渲染路徑不同。落地時 **grep device 同意頁模板**（如 `src/views/oauth2_templates.xml` 的 device 區塊）確認 scope 描述如何渲染；若它另有 dict／直接印 raw scope 字串，需在該處也補 `ha:provision` 的人話描述，否則使用者在授權頁只看到 `ha:provision` raw 字串。此步的驗收＝device 同意頁顯示人話描述。

- [ ] **Step 7: 跑測試 ＋ device 同意頁驗收**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestHAProvisionScopeHealer --stop-after-init`
Expected: PASS（3 測試綠）。
**F1 視覺驗收（必做，非只跑 healer 測試）**：以 device flow 走一次 `/device`
授權頁（或對其模板寫 HttpCase 斷言），確認 `ha:provision` 顯示為人話描述
（「Create and manage your cloud Home Assistant instance」）而非 raw scope
字串。若 device 頁不讀 `scope_descriptions`，此步未過＝Step 6 的 F1 未完成。

- [ ] **Step 8: Commit**

```bash
git add src/data/oauth_clients.xml src/data/oauth_ha_provision_scope_update.xml \
        src/models/oauth_client.py src/controllers/oauth2.py src/hooks.py \
        src/__manifest__.py src/tests/test_ha_provision_scope_healer.py
git commit -m "feat: 新增 ha:provision scope＋HA App public client seed＋idempotent healer（Refs #336）"
```

---

## Task 2：0 元 HA PaaS plan／product／隱藏 template seed（工作項 3；部分工作項 5）

**Files:**
- Modify: `src/models/cloud_app_template.py`（加 `is_hidden`）
- Modify: `src/data/subscription_products.xml`（`plan_ha_paas` ＋ `product_ha_paas`）
- Modify: `src/data/cloud_app_templates.xml`（`cloud_app_ha_paas`）
- Modify: `src/data/subscription_template_links.xml`（M2M link）
- Test: `src/tests/test_hidden_template_chain.py`（本 task 先建 seed 存在性測試）

**Interfaces:**
- Produces：xmlid `plan_ha_paas`（`sh.subscription.plan`、0 價、`service_type=cloud_service`）；`product_ha_paas`（`product.product`、`list_price=0`）；`cloud_app_ha_paas`（`cloud_app_template`、`is_hidden=True`、`is_active=False`）；欄位 `cloud_app_template.is_hidden`。
- Consumes：既有 plan seed 形狀（`plan_n8n_monthly`／`product_n8n_monthly`）；`_check_service_type_matches_template_family`；`_ensure_default_template_links` healer。

> **pattern-guard 命中**：`src/data/*.xml` → `seed-data`。`cloud_app_template.py` 加欄位不命中 model-write-guard（該 class pattern 是 `cloud_service` 非 `cloud_app`），但仍照 hook 指示讀 seed-data docs。

- [ ] **Step 1: 寫 seed 存在性 ＋ is_hidden 預設測試（先失敗）**

`src/tests/test_hidden_template_chain.py`（先只放 seed 存在性）：
```python
from odoo.tests.common import TransactionCase


class TestHAPaaSSeed(TransactionCase):
    """HA PaaS 0 元 plan／product／隱藏 template seed 存在性與屬性。"""

    def test_ha_paas_template_hidden_and_inactive(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.assertTrue(tpl.is_hidden, 'HA PaaS template 必須隱藏')
        self.assertFalse(tpl.is_active, '工作項5 image 未備前 template 不可 deployable')

    def test_ha_paas_plan_zero_price_cloud_service(self):
        plan = self.env.ref('woow_paas_platform.plan_ha_paas')
        self.assertEqual(plan.sh_plan_price, 0.0)
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertFalse(plan.sh_trial, '0 元不走 trial 路徑')

    def test_ha_paas_template_linked_to_zero_plan(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        plan = self.env.ref('woow_paas_platform.plan_ha_paas')
        self.assertIn(plan, tpl.subscription_plan_ids)

    def test_existing_templates_default_visible(self):
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertFalse(n8n.is_hidden)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `--test-tags /woow_paas_platform:TestHAPaaSSeed`
Expected: FAIL — xmlid 不存在 / `is_hidden` 欄位不存在。

- [ ] **Step 3: 加 `is_hidden` 欄位**

`src/models/cloud_app_template.py`（緊接 `is_active` 欄位定義之後）：
```python
    is_hidden = fields.Boolean(
        string='Hidden',
        default=False,
        help='若為真，此 template 及其 services／訂閱／發票不出現在 /woow '
             '前台任何清單（marketplace、workspace、dashboard、billing）。'
             '呈現層過濾、非 ACL。用於 HA PaaS 這類平台自動開通的隱藏服務。',
    )
```
> `-u` 會自動 ALTER TABLE 加欄並以 `default=False` 回填既有列（維持可見）。無需 migration。

- [ ] **Step 4: seed 0 元 plan ＋ product**

`src/data/subscription_products.xml`（整檔 `noupdate="1"`，新 xmlid 於 `-u` 自建）：
```xml
<!-- HA PaaS 0 元 plan：客人無地端設備也能一鍵開雲端 HA。0 價原生可表達
     （sh_plan_price=0），走 active 路徑（sh_trial=False）。每帳號限一由
     開通端點 enforce（#336）。service_type=cloud_service 與掛載它的
     cloud_app_ha_paas 家族一致（_check_service_type_matches_template_family）。 -->
<record id="plan_ha_paas" model="sh.subscription.plan">
    <field name="name">HA PaaS（免費雲端 Home Assistant）</field>
    <field name="sh_duration">1</field>
    <field name="sh_unit">month</field>
    <field name="sh_plan_price">0</field>
    <field name="sh_override_product" eval="True"/>
    <field name="sh_no_of_billing_cycle">-1</field>
    <field name="sh_never_expire" eval="True"/>
    <field name="sh_trial" eval="False"/>
    <field name="sh_start_immediately" eval="False"/>
    <field name="sh_is_close_by_customer" eval="True"/>
    <field name="service_type">cloud_service</field>
</record>
```
```xml
<record id="product_ha_paas" model="product.product">
    <field name="name">HA PaaS（免費雲端 Home Assistant）Subscription</field>
    <field name="type">service</field>
    <field name="list_price">0</field>
    <field name="sale_ok" eval="True"/>
    <field name="purchase_ok" eval="False"/>
    <field name="sh_product_subscribe" eval="True"/>
    <field name="sh_subscription_plan_id" ref="plan_ha_paas"/>
</record>
```
> 落地前 grep 一個既有 cloud_service plan（如 `plan_n8n_monthly`）逐欄比對，確認上游 `sh.subscription.plan` 欄位名一致（尤其 `sh_reminder` 是否必填——本 seed 略過，若上游要求則補既有 `reminder_*` ref）。

- [ ] **Step 5: seed 隱藏 HA PaaS template（noupdate=0）**

`src/data/cloud_app_templates.xml`（在 `<data noupdate="0">` 內追加）：
```xml
<!-- HA PaaS（免費雲端 Home Assistant）。工作項5（Elmo 輕量 HA image）未
     交付前，helm_chart_name 為佔位、is_active=False（供 provision 端點
     deployability 檢查）。工作項5 落地：填真 image + is_active=True + -u 即
     生效，無 migration。is_hidden=True：不出現在前台任何清單。 -->
<record id="cloud_app_ha_paas" model="woow_paas_platform.cloud_app_template">
    <field name="name">HA PaaS</field>
    <field name="slug">ha-paas</field>
    <field name="description">免費雲端 Home Assistant（平台自動開通）</field>
    <field name="category">container</field>
    <field name="is_hidden" eval="True"/>
    <field name="is_active" eval="False"/>
    <field name="helm_repo_url">TBD-elmo-lightweight-ha</field>
    <field name="helm_chart_name">TBD</field>
    <field name="helm_chart_version">0.0.0</field>
    <field name="default_port">8123</field>
    <field name="ingress_enabled" eval="True"/>
    <field name="min_vcpu">1</field>
    <field name="min_ram_gb">1</field>
    <field name="min_storage_gb">5</field>
</record>
```
> 落地前 grep `cloud_app_template` 必填欄位（`category` selection 值、`helm_*` 是否必填）逐欄核對。**絕不可把此 record 放進 noupdate=1 檔**（sticky skip 事故）。

- [ ] **Step 6: M2M link（noupdate=0）**

`src/data/subscription_template_links.xml`（在 `<data noupdate="0">` 內追加）：
```xml
<record id="cloud_app_ha_paas" model="woow_paas_platform.cloud_app_template">
    <field name="subscription_plan_ids" eval="[(6, 0, [ref('plan_ha_paas')])]"/>
</record>
```

- [ ] **Step 7: 跑測試確認通過**

Run: `--test-tags /woow_paas_platform:TestHAPaaSSeed`
Expected: PASS（4 測試綠）。若 `_check_service_type_matches_template_family` 報錯，檢查 link 是否把 plan 綁到非 cloud_app 家族 template。

- [ ] **Step 8: Commit**

```bash
git add src/models/cloud_app_template.py src/data/subscription_products.xml \
        src/data/cloud_app_templates.xml src/data/subscription_template_links.xml \
        src/tests/test_hidden_template_chain.py
git commit -m "feat: 新增 HA PaaS 0 元 plan／product／隱藏 template seed＋is_hidden 欄位（Refs #336）"
```

---

## Task 3：Hidden 鏈全鏈過濾（DRY helper ＋ 各曝光點）（工作項 1／4）

**Files:**
- Create/Modify: `src/controllers/ha_paas.py`（放 helper mixin `HiddenChainMixin`；或放既有共用 base——落地時 grep 現有 controller 有無共用 base class）
- Modify: `src/controllers/paas.py`、`src/controllers/billing.py`、`src/controllers/dashboard.py`
- Test: `src/tests/test_hidden_template_chain.py`（追加過濾測試）

**Interfaces:**
- Produces：`HiddenChainMixin` 四個 helper——`_hidden_template_ids()`／`_hidden_service_ids()`／`_hidden_subscription_ids()`／`_hidden_workspace_ids()`（回 id list）。
- Consumes：`cloud_service.template_id`／`.subscription_id`／`.workspace_id`；`account.move.sh_subscription_id`。

> **pattern-guard 命中**：`src/controllers/*.py` → `controller-endpoint`（不可 `.sudo()` 繞 ir.rule；helper 讀跨租戶 id 集用於呈現層過濾，若需 sudo 要更新 repo-lint baseline 並附 justification）。

**曝光點清單（草案 8 點 ＋ 審查補漏，全部要過濾）：**

| # | 端點/位置 | file:line（落地核對） | 過濾規則 |
|---|---|---|---|
| 1 | `api_cloud_templates` domain | `paas.py:797` | `('is_hidden','=',False)` |
| 2 | `api_cloud_template` 單 detail | `paas.py:838` | hidden → not found |
| 3 | `_list_services` | `paas.py:2033` | `('template_id','not in',hidden_template_ids)` |
| 4 | dashboard 服務計數 | `dashboard.py:196`+`:199` | 同上加進 search_count domain |
| 5 | dashboard active subs（＋`primary` 取值） | `dashboard.py:105`／`:113` | **先濾再取 primary**：`('id','not in',hidden_subscription_ids)`，避免 0 元 sub 搶 primary 綁架 cycle/next_bill |
| 6 | `api_subscriptions` | `billing.py:787`／`:800` | `('id','not in',hidden_subscription_ids)` |
| 7 | `api_subscription_detail` | `billing.py:809` | hidden sub → not found；service_list 也濾 |
| 8 | `api_invoices` | `billing.py:970`／`:993` | `('sh_subscription_id','not in',hidden_subscription_ids)`（`not in` 保留 NULL → topup 發票不受影響）|
| 9 | `api_service_change_plan` | `paas.py:942` | hidden service → not found |
| 10 | `_get_service` / `_format_service` | `paas.py:1178`／`:2246`／`:2839` | hidden service by-id → not found |
| 11 | revisions / rollback | `paas.py:1243`／`:1198` | 同上 |
| 12 | MCP servers `api_service_mcp_servers` | `paas.py:1343`（`_list_mcp_servers`:1400） | 同上 |
| 13 | `api_invoice_detail` | `billing.py:1044` | hidden sub 的發票 → not found |
| 14 | `api_upcoming_renewals` | `billing.py:893` | `('id','not in',hidden_subscription_ids)`（0 元 sub 每月續期會再現）|
| 15 | dashboard workspace list/count `_workspaces_block` | `dashboard.py:194`/`:197` | 排除專屬 hidden workspace（`('id','not in',hidden_workspace_ids)`）|
| 16 | generic create `api_workspace_services` | `paas.py:1101`（`_create_service`:2060） | 拒絕 hidden template（hidden 只能經 provision 端點建）|

**不濾（明確不改）**：`reconcile_report.py:27`（`_is_admin` gated）；Odoo backend 原生 tree/form（ops 可見＝設計可接受，hidden ≠ ACL）。

- [ ] **Step 1: 寫過濾測試（先失敗）**

`src/tests/test_hidden_template_chain.py` 追加：
```python
from odoo.tests.common import HttpCase


class TestHiddenChainFiltering(HttpCase):
    """HA PaaS（hidden template）的 service／訂閱／發票不出現在前台任何清單。"""

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'Hidden Chain User', 'login': 'hiddenchain@test',
            'email': 'hiddenchain@test',
        })
        self.partner = self.user.partner_id
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'HA PaaS WS', 'owner_id': self.user.id,
        })
        self.svc = self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'ha-paas', 'reference_id': 'test-ha-ref',
            'state': 'running', 'subdomain': 'ha-test',
            'provision_partner_id': self.partner.id,
        })
        self.svc._create_and_confirm_subscription(self.partner, self.plan)

    def test_marketplace_excludes_hidden_template(self):
        ids = self.env['woow_paas_platform.cloud_app_template'].search(
            [('is_hidden', '=', False)]).ids
        self.assertNotIn(self.tpl.id, ids)

    def test_hidden_service_bridged(self):
        CS = self.env['woow_paas_platform.cloud_service']
        self.assertIn(self.svc, CS.search([('template_id.is_hidden', '=', True)]))
        self.assertTrue(self.svc.subscription_id)
```
> 落地時把「session 登入該 user、POST 各端點、assert hidden id 不在 data」的 HttpCase 補完整，至少覆蓋端點 1、3、6、8、14、16。

- [ ] **Step 2: 跑測試確認失敗**（helper 未建 / 端點未過濾）

- [ ] **Step 3: 寫 DRY helper mixin**

`src/controllers/ha_paas.py`（或共用 base；落地時 grep 現有 controller 是否已有共用 base 可掛）：
```python
from odoo.http import request


class HiddenChainMixin:
    """HA PaaS 這類 hidden template 的呈現層過濾 helper。SSOT = template.is_hidden。
    每個 request 最多算一次（呼叫端可自行 memoize）。"""

    def _hidden_template_ids(self):
        return request.env['woow_paas_platform.cloud_app_template'].sudo() \
            .search([('is_hidden', '=', True)]).ids

    def _hidden_service_ids(self):
        CS = request.env['woow_paas_platform.cloud_service'].sudo()
        return CS.search([('template_id', 'in', self._hidden_template_ids())]).ids

    def _hidden_subscription_ids(self):
        # 依賴 D2 不變式：hidden-template service 只住其專屬 hidden workspace、
        # 該 workspace 不含其他 service（provision 端點以 code 保證）。故對每個
        # hidden service 的 M2O subscription_id 攤平即為完整 hidden sub 集。
        CS = request.env['woow_paas_platform.cloud_service'].sudo()
        svcs = CS.browse(self._hidden_service_ids())
        return [s.id for s in svcs.subscription_id if s]  # 濾 falsy

    def _hidden_workspace_ids(self):  # 2a 專屬
        CS = request.env['woow_paas_platform.cloud_service'].sudo()
        return CS.browse(self._hidden_service_ids()).mapped('workspace_id').ids
```
> helper **必須 state-agnostic（含 cancel）**：刪服務後留下的 cancelled 0 元 sub ＋ 歷史 0 元發票不可漏進 billing 歷史（上面對 state 無過濾，符合）。讓相關 controller class 繼承 `HiddenChainMixin`。

- [ ] **Step 4: 套用過濾到 16 個曝光點**

逐點依上表套用（每點一個 grep 定位 → 加 domain/guard）。範例：
```python
# 曝光點 1（paas.py api_cloud_templates）
domain = [('is_active', '=', True), ('is_hidden', '=', False)]
# 曝光點 6（billing.py api_subscriptions）
domain = [('sh_partner_id', '=', partner.id),
          ('id', 'not in', self._hidden_subscription_ids())]
# 曝光點 2/9/10/13（by-id not-found guard，對應 Open Question Q2 閉合方案）
if not template.exists() or not template.is_active or template.is_hidden:
    return {'success': False, 'error': 'Template not found'}
if svc.template_id.is_hidden:
    return {'success': False, 'error': 'Service not found'}
# 曝光點 16（generic create 拒 hidden）
if template.is_hidden:
    return {'success': False, 'error': 'This template cannot be deployed directly.'}
# 曝光點 5（dashboard 先濾再取 primary）
active_subs = request.env['sh.subscription.subscription'].sudo().search(
    [('sh_partner_id', '=', partner.id), ('state', '=', 'in_progress'),
     ('id', 'not in', self._hidden_subscription_ids())],
    order='sh_start_date desc, id desc')
```

- [ ] **Step 5: 跑過濾測試 ＋ 既有 dashboard/billing 回歸**

Run: `--test-tags /woow_paas_platform:TestHiddenChainFiltering,/woow_paas_platform:TestDashboardSummary`
Expected: PASS。既有 `test_dashboard_summary.py` 不得回歸。

- [ ] **Step 6: repo-lint sudo baseline**

Run: `python scripts/audit/repo-lint.py`
若新 helper 的 `.sudo()` 讓某 controller 超出 baseline：`python scripts/audit/repo-lint.py --update-baseline` 並在 commit 附 justification（helper 讀跨租戶 template/service id 集僅供呈現層過濾）。

- [ ] **Step 7: Commit**

```bash
git add src/controllers/ha_paas.py src/controllers/paas.py src/controllers/billing.py \
        src/controllers/dashboard.py src/tests/test_hidden_template_chain.py \
        scripts/audit/baseline/controller-sudo.txt
git commit -m "feat: HA PaaS hidden template 全鏈呈現層過濾（marketplace/workspace/dashboard/billing）（Refs #336）"
```

---

## Task 4：0 元 × #324 payment gate 邊界坐實（工作項 6 核心）

**Files:**
- Test: `src/tests/test_ha_paas_zero_price_billing.py`

**Interfaces:**
- Consumes：`sh_subscription_fix._has_paid_current_cycle`（`:43`）／`_compute_unused_refund`（`:72`）／`_settle_on_entity_delete`（`:116`，退點 gate `if refund > 0 and partner:` `:143`）；`account.move._pay_from_wallet`（`:38`，`amount<=0` guard `:72-76`）；`_create_and_confirm_subscription`（`subscription_lifecycle_mixin.py:108`）。

> 純測試（無 src 變更），坐實既有行為＝0 元訂閱永無 `done` deduct → 退點恆 0，且 0 元路徑不觸發 `_pay_from_wallet`。比照既有 `test_refund_requires_payment.py` fixture。

- [ ] **Step 1: 寫 0 元邊界測試**

`src/tests/test_ha_paas_zero_price_billing.py`：
```python
from odoo.tests.common import TransactionCase


class TestHAPaaSZeroPriceBilling(TransactionCase):
    """0 元 HA PaaS 訂閱 × #324 payment gate 邊界（退點恆 0、不建 deduct）。"""

    def setUp(self):
        super().setUp()
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas')
        self.partner = self.env['res.partner'].create({'name': 'Zero Price User'})
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTxn = self.env['wallet.transaction'].sudo()

    def _make_zero_sub(self):
        product = self.env['product.template'].search(
            [('sh_product_subscribe', '=', True),
             ('sh_subscription_plan_id', '=', self.plan.id)], limit=1)
        self.assertTrue(product, '0 元 plan 對應 product 必須存在')
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'product_id': product.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_source': 'manual',
        })

    def test_zero_sub_has_no_done_deduct(self):
        sub = self._make_zero_sub()
        self.assertFalse(self.WalletTxn.search_count([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'),
            ('state', '=', 'done'),
        ]), '0 元訂閱不得產生 done deduct')
        self.assertFalse(sub._has_paid_current_cycle(),
                         '0 元訂閱 _has_paid_current_cycle 恆 False')

    def test_zero_sub_refund_is_zero(self):
        sub = self._make_zero_sub()
        self.assertEqual(sub._compute_unused_refund(), 0.0,
                         '0 元訂閱退點恆 0（#324 payment gate 邊界，無套利）')

    def test_zero_invoice_never_hits_pay_from_wallet(self):
        from odoo.exceptions import UserError
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'HA PaaS 0 元', 'quantity': 1, 'price_unit': 0.0})],
        })
        move.action_post()
        with self.assertRaises(UserError):
            move._pay_from_wallet()  # amount<=0 → "Nothing to pay"
```
> 落地時對照 `test_refund_requires_payment.py` 的 `_mid_cycle_sub`/`_seed_deduct`；若 `_compute_unused_refund` 需 sub 有 start/next_payment 才不噴錯，補 `sh_start_date`/`sh_date_of_next_payment`（today±15d）。confirm 路徑若觸發 ECPay（Open Question Q5），視 company 設定調整。

- [ ] **Step 2: 跑測試（依賴 Task 2 seed）**

Run: `--test-tags /woow_paas_platform:TestHAPaaSZeroPriceBilling`
Expected: PASS（3 測試綠）。

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_ha_paas_zero_price_billing.py
git commit -m "test: 坐實 0 元 HA PaaS 訂閱 × #324 payment gate 邊界（退點恆 0、不觸發扣款）（Refs #336）"
```

---

## Task 5：`provision_partner_id` 欄位 ＋ partial unique index migration（工作項 5 每帳號限一 DB 層）

**Files:**
- Modify: `src/models/cloud_service.py`（`provision_partner_id`、`_HA_PROVISION_LOCK_NS`、`_ensure_ha_provision_unique_index`）
- Create: `src/migrations/18.0.1.0.54/post-migrate.py`
- Modify: `src/hooks.py`（post_init 呼叫 index method）
- Test: `src/tests/test_ha_paas_provision.py`（欄位＋index 存在）

**Interfaces:**
- Produces：`cloud_service.provision_partner_id`（Many2one `res.partner`）；module 常數 `_HA_PROVISION_LOCK_NS`（int）；`CloudService._ensure_ha_provision_unique_index()`（`@api.model`，冪等）。
- Consumes：migration delegate 慣例（`.52` `_woow_migrate_pricing_330`）。

> **pattern-guard 命中**：`src/models/cloud_service.py` → `model-write-guard`。編輯前照 hook 讀 docs（`two-stage-remote-provision-idempotency.md` 等）。

- [ ] **Step 1: 寫欄位＋index 測試（先失敗）**

`src/tests/test_ha_paas_provision.py`（本 task 部分）：
```python
from odoo.tests.common import TransactionCase


class TestProvisionUniqueIndex(TransactionCase):
    """provision_partner_id 欄位 ＋ partial unique index（每帳號限一 DB backstop）。"""

    def test_field_exists(self):
        self.assertIn('provision_partner_id',
                      self.env['woow_paas_platform.cloud_service']._fields)

    def test_partial_unique_index_present(self):
        self.env.cr.execute("""
            SELECT indexdef FROM pg_indexes
            WHERE tablename = 'woow_paas_platform_cloud_service'
              AND indexname LIKE '%%provision_partner%%'
        """)
        rows = self.env.cr.fetchall()
        self.assertTrue(rows, 'partial unique index 應存在')
        self.assertIn('provision_partner_id', rows[0][0])
        self.assertIn('IS NOT NULL', rows[0][0].upper())

    def test_second_provision_row_same_partner_rejected(self):
        partner = self.env['res.partner'].create({'name': 'Dup Provision'})
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        ws1 = self.env['woow_paas_platform.workspace'].create(
            {'name': 'w1', 'owner_id': self.env.user.id})
        ws2 = self.env['woow_paas_platform.workspace'].create(
            {'name': 'w2', 'owner_id': self.env.user.id})
        self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': ws1.id, 'template_id': tpl.id, 'name': 'ha1',
            'reference_id': 'r1', 'state': 'running', 'subdomain': 's1',
            'provision_partner_id': partner.id})
        from psycopg2 import IntegrityError
        with self.assertRaises(IntegrityError):
            with self.env.cr.savepoint():
                self.env['woow_paas_platform.cloud_service'].create({
                    'workspace_id': ws2.id, 'template_id': tpl.id, 'name': 'ha2',
                    'reference_id': 'r2', 'state': 'running', 'subdomain': 's2',
                    'provision_partner_id': partner.id})
```
> 落地確認 table 實名（`woow_paas_platform.cloud_service` → `woow_paas_platform_cloud_service`）。

- [ ] **Step 2: 跑測試確認失敗**

- [ ] **Step 3: 加欄位 ＋ 常數 ＋ index method**

`src/models/cloud_service.py`：
```python
    provision_partner_id = fields.Many2one(
        'res.partner',
        string='Provision Owner（每帳號限一）',
        index=True,
        help='僅平台自動開通的「每帳號限一」服務（如 HA PaaS）填值，供 partial '
             'unique index 保證同一 partner 至多一份。一般服務留空。',
    )
```
```python
# provision「每帳號限一」的 advisory-lock namespace（比照 project_project 的
# reach-lock；任意固定 int，全 module 唯一即可）。
_HA_PROVISION_LOCK_NS = 0x48415041  # 'HAPA'
```
```python
    @api.model
    def _ensure_ha_provision_unique_index(self):
        """建 partial unique index：同一 provision_partner_id 至多一列。冪等
        （IF NOT EXISTS）。不加 state 條件——排除 deleting 會放行 delete/create
        race。由 .54 post-migrate 呼叫（既有 DB），post_init 呼叫（新裝）。"""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
                woow_paas_cloud_service_provision_partner_uniq
            ON woow_paas_platform_cloud_service (provision_partner_id)
            WHERE provision_partner_id IS NOT NULL
        """)
```

- [ ] **Step 4: 建 .54 migration**

`src/migrations/18.0.1.0.54/post-migrate.py`：
```python
"""18.0.1.0.54 — #336 HA PaaS 每帳號限一 partial unique index。

新 seed（plan/product/template）走 -u data 自建、無需 migration（noupdate 只
gate 既有 xmlid 的 UPDATE）。唯一需 migration 的是 provision_partner_id 的
partial unique index（schema）。delegate 到 model method 供單測（比照 .52）。冪等。
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._ensure_ha_provision_unique_index()
```
`src/hooks.py` `post_init_hook` 加：
```python
    env['woow_paas_platform.cloud_service']._ensure_ha_provision_unique_index()
```

- [ ] **Step 5: 跑測試確認通過**

Run: `--test-tags /woow_paas_platform:TestProvisionUniqueIndex`
Expected: PASS（3 測試綠）。

- [ ] **Step 6: Commit**

```bash
git add src/models/cloud_service.py src/migrations/18.0.1.0.54/post-migrate.py \
        src/hooks.py src/tests/test_ha_paas_provision.py
git commit -m "feat: HA PaaS 每帳號限一 provision_partner_id＋partial unique index（.54 migration）（Refs #336）"
```

---

## Task 6：provision／status 端點（契約 v1.1 §7）（工作項 2；每帳號限一應用層 工作項 5）

**Files:**
- Modify: `src/controllers/ha_paas.py`（新增 `HAPaaSController`）
- Modify: `src/controllers/__init__.py`（`from . import ha_paas`）
- Test: `src/tests/test_ha_paas_provision.py`（追加端點測試）

**Interfaces:**
- Produces：`POST /api/ha-paas/provision`、`GET /api/ha-paas/status`（`type='http'`, `auth='none'`, `csrf=False`，Bearer + `ha:provision`）。
- Consumes：`verify_oauth_token(request, required_scopes=['ha:provision'])`（`oauth2.py:82`）；`ha_api.py` 的 `_authenticate`／`_json_response`／`_json_error` pattern（`auth='none'` + werkzeug→JSON）；`cloud_service.action_redeploy`（`:247`）／deploy cron；`_create_and_confirm_subscription`（mixin:108）；`_HA_PROVISION_LOCK_NS`、`provision_partner_id`（Task 5）；service URL builder（`paas.py:2831-2834`）；`workspace.create`（`paas.py:267`）。

> **pattern-guard 命中**：`src/controllers/*.py` → `controller-endpoint`。auth='none' + verify_oauth_token 是既有 `ha_api.py` 慣例，非繞 ACL。

**狀態機映射（契約 v1.1 枚舉；Q4 已拍板擴充直映）：**

> Q4 拍板後 status 枚舉擴為 6 值 `none/provisioning/ready/error/suspended/deleting`，`suspended`/`deleting` **直映**（不再保守塞回 error/none）。`ha_url` 仍**僅 `ready`** 給。provision 端點對 `suspended`/`deleting` 回 `409` 且 body 的 `status` 值與 status 端點對齊（維持契約一致：App 無論打哪個端點看到的 status 語意相同）。

| `cloud_service.state` | status 端點回 | provision 端點行為 |
|---|---|---|
| 無 service | `none` | 建 workspace+service+0元sub → `202 provisioning` |
| `pending`/`deploying`/`initializing`/`upgrading` | `provisioning` | `202 provisioning`（不重建）|
| `running` | `ready`（+ha_url）| `200 ready`（+ha_url）|
| `error` | `error`（+error msg）| 鎖內翻 error→pending + 觸發 cron → `202`（**不 inline redeploy**）|
| `suspended` | `suspended`（不給 ha_url）| `409 {"status":"suspended", "error":…}`（不靜默 resume；App 指引：聯繫支援）|
| `deleting` | `deleting`（不給 ha_url）| `409 {"status":"deleting", "error":…}` **不復活**：絕不對 deleting 呼叫 redeploy／不假裝 provisioning；App 指引：稍後可重新開通 |

- [ ] **Step 1: 寫端點契約 shape 測試（先失敗）**

`src/tests/test_ha_paas_provision.py` 追加（`HttpCase`，比照 `test_ha_api.py` token fixture）：
```python
import json
from odoo.tests.common import HttpCase


class TestHAPaaSProvisionEndpoint(HttpCase):
    """provision/status 端點契約 v1.1 shape 與冪等。"""

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'Prov User', 'login': 'provuser@test', 'email': 'provuser@test'})
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.token = self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'ha-provision-test-token', 'user_id': self.user.id,
            'client_id': self.client.id, 'scopes': 'ha:provision',
            # expires_at 依 model 欄位補未來時間
        })

    def _post(self, path, token='ha-provision-test-token'):
        return self.url_open(path, data='{}', method='POST',
            headers={'Authorization': 'Bearer %s' % token,
                     'Content-Type': 'application/json'})

    def _get(self, path, token='ha-provision-test-token'):
        return self.url_open(path, headers={'Authorization': 'Bearer %s' % token})

    def test_status_none_before_provision(self):
        r = self._get('/api/ha-paas/status')
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.content)
        self.assertEqual(body['status'], 'none')
        self.assertNotIn('ha_url', body)

    def test_missing_token_401(self):
        self.assertEqual(self.url_open('/api/ha-paas/status').status_code, 401)

    def test_insufficient_scope_403(self):
        self.env['woow_paas_platform.oauth_token'].create({
            'access_token': 'no-provision-scope', 'user_id': self.user.id,
            'client_id': self.client.id, 'scopes': 'workspace:read'})
        self.assertEqual(self._get('/api/ha-paas/status',
                                   token='no-provision-scope').status_code, 403)

    def test_provision_blocked_when_template_not_deployable(self):
        r = self._post('/api/ha-paas/provision')
        self.assertEqual(r.status_code, 503)
        self.assertFalse(self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)]),
            '未 deployable 時不得留下 cloud_service')

    def test_provision_idempotent_per_partner(self):
        self.env.ref('woow_paas_platform.cloud_app_ha_paas').is_active = True
        r1 = self._post('/api/ha-paas/provision')
        self.assertIn(r1.status_code, (200, 202))
        CS = self.env['woow_paas_platform.cloud_service']
        self._post('/api/ha-paas/provision')
        self.assertEqual(CS.search_count(
            [('provision_partner_id', '=', self.user.partner_id.id)]), 1,
            '第二次 provision 不得再建')

    def test_status_ready_gives_ha_url_only_when_running(self):
        self.env.ref('woow_paas_platform.cloud_app_ha_paas').is_active = True
        self._post('/api/ha-paas/provision')
        svc = self.env['woow_paas_platform.cloud_service'].search(
            [('provision_partner_id', '=', self.user.partner_id.id)], limit=1)
        svc.state = 'running'
        body = json.loads(self._get('/api/ha-paas/status').content)
        self.assertEqual(body['status'], 'ready')
        self.assertTrue(body.get('ha_url', '').startswith('https://'))
```
> 落地對照 `test_ha_api.py:16-58` token fixture（`oauth_token` 實際欄位名：`access_token`/`user_id`/`client_id`/`scopes`/expiry——grep 確認），確認 `is_access_token_valid()` 需要的 expiry 給未來時間。

- [ ] **Step 2: 跑測試確認失敗**（端點不存在 → 404）

- [ ] **Step 3: 寫 controller（provision + status）**

`src/controllers/ha_paas.py`（沿用 `ha_api.py` 的 `_authenticate`/`_json_*` 慣例）。核心結構：
```python
"""HA PaaS 開通 API（契約 v1.1 §7）。Bearer + ha:provision scope。"""
from __future__ import annotations
import json, logging
from odoo.http import Controller, Response, request, route
from .oauth2 import verify_oauth_token

_logger = logging.getLogger(__name__)
_HA_PAAS_APP_TEMPLATE = 'woow_paas_platform.cloud_app_ha_paas'


def _json(data, status=200):
    return Response(json.dumps(data), status=status, content_type='application/json')


def _auth(required_scopes):
    from werkzeug.exceptions import Unauthorized, Forbidden
    try:
        return verify_oauth_token(request, required_scopes=required_scopes)
    except Unauthorized as e:
        return _json({'error': 'Unauthorized', 'detail': str(e.description)}, 401)
    except Forbidden as e:
        return _json({'error': 'Forbidden', 'detail': str(e.description)}, 403)


class HAPaaSController(Controller):

    def _guard_partner(self, user):
        """回 partner 或 None。排除 public/system partner（多個 res.users 掛
        同一 public partner 時，若不排除會讓它們共用同一把 advisory lock →
        全站互擋）。"""
        partner = user.partner_id
        if not partner or not partner.active:
            return None
        blocked = set()
        for xmlid in ('base.public_partner', 'base.partner_root'):
            rec = request.env.ref(xmlid, raise_if_not_found=False)
            if rec:
                blocked.add(rec.id)
        if partner.id in blocked:
            return None
        return partner

    def _ha_paas_template(self):
        return request.env.ref(_HA_PAAS_APP_TEMPLATE, raise_if_not_found=False)

    def _template_deployable(self, tpl):
        return bool(tpl) and tpl.is_active \
            and not (tpl.helm_chart_name or '').startswith('TBD')

    def _find_existing(self, partner):
        return request.env['woow_paas_platform.cloud_service'].sudo().search(
            [('provision_partner_id', '=', partner.id)], limit=1)

    def _ha_url(self, svc):
        domain = request.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io')
        return 'https://%s.%s' % (svc.subdomain, domain)

    @route('/api/ha-paas/provision', type='http', auth='none',
           methods=['POST'], csrf=False)
    def provision(self, **kw):
        res = _auth(['ha:provision'])
        if isinstance(res, Response):
            return res
        user, _t = res
        partner = self._guard_partner(user)
        if not partner:
            return _json({'error': 'Forbidden', 'detail': 'No valid partner'}, 403)
        tpl = self._ha_paas_template()
        if not self._template_deployable(tpl):
            return _json({'status': 'error',
                          'error': 'HA PaaS is not available yet.'}, 503)
        from ..models.cloud_service import _HA_PROVISION_LOCK_NS
        request.env.cr.execute('SELECT pg_advisory_xact_lock(%s, %s)',
                               (_HA_PROVISION_LOCK_NS, int(partner.id)))
        existing = self._find_existing(partner)
        if existing:
            state = existing.state
            if state == 'running':
                return _json({'status': 'ready', 'ha_url': self._ha_url(existing)}, 200)
            if state == 'error':
                # 可重試：翻 error→pending + 觸發 cron（鎖內不 inline redeploy）
                existing.sudo().write({'state': 'pending', 'error_message': False})
                cron = request.env.ref(
                    'woow_paas_platform.ir_cron_deploy_cloud_services',
                    raise_if_not_found=False)
                if cron:
                    cron.sudo()._trigger()
                return _json({'status': 'provisioning'}, 202)
            if state in ('pending', 'deploying', 'initializing', 'upgrading'):
                return _json({'status': 'provisioning'}, 202)
            if state == 'deleting':
                # 刪除進行中：不復活、不假裝 provisioning（會誤導 client 以為有進度
                # 但 deleting 由另一路徑控制、cron 不會撿它）。回明確 409、status=deleting
                # （契約 v1.1 直映；Q4）。App 待其刪完後重試會走「無既有」新建路徑。
                return _json({'status': 'deleting',
                              'error': 'A previous instance is being removed. Retry shortly.'}, 409)
            if state == 'suspended':
                # 蓄意停用：不靜默 resume。回明確 409、status=suspended（契約 v1.1 直映；
                # Q4）。App 指引：聯繫支援。
                return _json({'status': 'suspended',
                              'error': 'Service is suspended. Contact support.'}, 409)
            # 未知 state 的 fail-closed fallback（不洩漏 ha_url）
            return _json({'status': 'provisioning'}, 202)
        try:
            with request.env.cr.savepoint():
                self._create_ha_paas_service(user, partner, tpl)
        except Exception:  # partial unique index 撞 → 回既有
            _logger.exception('HA PaaS provision create race')
            existing = self._find_existing(partner)
            if existing:
                return _json({'status': 'provisioning'}, 202)
            raise
        return _json({'status': 'provisioning'}, 202)

    @route('/api/ha-paas/status', type='http', auth='none',
           methods=['GET'], csrf=False)
    def status(self, **kw):
        res = _auth(['ha:provision'])
        if isinstance(res, Response):
            return res
        user, _t = res
        partner = self._guard_partner(user)
        if not partner:
            return _json({'error': 'Forbidden', 'detail': 'No valid partner'}, 403)
        svc = self._find_existing(partner)
        if not svc:
            return _json({'status': 'none'}, 200)
        mapping = {
            'pending': 'provisioning', 'deploying': 'provisioning',
            'initializing': 'provisioning', 'upgrading': 'provisioning',
            'running': 'ready', 'error': 'error',
            'suspended': 'suspended', 'deleting': 'deleting',  # 契約 v1.1 直映（Q4）
        }
        st = mapping.get(svc.state, 'provisioning')  # fail-closed default
        body = {'status': st}
        if st == 'ready':
            body['ha_url'] = self._ha_url(svc)
        elif st == 'error':
            body['error'] = svc.error_message or 'Provisioning failed'
        # suspended/deleting 只回 status 值（契約 v1.1 §7.3）；App 端據此顯示指引
        # （suspended＝聯繫支援、deleting＝稍後重試）。ha_url 僅 ready、error 僅 error。
        return _json(body, 200)
```

- [ ] **Step 4: 寫 `_create_ha_paas_service`（建 workspace+service+sub）**

`HAPaaSController` 內（比照 `paas.py:_create_service:2060` 與 `_create_workspace:267`，精簡為 HA PaaS 專用、無 user helm values）：
```python
    def _create_ha_paas_service(self, user, partner, tpl):
        import uuid
        from ..services.naming import make_namespace, make_service_subdomain
        env = request.env
        # ⚠️ auth='none' 下 request.env.user 是 public——一律用 token 的 user 當 owner
        ws = env['woow_paas_platform.workspace'].sudo().with_user(user).create({
            'name': 'HA PaaS', 'owner_id': user.id})
        reference_id = str(uuid.uuid4())
        ns_prefix = env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.namespace_prefix', 'paas-ws-')
        subdomain = make_service_subdomain(ws.slug, reference_id, 'ha')
        svc = env['woow_paas_platform.cloud_service'].sudo().create({
            'workspace_id': ws.id, 'template_id': tpl.id, 'name': 'HA PaaS',
            'reference_id': reference_id, 'state': 'pending', 'subdomain': subdomain,
            'internal_port': tpl.default_port,
            'helm_release_name': 'svc-%s' % reference_id[:8],
            'helm_namespace': make_namespace(ws.slug, prefix=ns_prefix),
            'helm_chart_version': tpl.helm_chart_version,
            'provision_partner_id': partner.id})
        svc._create_and_confirm_subscription(
            partner, env.ref('woow_paas_platform.plan_ha_paas'))
        cron = env.ref('woow_paas_platform.ir_cron_deploy_cloud_services',
                       raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return svc
```
> 落地重點：(1) workspace owner = token user（`_auth` 回傳的 user），非 `env.user`。(2) D2 不變式：此 workspace 只裝這一個 service。(3) `helm_values` 可帶 template 的 `helm_default_values`（比照 `_create_service`）。(4) `with_user(user)` 是否需要視 workspace.create 的 owner 自動 access 邏輯而定，落地核對 `workspace.create`（`workspace.py:74`）。

- [ ] **Step 5: 註冊 controller**

`src/controllers/__init__.py` 加 `from . import ha_paas`。

- [ ] **Step 6: 跑端點測試確認通過**

Run: `--test-tags /woow_paas_platform:TestHAPaaSProvisionEndpoint`
Expected: PASS。狀態機完整矩陣在 Task 7。

- [ ] **Step 7: 契約 §7 修訂 v1.1（Q4 拍板；同 PR 原子改）**

Q4 拍板擴充 status 枚舉，契約即 single source of truth，須與端點實作**同 PR 原子更新**（否則 Elmo 依舊 v1 4 值實作會與平台 6 值回應不一致）。改 `docs/reference/api/ha-app-oauth-provisioning.md`：

1. **文首變更記錄**（在文件開頭狀態 blockquote 內補一行）：
   ```
   > - **2026-07-12 契約 §7 升 v1.1**：status 枚舉由 4 值擴為 6 值（新增 `suspended`/`deleting` 直映 `cloud_service.state`）；`ha_url` 仍僅 `ready` 給。詳見 §7.3。
   ```
2. **frontmatter `status:`**（行 4 附近）：把 `contract-v1` 標記更新為 `contract-v1.1`（保留其餘敘述）。
3. **§7 標題與 blockquote**（行 261-263 附近）：`契約 v1` → `契約 v1.1`（標題與「✅ 契約 v1（…）」那行）。
4. **§7.2 `POST /provision`**：在「回應」補 409 分支（維持與 status 端點 status 值一致）：
   ```
   - `409 {"status": "suspended", "error": …}` — 服務已停用（蓄意 suspend），不自動 resume；App 引導使用者聯繫支援。
   - `409 {"status": "deleting", "error": …}` — 前一個 instance 正在刪除中，不復活；App 稍後可重新 POST 開通。
   ```
5. **§7.3 `GET /status`**：枚舉擴為 6 值並補說明：
   ```
   - **回應**：`200 {"status": "none"|"provisioning"|"ready"|"error"|"suspended"|"deleting", "ha_url": …, "error": …}`
     - `status`：`none`（尚未開通）／`provisioning`（建置中）／`ready`（就緒）／`error`（佈建失敗）／`suspended`（服務暫停，聯繫支援）／`deleting`（刪除中，稍後可重新開通）。
     - `ha_url`：**僅 `ready` 時給**。
     - `error`：**僅 `error` 時給**（`suspended`/`deleting` 不帶額外欄位，App 端據 status 值顯示對應指引）。
   ```
6. **§8 sequence diagram**：把 Note `status＝none/provisioning/ready/error（ha_url 僅 ready）` 更新為 `status＝none/provisioning/ready/error/suspended/deleting（ha_url 僅 ready）`。

> 落地時 grep 契約文件實際行號重核（本 plan 撰寫時契約在 §7.1=`:267`、§7.2=`:273`、§7.3=`:283`、§8 Note=`:335` 附近）。改完通讀 §7 全段確認 v1/v1.1 標記一致、無殘留 4 值枚舉。

- [ ] **Step 8: Commit**

```bash
git add src/controllers/ha_paas.py src/controllers/__init__.py \
        src/tests/test_ha_paas_provision.py \
        docs/reference/api/ha-app-oauth-provisioning.md
git commit -m "feat: HA PaaS 開通/狀態端點（契約 v1.1 §7；Bearer+ha:provision、冪等每帳號限一、advisory lock、status 6 值枚舉）（Refs #336）"
```

---

## Task 7：完整測試矩陣補完 ＋ 全套件回歸 ＋ 契約文件回填

**Files:**
- Modify: `src/tests/test_ha_paas_provision.py`（並發/狀態機矩陣）
- Modify: `src/tests/test_hidden_template_chain.py`（by-id 端點矩陣）
- Modify: `docs/reference/api/ha-app-oauth-provisioning.md`（§7 標記平台實作進度）

**Interfaces:** 無新 produces；收斂測試覆蓋率至設計裁決 §7 必補清單。

- [ ] **Step 1: 補並發/狀態機測試**（`test_ha_paas_provision.py`）
- 兩個並發 POST /provision → 恰一個 service（unique index backstop 回 existing 非 duplicate）。
- error→re-POST → state 翻 pending + cron 觸發、**不** inline redeploy（mock `action_redeploy` 斷言未被呼叫）。
- status 窮舉 8 個 state → 映射正確 + `ha_url` 僅 ready + fail-closed default 不洩漏 ha_url。
- `deleting` re-POST 不復活/不新建；`suspended` re-POST 不靜默 resume。
- enforcement key = partner：同 partner 兩 user token → 仍一份；同 user 兩 workspace → 仍一份。
- token.user_id scoping：user B 的 token 看不到 user A 的 HA（status 依 token user filter）。

- [ ] **Step 2: 補 hidden by-id 端點矩陣**（`test_hidden_template_chain.py`）
曝光點 9-16 各一個「hidden by-id → not found 或不含」；`api_upcoming_renewals` 排除 0 元 sub；dashboard `primary` 不被 0 元 sub 搶；generic create 拒 hidden template；2a 專屬 hidden workspace 從 dashboard workspace list/count 排除；delete→cancel→re-provision 後 cancelled 0 元 sub + 發票不漏進 billing 頁（helper state-agnostic）。

- [ ] **Step 3: 補 seed upgrade 路徑測試**
fresh install 與 upgrade（-u）路徑都須顯示 plan 已配置（sticky link 陷阱）。

- [ ] **Step 4: 跑全套件回歸**（k8s-dev-sandbox 或本地 paas-odoo image，native amd64）
```
./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init
```
Expected: 0 failed（已知 2 個 agent-runtime ns-left-set flake 非本 PR）。
> 區分 regression vs 既有 flake：先只跑本 PR 新增 test class（`--test-tags
> /woow_paas_platform:TestHAPaaSProvisionEndpoint,...`）確認全綠；再跑全套件，
> 若唯二失敗恰為既有 agent-runtime ns-left-set 兩筆（比對其 test 名）即非本
> PR 引入。leader 隔離重跑本 PR test class 佐證（比照 #327/#324 landing 慣例）。

- [ ] **Step 5: 契約文件 §7 進度回填（v1.1 枚舉修訂已在 Task 6 Step 7 完成）**
`docs/reference/api/ha-app-oauth-provisioning.md`：**v1.1 枚舉修訂已於 Task 6 Step 7 隨端點實作原子交付**（勿在此重複改枚舉／版本標）。此處只把 §7「平台端實作追 #336」的進度註記更新為「平台端已實作（Refs #336）」，並在 PR／回報知會 Elmo 契約已升 v1.1（status 6 值）。若 Task 6 Step 7 已一併完成進度註記，本 step 僅通讀確認、無額外改動。

- [ ] **Step 6: Commit ＋ 版號確認**
```bash
grep '"version"' src/__manifest__.py   # 應為 18.0.1.0.54
git add src/tests/ docs/reference/api/ha-app-oauth-provisioning.md
git commit -m "test: HA PaaS 開通/隱藏鏈完整測試矩陣＋契約 §7 進度回填（Refs #336）"
```

---

## Self-Review（對 spec 逐條）

**Spec coverage：**
- 工作項 1（ha:provision scope seed+healer）→ Task 1 ✓
- 工作項 2（provision/status 端點依 §7）→ Task 6 ✓
- 工作項 3（0 元 plan seed）→ Task 2 ✓
- 工作項 4（hidden 全鏈）→ Task 2（欄位）+ Task 3（過濾）✓
- 工作項 5（每帳號限一 enforcement；掛 image blocked 除外）→ Task 5（DB 層）+ Task 6（應用層 + 503 未備行為）✓；掛 image 明確 out-of-scope（佔位 + is_active=False）
- 工作項 6（測試：0元×#324 邊界、hidden 全鏈、限一冪等、契約 shape）→ Task 4 + Task 3 + Task 6 + Task 7 ✓
- 契約 §7 逐條（Bearer+scope、202/200、狀態機、ha_url 僅 ready、error 可重試、401/403）→ Task 6 狀態機表 ✓
- 版號 .54 → Global Constraints + Task 1 Step 5 ✓
- seed noupdate/healer 慣例 → Task 1/2 依 D5 ✓

**Placeholder scan：** 每個 code step 附實際程式碼；`TBD` 僅出現在 HA PaaS template 的 helm image 佔位（工作項 5 blocked，刻意），並在 provision 端點以 deployability 檢查閉合（未備回 503）。所有 `file:line` 標「落地核對」因環境無法複讀原始碼。

**Type consistency：** `provision_partner_id`（Task 5 定義 → Task 6 使用）；`_HA_PROVISION_LOCK_NS`（Task 5 → Task 6 import）；`is_hidden`（Task 2 → Task 3）；helper 四方法（Task 3 → 各端點）；`plan_ha_paas`/`product_ha_paas`/`cloud_app_ha_paas`/`oauth_client_ha_app` xmlid 跨 task 一致。

**已知落地風險（實作時務必處理）：**
1. **環境無法複讀原始碼** → 每步先 grep 核對行號/欄位名/簽名。
2. **F1 device consent 頁 scope 渲染**（Task 1 Step 6）→ 必須定位 device 頁模板，否則 App 授權頁只見 raw scope。
3. **auth='none' 下 env.user 是 public**（Task 6 Step 4）→ 一律用 token user 建 workspace。
4. **並發 rollback 用 savepoint**（Task 6 Step 3）→ 不可讓 IntegrityError 崩掉整個 request transaction。
5. **Open Questions Q1-Q5 已於 2026-07-12 由 Eugene 拍板**（見 Open Questions 段權威結論）：Q1=(a) 自足 seed App client、Q2=(a) by-id not-found guard、Q3=(a) filter 路線、Q4=擴充枚舉修契約（status 6 值直映、契約升 v1.1）、Q5=照 plan 壓測寫回報不做產品決策。實作直接依結論，無需再等拍板。

<!-- peer-reviewed: 2026-07-12T09:42:51Z rounds=2 verdict=approved by=miniclaude -->

<!-- peer-reviewed: 2026-07-12T11:42:35Z rounds=1 verdict=approved by=miniclaude （Q4 拍板對齊增量修訂） -->
