---
title: SA Policy Rule 子模型 + AND/OR/NOT + Bypass + Selector 擴充 + Precedence 排序 實作
category: plans
status: draft
created: 2026-06-13
updated: 2026-06-13
topic: sa-policy-rule-submodel
phase: implementation
related:
  - docs/plans/2026-Q2/2026-06-13-sa-policy-rule-submodel-design.md
  - src/models/security_access_access_policy.py
  - src/models/security_access.py
  - src/models/sh_subscription_plan_sa_quota.py
  - src/controllers/security_access.py
  - extra/paas-operator/src/models/schemas.py
  - extra/paas-operator/src/services/cloudflare.py
---

# SA Policy Rule 子模型 + AND/OR/NOT + Bypass + Selector 擴充 + Precedence 排序 實作

> 設計與決策依據見 design 雙檔的另一半。本檔只列「改哪個檔的哪一段、改成什麼、怎麼驗」。
> 依賴順序：Phase 1（model 地基）→ Phase 2（operator 契約 lock-step）→ Phase 3（controller BFF）→ Phase 4（前端）→ Phase 5（live E2E）。每個 Phase 結束都要綠才進下一個。

## Phase 列表

- [x] **Phase 1**：後端資料模型 + migration + 配額 + ACL/ir.rule — ✅ 完整模組套件 0 failed of 1222 tests（sandbox 實測）
- [x] **Phase 2**：operator 契約 + Odoo payload（include/require/exclude 三組 + bypass + 新 selector）— ✅ operator 212 passed + Odoo 受影響 class 全綠
- [x] **Phase 3**：controller BFF（三組 payload + bypass + reorder endpoint）— ✅ 完整模組套件 0 failed of 1231 tests（含 8 個新 HttpCase 守線）
- [x] **Phase 4**：前端 policy builder + 平面拖曳排序 + bypass badge — ✅ headed 瀏覽器實測（sandbox）console 零 OWL/JS error
- [ ] **Phase 5**：live stg E2E 驗證（真 CF）

