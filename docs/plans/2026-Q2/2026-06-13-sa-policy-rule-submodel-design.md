---
title: SA Policy Rule 子模型 + AND/OR/NOT + Bypass + Selector 擴充 + Precedence 排序 設計
category: plans
status: draft
created: 2026-06-13
updated: 2026-06-13
topic: sa-policy-rule-submodel
phase: design
related:
  - src/models/security_access_access_policy.py
  - src/models/security_access.py
  - src/models/sh_subscription_plan_sa_quota.py
  - src/controllers/security_access.py
  - src/static/src/paas/components/modal/AddPolicyModal.js
  - src/static/src/paas/pages/security-access/SecurityAccessDetailPage.js
  - src/static/src/paas/services/security_access.js
  - extra/paas-operator/src/models/schemas.py
  - extra/paas-operator/src/services/cloudflare.py
  - src/security/ir.model.access.csv
  - src/security/ir_rules.xml
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
  - docs/decisions/patterns/two-stage-remote-provision-idempotency.md
  - docs/decisions/patterns/phase-versioned-test-assertion.md
  - docs/tracking/pitfalls/operator-cf-access-delete-202-mock-contract.md
  - docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
  - docs/tracking/pitfalls/owl-template-crash-class-syntax.md
---

# SA Policy Rule 子模型 + AND/OR/NOT + Bypass + Selector 擴充 + Precedence 排序 設計

## 背景

Security Access（SA）目前的 Access Policy 是**「1 policy = 恰好 1 條 rule」的扁平結構**，相對 Cloudflare Access 原生能力是一個極簡子集：

- **policy 模型扁平**（`src/models/security_access_access_policy.py:50-61`）：`rule_type` + `rule_value` 兩個欄位直接掛在 policy row 上，沒有 rule 子表，沒有 include/require/exclude 群組概念。
- **邏輯只有 OR**：唯一的「多值」是 `email_list` 逗號分隔（operator 展開成多條 CF include entry，`extra/paas-operator/src/services/cloudflare.py:1423-1425`）；無 NOT（exclude）、無 AND（require）。`create_reusable_policy` 簽名雖預留 `exclude`/`require` 參數（`cloudflare.py:1150-1166`），但唯一呼叫端 `sync_access_app` 只傳 `include=`（`cloudflare.py:1321-1325`），契約層 `AccessPolicySpec` 根本沒有對應欄位（`schemas.py:486-498`）。
- **decision 只有 2 種**：`[('allow','Allow'),('deny','Block')]`（`security_access_access_policy.py:45-49`），無 Bypass。model help 寫的「Block rules win over allow」是**不準確的願望式描述**——CF 實際是 first-match by precedence。
- **selector 只有 3 種**：`email_domain` / `email_list` / `ip_range`（`security_access_access_policy.py:50-57`、`cloudflare.py:1418-1428`），CF 官方有 21 種。
- **precedence 全部撞號**：`sequence` default=10（`security_access_access_policy.py:62`），controller create 也 default 10（`controllers/security_access.py:826`），而**前端從不送 sequence**（`AddPolicyModal.js:77-82` payload 只有 name/decision/rule_type/rule_value）→ 所有 policy 的 precedence 都是 10，送進 CF 互相打架，排序退化成只看 id。
- **email_list 無數量上限**：`rule_value` 是無 size 的 `Char`，`_check_rule_value`（`security_access_access_policy.py:74-78`）只驗格式不驗筆數。一條 email_list 可塞數百 email，每個展開成一條 CF rule，實質繞過 `sa_max_access_policies` 配額精神，並往 CF 帳戶級上限（reusable policies 500 / rules-per-app 1,000）逼近。

P2.5 之後 SA 的「application 等價物」是**每條 `is_protected=True` 的 Application Route**（每 route 一個獨立 CF Access app），policy 以 One2many 掛 route（`security_access.py:904-908`），但**配額是 SA 級 pooled total**（`sa_max_access_policies`，跨 SA 所有 route 計總，`security_access_access_policy.py:87-123`）。本設計在這個 P2.5 結構上往下加一層 rule 子模型，不動 route↔policy 綁定。

## 為什麼現在做（而非繼續 park Exclude/Require）

先前評估傾向「Exclude/Require 實務少用、先 park」。**本設計推翻該決定**，理由是 user 拍板的架構考量：

> rule 子模型是架構問題,後續再改成本太高;CF 既然原生提供 1 policy 多 rule,就一次做進去。

關鍵在於：「1 policy = 1 rule」→「1 policy = N rule」是**資料模型的形狀改變**，牽動 model / migration / operator 契約 / controller / 前端 / 全套測試。一旦先以扁平結構再上線一輪，日後補做要再走一次同規模的 migration + 契約破壞。而「多 Include（OR）」「Exclude（NOT）」「Require（AND）」三者**共用同一個前置改動**（rule 子表 + group 欄位），做了子表後三組只差「多一個 group 值」，邊際成本趨近零。因此一次做滿 AND/OR/NOT 是正確的切法——**簡化的邊界從「砍邏輯能力」移到「砍 selector 種類」**（先支援 5 種、其餘 16 種 CF selector 暫不做）。

## 已核准的決策

| 決策 | 結果 | 來源 |
|------|------|------|
| rule 子模型 | 引入 `security_access_access_policy_rule`（1 policy → N rules） | user（架構考量） |
| 邏輯運算子 | 一次做滿 **include(OR) / require(AND) / exclude(NOT)** 三組 | user（推翻先前 park） |
| decision | 加 **bypass**（成為 allow / deny / bypass 三值） | user |
| selector 擴充 | 加 **everyone / country**（成為 5 種）；service_token 經 user 決定移除（見下） | user |
| precedence | **鏡像 CF 平面清單**：我方存扁平 sequence、送 precedence+decision，CF 自己做 Bypass/ServiceAuth 分層；我方**不**自行 tier-sort | user（截圖確認 CF 是平面清單） |
| precedence 配號 | sequence **稀疏配號**（新增 = max+10）+ 排序 UI 寫回，解決全撞 10 | user |
| email 上限 | 新增 plan 配額 `sa_max_emails_per_policy`，**全 plan 一律 20**（非分級） | user |
| `Block wins` help | 移除，改為 first-match-by-precedence + CF 分層說明 | 本設計（事實校正） |

## 目標資料模型

```
security_access_access_policy            # policy = 一個 CF reusable policy
  ├─ route_id            (M2O → route, required, cascade)   不變
  ├─ security_access_id  (related store, route_id.security_access_id)  不變（ir.rule walk + quota）
  ├─ name                (Char, required)                   不變
  ├─ decision            (Selection: allow / deny / bypass) ← 加 bypass
  ├─ sequence            (Integer, = CF precedence)         保留，default 改 max+10
  ├─ cf_policy_id        (Char)                             不變
  ├─ rule_ids            (One2many → policy_rule)           ← 新增（取代扁平 rule_type/rule_value）
  └─ [移除] rule_type / rule_value                          ← 下放到子模型

security_access_access_policy_rule       # rule = policy 內一條 selector
  ├─ policy_id           (M2O → policy, required, cascade)
  ├─ security_access_id  (related store, policy_id.security_access_id)  ← ir.rule walk + email quota
  ├─ group               (Selection: include / require / exclude, default include)
  ├─ rule_type           (Selection: email_domain / email_list / ip_range / everyone / country)
  ├─ rule_value          (Char, NOT required)   ← everyone 無值
  └─ sequence            (Integer, default 10)  ← group 內排序（次要）
```

**precedence 軸的層級**：rule 子模型化後，precedence（評估順序）**回到 policy 級**——一個 policy = 一個 CF reusable policy = 一條 precedence。rule 的 `sequence` 只決定同一 policy 內 selector 的顯示順序，不影響 CF 評估（CF 的 include/require/exclude 內部無序，OR/AND/NOT 是集合語意）。

## CF 對照與語意（實作依據）

### include / require / exclude 三組布林語意

| 群組 | 運算子 | CF 官方語意 |
|---|---|---|
| include | **OR** | 符合任一條即通過；**每 policy 至少 1 條 include**（CF 硬規則） |
| require | **AND** | 必須同時符合所有條 |
| exclude | **NOT** | 符合任一條即被擋 |