> **Phase 4 改動 + headed 實測**（[[owl-template-crash-class-syntax]] 守線）：
> - `AddPolicyModal` 重建成 policy builder：3 action card（Allow/Block/**Bypass**）+ include/require/exclude 三組（每組可動態增刪 selector 列）+ 5 種 selector（含 everyone 無值 / country）。`setDecision('bypass')` 即時把 email selector 從選項移除並把既有 email 列改寫成 ip_range（前端先擋，呼應兩層 guard）。
> - `SecurityAccessDetailPage`：policy 清單改平面**可拖曳排序**（native HTML5 draggable + `onPolicyDragStart/Over/Drop` → `reorderPolicies`），bypass amber badge + decision-aware mark/icon + include/require/exclude 摘要列。
> - service 加 `reorderPolicies`；create/update 改送 `{include, require, exclude}` 三組（service 既有 `...vals` 透傳）。
> - **playwright headed E2E（sandbox，restart pod 載入 Phase 1-4 + 重生 OWL bundle）**：dashboard / Access tab / builder modal / bypass sanitize / 3 組 create 端到端（summary 顯示「10.0.0.0/8 · +1 require」證實 builder→BFF→model→to_dict round-trip）/ 拖曳 reorder 持久化 — **全程 console 僅 2 個 favicon 404，零 OWL/JS error**。
> - 過渡相容：前端已送三組，controller 的扁平 fallback 與 `Policy.create` compat shim 可開始退場（見 follow-ups.md 的 compat-shim 條）。

> **Phase 3 設計細節**：
> - `_create_policy`/`_update_policy` 接 include/require/exclude 三組（`_build_rule_commands` 把每列轉成 `(0,0,{group,rule_type,rule_value})`，`group` 由 server 端迴圈鍵設定、不取自 client → 無 mass-assignment 面），並保留**扁平 fallback**（create 走 compat shim、update 走「(5,0,0)+單 include」）讓 in-flight 前端在 Phase 4 前不中斷。sparse precedence：create 不帶 sequence 時由 model 配 max+10。
> - **reorder action**：`{action:'reorder', policy_ids:[...]}`，逐筆 `_sa_policy` SA-pin（無 sudo、跨 SA id 靜默跳過、絕不寫他人 SA），序號用「成功 pin 計數×10」（跳過的 foreign id 不留空號），per-write sync 抑制 + 結尾單次 route re-sync。
> - **`sa_rule_replace` context**：`_update_policy` 的整組 replace 用 delete-before-create（(5,0,0)+creates）讓 email 配額只算新 rules；過渡空狀態會誤觸 rule-level「≥1 include」檢查，故 `rule.unlink` 在此 context 下抑制 rule-level 檢查——**policy-level `@api.constrains('rule_ids')` 仍在 write 後守最終態**（無 include 的 replace 被擋，已測 `test_owner_update_replace_without_include_rejected`）。
> - **email/bypass 驗證**：不在 controller 重複，由 model constraint 單點強制，create 的 try/except 把 ValidationError 透成 structured error（避免 two-layer divergence；已測 bypass×email、email>cap 經 BFF 皆 structured error）。
> - **手動 security review**（Phase 3 workflow 腳本 parse 失敗，改逐項手核）：mass-assignment / 跨租戶 pin / invariant-escape 三面皆 clean，invariant-escape 補測試上鎖。

> **Phase 1/2 接縫修正（2026-06-13 對抗式審查抓到的 critical）**：Odoo 的 `everyone` selector 是空 `Char`，ORM 讀回為 Python `False` → 序列化成 JSON `false` → 被 operator `AccessRuleSpec.rule_value: Optional[str]` 拒絕（非字串）→ 整個 `/api/access/sync` 422。mock 測試各自綠（operator 省略 rule_value 走 None、Odoo 只驗 ORM），無人跑「Odoo 實際輸出 → operator schema」這條真 payload。修：① Odoo `_rules_by_group` 出口 `r.rule_value or None`（security_access_access_policy.py）② operator `AccessRuleSpec` `@field_validator(mode="before")` falsy→None（defense in depth）。補三條端到端守線測試（Odoo payload 正規化 / operator schema 容錯 / endpoint mode="json" 序列化）。`bypass + everyone` 是最常見組合,此 bug 等於 bypass 政策上線即同步失敗。
>
> 註：`_build_access_payload` 的三組化（原列 Phase 2.3）實際在 **Phase 1** 完成（它是 Odoo 端、Phase 1 mock 測試驗證其 shape），Phase 2 只動 operator 端契約 + 上述接縫修正。

---

## Phase 1 — 後端資料模型 + migration + 配額 + ACL/ir.rule

**目標**：rule 子模型落地、policy 變容器、decision 加 bypass、四個 constraint、plan 配額 + seed、ACL/ir.rule 鏡像、migration 把既有扁平 policy 搬成 include rule。結束標準：`-u woow_paas_platform` 綠 + 後端 model/ACL/migration 測試綠。

### 1.1 新 rule 子模型

新檔 `src/models/security_access_access_policy_rule.py`：

- `_name = 'woow_paas_platform.security_access_access_policy_rule'`，`_order = 'sequence, id'`。
- 欄位：
  - `policy_id` M2O → `...security_access_access_policy`，required, ondelete='cascade', index。
  - `security_access_id` M2O → `...security_access`，`related='policy_id.security_access_id'`, store=True, index（給 ir.rule walk + email quota）。
  - `group` Selection `[('include','Include'),('require','Require'),('exclude','Exclude')]`, default='include', required。
  - `rule_type` Selection `[('email_domain'),('email_list'),('ip_range'),('everyone'),('country')]`, default='email_domain', required。
  - `rule_value` Char, **required=False**（everyone 無值）。
  - `sequence` Integer, default=10。
- `_DOMAIN_RE`（從 `security_access_access_policy.py:7` 搬來）。
- `_check_rule_value`（@api.constrains，從 policy `:65-85` 搬來並擴）：
  - email_domain / email_list / ip_range：維持原驗證。
  - everyone：`rule_value` 須為空（有值則 raise，避免誤填）。
  - country：split `,` 後每段為 2 碼 ISO Alpha-2（`^[A-Za-z]{2}$`）。
- CRUD override（create/write/unlink）：包 `with self.env.cr.savepoint()`，觸發 `policy_id.route_id._sync_access_to_operator()`（對齊 policy `:144-189` 原子性註解）；`sa_skip_route_sync` context 旁路。
- `to_dict()`：`{id, group, rule_type, rule_value, sequence}`。
- `src/models/__init__.py`：在 `:19 from . import security_access_access_policy` 之後加 `from . import security_access_access_policy_rule`。

### 1.2 policy 模型改造（`src/models/security_access_access_policy.py`）

- **移除** `rule_type`（`:50-57`）、`rule_value`（`:58-61`）。
- **新增** `rule_ids = fields.One2many('...security_access_access_policy_rule', inverse_name='policy_id')`。
- `decision`（`:46`）：`[('allow','Allow'),('deny','Block'),('bypass','Bypass')]`；help 移除「Block rules win over allow」，改為「Evaluated by precedence top-to-bottom, first match wins; Bypass/Service Auth are evaluated first (Cloudflare tiering).」
- `sequence`（`:62`）：default 改為「同 route 內 max(sequence)+10」（用 `default=` callable 或 create override 計算；無既有則 10）。
- `_check_rule_value`（`:65-85`）：刪除（已下放到 rule 子模型）。
- **新增** `_check_at_least_one_include`（@api.constrains on `rule_ids`，並由 rule 子模型 CRUD 觸發 policy 重檢）：policy 無任何 `group=='include'` 的 rule → ValidationError。
- **新增** `_check_decision_selector_compat`（@api.constrains）：`decision=='bypass'` 且 `rule_ids` 含 `rule_type in ('email_domain','email_list')` → ValidationError（bypass 不支援 identity selector）。
- **新增** `_check_emails_per_policy`（@api.constrains）：統計 policy 下所有 `email_list` rule 的 email 總數（split `,`），> `sa.subscription_id.sh_subscription_plan_id.sa_max_emails_per_policy` → raise；無 sub/plan 放行；旁路掛 `cr.sa_skip_policy_quota`（cursor flag，非 context）。`subscription_id` 讀用 `sa.sudo()`（對齊 `:115`）。
- `write` resync 白名單（`:160-162`）：移除 `'rule_type','rule_value'`，保留 `'name','decision','sequence','route_id'`（rule 變動由 rule 子模型自身 CRUD 觸發 resync）。
- `to_dict()`（`:191-197`）：移除 rule_type/rule_value，改吐 `rule_ids` 依 group 聚合的 `{include:[...], require:[...], exclude:[...]}` + decision(含 bypass) + sequence。

### 1.3 plan 配額（`src/models/sh_subscription_plan_sa_quota.py`）

- `:37` 後新增 `sa_max_emails_per_policy = fields.Integer(default=20, help='Security Access: max emails per access policy (email_list rules). 0 = unlimited.')`（只加欄位，守 [[odoo-std-field-no-override]]）。

### 1.4 seed（`src/data/subscription_products.xml`）

- 4 plan 各加一行 `<field name="sa_max_emails_per_policy">20</field>`：entry_monthly（接 `:217` sa_max_email_rules 後）、entry_yearly（`:238` 後）、pro_monthly（`:259` 後）、pro_yearly（`:280` 後）。
- 確認該檔 / 鄰近 record 無 `noupdate="1"`（[[seed-noupdate-sticky-template-links]]）；若 sticky，Phase 1 migration 直接 write plan 欄位翻正既有 DB，不只靠 seed `-u`。

### 1.5 ACL + ir.rule（鏡像 policy）

- `src/security/ir.model.access.csv`：`:15` 後加 2 列——`access_security_access_access_policy_rule_user`,`model_woow_paas_platform_security_access_access_policy_rule`,`base.group_user`,1,1,1,1 / `_admin`,...,`base.group_system`,1,1,1,1。
- `src/security/ir_rules.xml`：`:253`（policy admin_full）後加 3 條對稱 rule，walk 經 stored related `security_access_id`：
  - member read：domain `[('security_access_id.workspace_id.access_ids.user_id','=',user.id)]`，group_user，僅 perm_read。
  - owner/admin manage：domain `[('security_access_id.workspace_id.access_ids','any',[('user_id','=',user.id),('role','in',['owner','admin'])])]`，group_user，read/write/create/unlink 全 True。
  - admin_full：`[(1,'=',1)]`，group_system 全 True。

### 1.6 manifest + migration

- `src/__manifest__.py:3`：`'18.0.1.0.38'` → `'18.0.1.0.39'`。
- 新檔 `src/migrations/18.0.1.0.39/post-migrate.py`（`def migrate(cr, version):`，`api.Environment(cr, SUPERUSER_ID, {})`）：
  1. raw SQL + `information_schema` guard 讀既有 policy 的 orphan `rule_type`/`rule_value` 欄。
  2. `try: cr.sa_skip_policy_quota = True` 包住 → 逐 policy create 一條 rule（`policy_id`, `group='include'`, `rule_type`=舊值, `rule_value`=舊值），帶 `with_context(sa_skip_route_sync=True)`；`env.flush_all()` 在 flag 仍 set 時呼叫；`finally: cr.sa_skip_policy_quota = False`。
  3. idempotent：已有 rule 的 policy 跳過。
  4.（可選）migration 末尾 drop orphan `rule_type`/`rule_value` 欄（仿 `.38` drop is_primary，column-existence guard）。
  5. 若 1.4 偵測到 seed sticky：補 write 4 plan 的 `sa_max_emails_per_policy=20`。

### 1.7 後端測試

- `src/tests/test_security_access_access_policy.py`：
  - 加 test helper `_mk_policy(route, decision='allow', rules=[('include','email_domain','acme.com')])`，用 `rule_ids: [(0,0,{...})]`，全 class setUp/body 改走它（D6）。
  - `test_policy_requires_at_least_one_include`：全 require/exclude → ValidationError。
  - `test_bypass_decision_rejects_email_selector`：bypass + email_domain/email_list → ValidationError；對稱正向 allow/deny+email OK。
  - `test_email_list_count_quota_enforced`：plan=20，21 email → ValidationError，20 OK；無 sub 放行。
  - `test_rule_group_aggregation`：policy 含 include×2 + require×1 + exclude×1 → `to_dict()` 三組正確分組。
  - `test_precedence_per_policy_sparse`：新增多 policy 不撞號（max+10）。
  - rule 子模型格式驗證 test（everyone 無值 / country ISO）移到 rule 層 test。
- `src/tests/test_security_access_p2_5_migration.py`（或新 39 migration test）：`test_migration_flat_policy_becomes_include_rule`：legacy flat policy → 升級後 policy 仍在 + 自動生成 1 條 `group='include'` rule、decision 保留。
- ACL：`TestAccessPolicyAcl` 補 rule 子模型的 cross-workspace negative（member read-only、non-member 不可讀、role='user' 不可 create/write/unlink rule、owner 可 mutate）。

### Phase 1 驗證

- [ ] `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform` 綠（model + ACL + migration test）。
- [ ] 「先裝 `18.0.1.0.38` 後升 `.39`」DB：既有扁平 policy 確實多出一條 include rule、decision 不變、無孤兒空殼 policy。
- [ ] `-u` 後 `ir.model.access` 有新子模型 2 列、`ir.rule` 有 3 條對稱、4 plan `sa_max_emails_per_policy=20`。

---

## Phase 2 — operator 契約 + Odoo payload（三組 + bypass + 新 selector）

**目標**：Odoo ↔ operator 契約 lock-step 升級成 include/require/exclude 三陣列 + bypass + everyone/country。結束標準：operator unit test 綠 + Odoo `_build_access_payload` test 綠。

### 2.1 operator schema（`extra/paas-operator/src/models/schemas.py`）

- 新增 `class AccessRuleSpec(BaseModel)`：`rule_type: str`、`rule_value: Optional[str] = None`。
- 新增 `class AccessDecision(str, Enum)`：`ALLOW='allow'`、`DENY='deny'`、`BYPASS='bypass'`（沿用檔內既有 `(str, Enum)` pattern）。
- `AccessPolicySpec`（`:486-498`）：移除扁平 `rule_type`/`rule_value`，改 `decision: AccessDecision`、`include: List[AccessRuleSpec] = Field(default_factory=list)`、`require: List[AccessRuleSpec] = Field(default_factory=list)`、`exclude: List[AccessRuleSpec] = Field(default_factory=list)`，保留 `name`、`precedence`。
- validator：(a) include 至少 1 筆；(b) decision=bypass 且 include/require/exclude 任一含 email_domain/email_list → raise（defense in depth）。

### 2.2 operator service（`extra/paas-operator/src/services/cloudflare.py`）

- `_build_access_rule`（`:1418-1428`）：重構成「單筆 rule dict mapper」，新增分支：
  - everyone → `{"everyone": {}}`
  - country → `{"geo": {"country_code": v.strip().upper()}}`（可逗號多國 → 多 dict）
  - email_domain / email_list / ip_range 維持。
- `sync_access_app`（`:1318-1326`）：把只組 include 改成分別對 `p["include"]`/`p["require"]`/`p["exclude"]` 各跑 `_build_access_rule` 得三組 list。
- `create_reusable_policy` 呼叫（`:1321-1325`）：補傳 `include=..., require=..., exclude=...`（簽名 `:1155-1156` + payload `:1163-1164` 已預留，service 內部不動）。
- precedence（`:1326` / `:1341-1342`）：shape 不變（已對齊 CF）；組 app_payload 前對 `new_policy_ids` 依 precedence 排序 `sorted(key=lambda t: t[1])`，確保宣告序與 CF 一致。**不做 decision tier-sort**（CF 自分層，見 design D1）。
- **保留 detach-before-delete + reclaim/orphaned**（`:1302-1416`）不變（[[two-stage-remote-provision-idempotency]]）。
- `api/access.py:71` `[p.model_dump() for p in request.policies]` 不動（新欄位自動流入；確認 Enum dump 成 str）。

### 2.3 Odoo payload（`src/models/security_access.py`）

- `_build_access_payload`（`:1208-1213`）：每 policy 從 `{name,decision,rule_type,rule_value,precedence}` 改成 `{name, decision, precedence: p.sequence, include:[...], require:[...], exclude:[...]}`，三組從 `p.rule_ids` 依 group 聚合，每元素 `{rule_type, rule_value}`。sorted key 仍 `(sequence, id)`（`:1212`）。
- `_sync_access_to_operator` 回寫（`:1265-1271`）：確認 operator 仍 per-policy 回一個 cf_policy_id（1 policy↔1 reusable policy），zip 不錯位。

### 2.4 operator 測試（`extra/paas-operator/tests/test_access_sync.py`）

- `TestReusablePolicyCrud`：補 exclude/require body 正確的 test（現只測 include）。
- `TestBuildAccessRule`：新增 `test_everyone`/`test_country`，斷言 CF shape（`{"everyone":{}}` / `{"geo":{"country_code":...}}`）。
- `TestSyncAccessApp`：補三組聚合 test（一 policy 多 rule → create_reusable_policy 收到 include/require/exclude 三齊）；補 bypass decision 透傳 test。
- 既有硬編 `include==[{email_domain..}]`（`:212`）、`precedences==[1,5]`（`:234`）改 monotonic/存在性斷言（[[phase-versioned-test-assertion]]）。
- `AccessPolicySpec(...)` 建構處（`:18-21` 等）lock-step 改新 shape。

### Phase 2 驗證

- [ ] `cd extra/paas-operator && pytest tests/ -v` 綠。
- [ ] Odoo `test_build_payload_*` 綠（三組 shape）。
- [ ]（建議）`HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v` 綠。

---

## Phase 3 — controller BFF

**目標**：BFF 接三組 payload + bypass + 新 reorder endpoint + email 預檢，兩層 ACL lock-step。結束標準：controller HttpCase cross-user negative 綠。

### `src/controllers/security_access.py`

- `_create_policy`（`:784-831`）：whitelist（`:820-827`）改成 `name` + `decision`（含 bypass）+ `sequence` + 三組 rules（`include`/`require`/`exclude`，每列 `{rule_type, rule_value}`），轉成 `rule_ids: [(0,0,{group,rule_type,rule_value})...]`。route_id pin（`:790-799`）、cf_policy_id/security_access_id 排除（`:814-818`）不變。
- quota 預檢（`:806-813`）：保留 `sa_max_access_policies`；新增 email 數預檢（讀 `sa_max_emails_per_policy`，create/update 收到 email_list 時 `len(emails)<=20` fail-fast，與 model constraint lock-step）。
- `_update_policy`（`:841-857`）：whitelist（`:848`）移除 rule_type/rule_value，改 replace 整組三 rules + decision(含 bypass)。
- **新增** `action=='reorder'` 分支（`api_sa_access_policies` `:603-655`）：payload `{action:'reorder', route_id, policy_ids:[...新順序]}`，逐筆經 `_sa_policy` SA-pin（`:833-839`）→ `pol.write({'sequence': idx*10})`；owner/admin gate（`:641-643`）+ `_check_operation_allowed('write')`（`:644-647`）。**逐筆 SA-pin 防跨 SA reorder 越權**（[[orm-vs-endpoint-acl-test-coverage]]）。

### Phase 3 測試（`src/tests/test_security_access_endpoints.py`）

- non-member / role='user' 對 reorder endpoint denied（cross-user negative，structured `{success:False}` 非 crash）。
- 跨 SA reorder：A SA owner 傳 B SA 的 policy_id → 該筆被 `_sa_policy` 濾掉、不寫 B。
- bypass+email 經 BFF create → structured error（與 model constraint lock-step）。
- email>20 經 BFF create → structured error。

### Phase 3 驗證

- [ ] controller HttpCase 綠（含 cross-user / cross-SA negative）。
- [ ] ORM 綠 ≠ endpoint secure：確認 list/create/reorder 路徑無 `.sudo()` 繞 ir.rule。

---

## Phase 4 — 前端 policy builder + 平面拖曳排序 + bypass badge

**目標**：AddPolicyModal 升級成 CF 風格 builder、DetailPage policy 清單可拖曳排序、bypass badge。結束標準：headed 瀏覽器 E2E 走通、console error 零。

### 4.1 `AddPolicyModal`（`.js:39-46` state / `.xml:43-62`）

- state 從單組 `rule_type/rule_value` 改成 policy builder：`include`/`require`/`exclude` 三區塊，每區可動態增/刪多條 selector 列（每列 `rule_type` 下拉 + `rule_value` 輸入；everyone 無值輸入）。
- `ruleValueConfig`（`.js:54-68`）擴 everyone/country 的 label/placeholder/hint。
- decision：兩張 card（`.xml:25-40`）加第三張 **bypass** card；選 bypass 時 disable/隱藏 email_domain/email_list selector 選項（前端先擋，呼應兩層 guard）。
- `confirm()`（`.js:70-94`）：payload 從 `{name,decision,rule_type,rule_value}` 改成 `{name, decision, include:[...], require:[...], exclude:[...]}`；編輯模式從 `props.policy` 反序列化三組；前端驗「至少 1 include」。

### 4.2 `SecurityAccessDetailPage`（`.xml:311-333` / `.js`）

- policy 清單（per-route 內）改**平面可拖曳排序**（鏡像 CF，native HTML5 `draggable` + `dragstart`/`dragover`/`drop` handler，OWL 無框架支援），拖放後算新順序 → 呼 `reorderPolicies` → reload。
- bypass badge：decision==='bypass' 顯示第三種 badge/icon（現只有 allow 綠 `:318` / deny 灰 `:320`）。
- selector 摘要：從單一 rule_value 改成 include/require/exclude 三組摘要。

### 4.3 service（`src/static/src/paas/services/security_access.js`）

- 新增 `reorderPolicies(workspaceId, saId, routeId, policyIds)` → POST access-policies `{action:'reorder', route_id, policy_ids}`。
- `createPolicy`（`:155-167`）/`updatePolicy`（`:169-181`）vals 改新 shape（三組 + bypass）。

### Phase 4 驗證（[[owl-template-crash-class-syntax]]：必走真瀏覽器）

- [ ] `playwright-cli`（headed）走：建 policy（include 多列 + require + exclude）→ 存 → 重開編輯回填正確。
- [ ] 拖曳 reorder → 重載順序正確 → CF precedence 跟著變。
- [ ] bypass policy（配 ip_range）建立成功；bypass + email 在前端被擋。
- [ ] `agent-browser`（headed）截圖驗 builder 三區塊 + bypass badge 渲染；**console error 零容忍**。

---

## Phase 5 — live stg E2E 驗證（真 CF）

**目標**：mock 測不到的 CF selector key 正確性 / bypass×identity / 三組 / teardown 在真 Cloudflare 驗證（[[operator-cf-access-delete-202-mock-contract]]：mock 寫死狀態碼/不驗 body，唯 live 暴露）。

- [ ] `/woow:deploy-stg` 部署含本變更的 image 到 stg sandbox。
- [ ] `kubectl exec` 打 operator `PUT /api/access/sync`，走 create → resync → teardown：
  - [ ] everyone / country（geo.country_code）兩新 selector 被真 CF 接受（非 422）。
  - [ ] include/require/exclude 三組 policy 建立成功，CF 上 rule 結構正確。
  - [ ] bypass policy（ip_range）建立成功；bypass+email 在 Odoo/operator 層已擋（不送到 CF）。
  - [ ] resync detach-before-delete 無孤兒 reusable policy；teardown CF 零殘留。
- [ ] precedence：CF 上多 policy 順序與我方 sequence 一致；bypass 經 CF 分層先評估（與設計 D1 一致）。

---

## 整體驗證

- [ ] 全 5 phase 各自 checklist 綠。
- [ ] 升級路徑（先裝 `.38` 後升 `.39`）DB 上既有 policy 行為等價（扁平 → include rule）。
- [ ] 無 `.sudo()` 繞 ir.rule（controller layer）；rule 子模型 cross-workspace negative 全綠。
- [ ] CF 帳戶零殘留（teardown）；`sa_max_emails_per_policy=20` 兩層（model constraint + controller 預檢）生效。
- [ ] follow-up：若日後規模逼近 CF 帳戶級 500 reusable policies / 500 apps / 1,000 rules-per-app，補平台級計數守線（本期僅記風險，見 design「範圍邊界」）。

## 相關

- 設計：`docs/plans/2026-Q2/2026-06-13-sa-policy-rule-submodel-design.md`
- 既有 migration 範例：`src/migrations/18.0.1.0.37/post-migrate.py`（P2.5 fan-out + cursor flag）、`18.0.1.0.38/`（drop 欄位）
- operator 整合測試：`extra/paas-operator/tests/integration/README.md`