完整布林 = `(include₁ OR include₂ …) AND (require₁ AND …) AND NOT(exclude₁ OR …)`。三組在 CF API 都是「AccessRule 物件陣列」，**結構完全相同**（同一組 selector JSON 三欄通用）。

### decision 分層評估（precedence 設計的核心）

CF 官方（https://developers.cloudflare.com/cloudflare-one/access-controls/policies/）：

> Bypass and Service Auth policies are evaluated **first**, from top to bottom as shown in the UI. **Then**, Block and Allow policies are evaluated based on their order from top to bottom. Once a user matches an Allow or Block policy, evaluation stops.

→ **兩 tier**：Tier 1 = Bypass + Service Auth（依 precedence），Tier 2 = Block + Allow（依 precedence、首中即停）。**重點：分層是 CF 引擎自己做的**，我方只送 `precedence` + `decision`，CF 依 decision 歸 tier。因此：

- **我方不需要、也不該自己 tier-sort**。`_build_access_payload` 依 `(sequence, id)` 平面排序送出即可（`security_access.py:1212`），CF 端自動把 bypass 提前。
- UI 鏡像 CF 的**平面可拖曳清單**（不分組）。「Bypass 排在後面卻仍先生效」是 CF 本身的行為，鏡像 CF 即與使用者在 CF 看到的一致。
- `Block wins over allow` 不成立：Tier 2 內 Block/Allow 依位置首中即停，不是 Block 恆贏。help 文字校正。

### selector → CF JSON 映射

| 我方 rule_type | CF JSON（單筆 rule dict） | 備註 |
|---|---|---|
| email_domain | `{"email_domain": {"domain": v.lstrip('@').strip()}}` | 不變 |
| email_list | `{"email": {"email": e}}`（逗號展開多筆） | 不變；受 `sa_max_emails_per_policy` 上限 |
| ip_range | `{"ip": {"ip": v.strip()}}` | 不變 |
| everyone | `{"everyone": {}}` | **新增**，rule_value 空 |
| country | `{"geo": {"country_code": c.upper()}}` | **新增**，ISO 3166-1 Alpha-2；可逗號多國 |

decision 合法值在 CF API 為 `allow` / `deny` / `non_identity`（=UI Service Auth）/ `bypass`。**本案只開 `bypass`，不開 `non_identity`**（Service Auth 不在 user 需求內）。

### Bypass × identity-selector 限制（必須兩層守）

CF 官方明文：「Bypass policies do not support identity-based rule types … such as email」。因此 **decision=bypass 的 policy 不可含 `email_domain` / `email_list`**（identity selector），只能配 `ip_range` / `everyone` / `country`。CF server-side 會擋，但**不可把錯誤推到 CF 才炸**——須在 Odoo `@api.constrains`（layer 1）+ operator schema validation（layer 2，defense in depth）兩層 fail-fast。

### 帳戶級硬上限（記入風險，本期不主動守）

CF default account limits（Enterprise 可調升）：Rules per application **1,000**、Reusable policies per account **500**、Applications per account **500**。多租戶共用單一 CF 帳戶下，SA 數 × 每 SA policy 數會往 500 reusable policies 逼近。`sa_max_emails_per_policy=20` 順帶壓低單 policy rule 數。**平台級計數守線不在本期範圍**，僅記為已知風險。

## 關鍵設計決策與理由

### D1 — precedence：鏡像 CF，不 tier-sort，稀疏配號

CF 自己分層，我方做 tier-sort 反而會與 CF 行為打架。實作只需三件：(a) `sequence` default 從固定 10 改為「同 route 內現有 max sequence + 10」，新增不撞號；(b) 前端把 sequence 寫回（現在完全不送）；(c) 新增 reorder endpoint，拖曳後批次重寫 sequence（10/20/30…稀疏間隔，留插入空間）。`_build_access_payload` 與 `_sync_access_to_operator` 回寫 `cf_policy_id` 的 `(sequence, id)` 排序鍵必須兩處同序（`security_access.py:1212` / `:1267`），配號分散後仍安全（1 policy↔1 reusable policy，zip 不錯位）。

### D2 — Bypass×identity 兩層 guard（非 ir.rule）

這是輸入合法性，不是可見性，故用 `@api.constrains`（Odoo）+ pydantic validator（operator），不是 ir.rule。Odoo 層擋住正常路徑，operator 層擋「繞過 Odoo 直打 /api/access/sync」的縫（現況 `decision` 是純 str 無 enum，`schemas.py:490`）。

### D3 — email 上限落在 rule 層 + cursor-flag 對稱

子模型化後 email_list 在 rule 子表，故 `_check_emails_per_policy` 寫在 rule 子模型（或 policy 層跨讀 rule_ids），計該 policy 下所有 `email_list` rule 的 email 總數 ≤ `plan.sa_max_emails_per_policy`。**無 subscription / 無 plan 則放行**（grandfather，對齊 `_check_policy_quota:116`）。**0 的語意**：因 4 plan 一律 seed 20，0 只會出現在無 plan 情境＝不限；若 plan 顯式給 0，視為「不限」（0 封 email 無意義，與其他 quota 的「0=禁止」相反，需在 help 註明）。constraint 旁路須掛 **cursor flag**（如 `sa_skip_policy_quota`）非 context——因 stored related recompute 在 context-stripped env 跑（`security_access_access_policy.py:103-112`），否則 migration 拆 rule 時被新上限誤擋。

### D4 — ACL / ir.rule 鏡像（two-layer lock-step）

rule 子表持有 email/ip selector 等敏感資料，是「比 route 更敏感的 CF Access gate 的一部分」（`ir_rules.xml:189-191` 已強調 policy 的敏感性）。新子表必須：(a) `ir.model.access.csv` 加 2 列鏡像 policy（user 1,1,1,1 / admin 1,1,1,1）；(b) `ir_rules.xml` 加 3 條對稱 rule（member read-only / owner-admin manage / group_system full），walk 經 stored related `security_access_id`（= `policy_id.security_access_id`，再走 `workspace_id.access_ids`）。否則 `role='user'` 可經 backend ORM 直接 create/edit/delete rule，繞過 BFF role gate（`controllers/security_access.py:642`）+ subscription `_check_operation_allowed`＝跨 workspace data leak（[[two-layer-acl-lockstep]] / [[paas-workspace-member-acl-tightening]]）。BFF（layer 2）reorder/create/update endpoint 任一改 domain，ir.rule（layer 1）須同步。

### D5 — migration：扁平 → include rule（post-migrate + cursor flag）

新 `src/migrations/18.0.1.0.39/post-migrate.py`（manifest 38→39）。schema 更新先於 post-migrate 執行，故 rule 子表此時已建。逐 policy 把舊 `(rule_type, rule_value)` 搬成一條 `group='include'` 子 rule、`decision` 保留。舊欄位移出 ORM 後成 orphan column，用 **raw SQL + information_schema column-existence guard** 讀（仿 `18.0.1.0.37` / `.38` 慣例）。create rule 會觸發 `_check_policy_quota` / 新 email 上限 + stored related deferred recompute（context-stripped），故須 `cr.sa_skip_policy_quota = True ... finally: = False` + `env.flush_all()`（在 flag 仍 set 時 flush，仿 `.37:203-227`），並帶 `with_context(sa_skip_route_sync=True)` 不打 operator。prod no-op（無 SA 資料）、idempotent（已有 rule 的 policy 跳過）。**fresh install 綠 ≠ 升級路徑綠**——須建「先裝舊版後升級」DB 驗子 rule 真的搬到（[[seed-noupdate-sticky-template-links]] 同類教訓）。

### D6 — 測試 forward-coupling 控制

`rule_type`/`rule_value` 從 policy 移到子表後，約 20+ 處 `Policy.create({...'rule_type','rule_value'...})`（遍佈 `test_security_access_access_policy.py` 5 個 class + `test_security_access_endpoints.py` + `test_security_access_p2_5_migration.py`）會整批紅。**對策**：建一個 test helper（如 `_mk_policy(route, decision='allow', rules=[('include','email_domain','acme.com')])`）集中 churn，內部用 idiomatic One2many command（`rule_ids: [(0, 0, {...})]`）。**不**在 model `create()` 加「magic 反向展開 convenience kwarg」——那會遮蔽新結構、違反顯式原則。

## 範圍邊界（本期不做）

- **Service Auth（non_identity decision）** — user 只要 bypass。
- **其餘 16 種 CF selector**（Service Token / Any Access Service Token / External Evaluation / Common Name / Valid Certificate / User Risk Score / Linked App Token / Login Methods〔policy 層〕/ Authentication Method / IdP group / SAML Group / OIDC Claim / Device posture / Warp / Gateway / CF Account Member）— 簡化保留在「selector 種類」維度。**Service Token 原列入擴充、經 user 決定移除**（理由：token_id 需自由輸入或下拉選 CF 帳戶級 service token，對目標用戶門檻過高、不會用到）。
- **Rule groups / Access groups 引用** — 不做可重用 rule 群組。
- **平台級 CF 帳戶上限（500/500/1000）計數守線** — 僅記風險。
- **policy 跨 app 重用** — 維持現況「每次 sync 刪舊建新、policy id 每次換新」（detach-before-delete，`cloudflare.py:1302-1416`），不改為 CF reusable 一改全傳播。

## 風險與 pitfall 對照

| 風險 | 對照 | 守線 |
|---|---|---|
| operator mock 寫死狀態碼/shape，新 selector key 錯誤只有 live CF 才暴露 | [[operator-cf-access-delete-202-mock-contract]] | Phase 5 live stg E2E（`kubectl exec` 打 `PUT /api/access/sync` 走 create→resync→teardown），驗 everyone/country/bypass/三組 |
| mock 測試硬編 `include==[{email_domain..}]`、`precedences==[1,5]` | [[phase-versioned-test-assertion]] | 改 monotonic / 存在性斷言，勿續硬編 shape |
| migration fan-out `copy()` 不帶 One2many 子 rule → 空殼 policy | D5 / [[seed-noupdate-sticky-template-links]] | 升級路徑 DB 驗證 + 子 rule 顯式處理 |
| OWL builder（動態增刪列、3 區塊、第三張 card）+ 拖曳語法錯 → runtime 白屏，Python 測試/parse 攔不到 | [[owl-template-crash-class-syntax]] | headed 瀏覽器實測 + console error 零容忍；drag 用 native HTML5（OWL 無框架支援） |
| rule_type/rule_value 移位致 20+ test forward-coupled red | D6 | test helper 集中 churn |
| detach-before-delete 順序被三組化打破 → in-use 拒刪孤兒 | [[two-stage-remote-provision-idempotency]] | 保留 create-new→put-app→delete-old 順序 + reclaim/orphaned 回報涵蓋新 selector |
| bypass×email 送到 CF 才被拒 | D2 | Odoo + operator 兩層 fail-fast |

## 待確認決策（實作前可再敲）

1. **Bypass 是否自動釘在清單頂**：預設**鏡像 CF（不釘、放任拖曳）**。要更貼心可在我方 UI 把 bypass 自動置頂避免「排了不生效」困惑——但會偏離 CF。**預設不釘**。
2. **`sa_max_emails_per_policy` 計數範圍**：算「該 policy 下所有 `email_list` rule 的 email 總數」還是「單條 rule 的 email 數」？預設**整 policy 加總**（更貼近「policy 配額」精神）。

## 相關

- 實作步驟：`docs/plans/2026-Q2/2026-06-13-sa-policy-rule-submodel-implementation.md`
- P2.5 per-route 架構：`src/migrations/18.0.1.0.37/post-migrate.py`、`docs/decisions/refactor/cloud-service-project-decouple.md`
- pattern：[[two-layer-acl-lockstep]]、[[orm-vs-endpoint-acl-test-coverage]]、[[two-stage-remote-provision-idempotency]]、[[phase-versioned-test-assertion]]
- pitfall：[[operator-cf-access-delete-202-mock-contract]]、[[seed-noupdate-sticky-template-links]]、[[owl-template-crash-class-syntax]]
