---
title: Test Report — Decouple Project (Frontend + Controller 收尾) TDD red
category: tracking
phase: red-explained
team: decuple-project (feature agent pod)
test_explainer: decuple-project-test-explainer
plan_ref: docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
decision_ref: docs/decisions/refactor/cloud-service-project-decouple.md
created: 2026-05-28
updated: 2026-05-28
scope: D1=A + D2=A controller + frontend modal 收尾（OWL test 與 ServiceDetailPage 入口 regression 不在本輪 unit scope）
related:
  - docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/patterns/paas-workspace-member-acl-tightening.md
  - docs/archived/tracking/2026-05-20-project-detail-page-issue-48-tdd-red.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
status: archived
archived-from: docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md
archived: 2026-07-19
archived-reason: "一次性交付/稽核收口報告"
---

# Test Report — Decouple Project（Frontend + Controller 收尾）TDD red

> 這份報告**不是** plan 的逐條翻譯（`docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md` 是 source-of-truth）。
> 它解釋：**「為何只測這 2 條、其餘為何不測、紅的形狀為何 layered、若 D2 機制換掉 test 為何仍綠」**，餵給 reviewer / stakeholder 對齊測試意圖與**邊界劃分**。
>
> **本次是 cloud-service-project-decouple refactor 漏掉的收尾**：後端 model + ir.rule dual-leg 已落地（5 條 backend test 全綠 baseline），本輪補 **controller create path** 與 **frontend Modal UX**。Scope 故意窄 — 不開新 design 平面、不重做 backend 解耦。

---

## 1. TL;DR

| 項 | 值 |
|---|---|
| Plan | `docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md`（A+A+A signed-off 2026-05-28）|
| Decision lock | D1=A 允許完全 unbound · D2=A controller `sudo().create()` + `message_subscribe` follower · D3=A UI cloud_service dropdown 在無 workspace 時隱藏 |
| Red 數 | **2 條**（T1 endpoint create + follower subscribe · T2 cross-user ACL 對等）|
| Test file | `src/tests/test_project_decouple_frontend_endpoint.py`（HttpCase, `@tagged('post_install', '-at_install')`）|
| Wiring | `src/tests/__init__.py:46-47` |
| 既有 baseline | **backend 5 條 test 續綠**（`src/tests/test_project_decouple.py` AC#1 / AC#3 / AC#4 / AC#5 / AC#6） |
| 不在本輪 | OWL component test、ServiceDetailPage 預填 regression（playwright-cli）、`createUnboundProject` service caller HTTP shape |
| 失敗性質 | 全 **missing behaviour**（dispatcher guard 拒絕 + 缺 `message_subscribe`），非 fixture / import 噪訊 |

**Gate 答覆**（phase-test-explainer 自我確認）：

- **測這 2 條** = D1=A + D2=A 的「**outcome 鎖**」：endpoint 接受完全 unbound 並回 success（D1=A）+ creator 立刻看得到自己剛建的 project（D2=A）。兩條都是 **endpoint 行為層** assertion，可在 HttpCase + ORM `with_user()` 確定性驗證。
- **不測的部分** = 後端 ir.rule dual-leg 本身（已有 5 條 backend AC test 守，本輪不重做）、frontend Modal 點擊 flow（OWL test 在 plan §Test scope 列為「新增」但本輪只交付 backend HttpCase，OWL test 與 playwright-cli 驗收挪到 implementation 階段）。
- **layered red 設計**：T1 失敗在 dispatcher guard（`ai_assistant.py:1038-1040`）即拒；即使 guard 拿掉，**第二層**會踩到 `sudo().create()` 缺 `message_subscribe`（`ai_assistant.py:1228`）。T2 假設 T1 endpoint 已能跑通才有 project_id 可探 ACL，所以 T2.alice 是「**hidden trap 的可觀察後果**」（creator 看不到自家 project），T2.bob 是「**對等 deny 的 regression lock**」。

---

## 2. Scope（測什麼、不測什麼，本輪 vs 既有 baseline）

### 2.1 本輪 unit scope（2 條 test）

**File**: `src/tests/test_project_decouple_frontend_endpoint.py`
**Class**: `TestProjectDecoupleFrontendEndpoint`（HttpCase）
**Wired**: `src/tests/__init__.py:46-47`（comment: `fix/decuple-project: frontend + controller 收尾 (D1=A + D2=A — TDD red)`）

| # | Test method | 行 | 對應 plan 主題 |
|---|---|---|---|
| T1 | `test_t1_endpoint_create_unbound_project_succeeds_and_subscribes_creator` | `src/tests/test_project_decouple_frontend_endpoint.py:162-268` | §2 Controller 必改 1 + 必改 2（D1=A 移 guard + D2=A 補 `message_subscribe`） |
| T2 | `test_t2_unbound_project_visible_only_to_creator_follower` | `src/tests/test_project_decouple_frontend_endpoint.py:273-359` | §Test scope ACL bullet（unbound project 建立後非 follower 看不到） |

### 2.2 本輪**不測**（with reason）

| 未測項目 | 為何不在本輪 | 替代覆蓋路徑 |
|---|---|---|
| **OWL `CreateProjectModal` 元件 test**（不選 workspace + 不選 service → call `createUnboundProject`）| 本 repo OWL component 沒有既有 unit test 框架（Hoot 未啟用）；plan §Test scope 列為「新增」但 leader hand-off 收斂為**先交付 backend HttpCase**，OWL flow 由 playwright-cli §TC1/TC5 在 implementation 階段驗 | playwright-cli §TC1（unbound create 端到端）、§TC5（modal UI 樣態：dropdown 第一個 option 是「— No workspace — (Personal project)」、無 workspace 時 cloud_service dropdown 隱藏）|
| **`support_service.js` 新 method `createUnboundProject` HTTP shape**（call 走 `/api/support/projects` 無 path 參數）| HTTP shape 與 T1 走的 endpoint 完全一致 — T1 已守 endpoint 端 contract。Service caller 是純 forwarding layer，無獨立行為層 assertion | T1 endpoint contract（path、payload、回應 shape）+ playwright-cli §TC1 端到端 |
| **ServiceDetailPage 入口 regression**（modal 預填 workspace + cloud_service → submit 仍 work）| 既有 bound create path（`cloud_service_id` 帶值）行為**未動** — controller `_create_project` 的 `if cloud_service:` 分支保留原邏輯，無新 assertion 需求 | playwright-cli §TC3（bound create 仍 work）+ 既有 backend AC#3（`test_ac3_bound_project_workspace_id_unchanged`）|
| **「user 選 workspace 但無 service」語意**（workspace_id 不 persist）| Plan §刻意不測 第 1 條：D1=A 走「workspace 選擇實際不 persist」是有意設計；獨立決策不在本 plan 範圍 | 無 — 留 follow-up |
| **跨 workspace 移動 unbound project** | Plan §刻意不測 第 2 條：未來 feature，不在 scope | 無 |
| **backend ir.rule dual-leg domain shape**（`'\|', bound-leg, '&', unbound-clause, '\|', follower-sub-leg, creator-sub-leg`）| **已有 5 條 backend baseline 守**，本輪不重做（見 §6 Regression baseline）| `src/tests/test_project_decouple.py::TestProjectDecouple` AC#1 / AC#3 + `TestProjectUnboundACL` AC#4 / AC#5 / AC#6 |

---

## 3. 為何測這 2 條 — 機制與 outcome 對應

### 3.1 T1 — endpoint create unbound + follower subscribe

**Plan ref**: §2 Controller 必改 1（移 dispatcher guard）+ 必改 2（補 `message_subscribe`）
**Outcome 鎖**（不論 implementer 怎麼實作，T1 必須 green 的條件）：

1. `POST /api/support/projects` 帶 `action=create` + `name=...`、**完全無** `workspace_id` URL segment、**完全無** `cloud_service_id` param → JSON-RPC `result.success == True`
2. response `data.cloud_service_id` 與 `data.workspace_id` 均為 falsy
3. 持久化的 `project.project` row：`cloud_service_id == False` 且 `workspace_id == False`（compute from empty `cloud_service_id`）
4. **creator 的 `partner_id` 在 `project.message_partner_ids` 內**（follower subscribe 真實發生）

**對應的 production code 缺口**（紅在哪）：

- 第 1 缺：`src/controllers/ai_assistant.py:1038-1040`
  ```python
  if not cloud_service and not workspace:
      return {'success': False, 'error': 'cloud_service_id is required for create'}
  ```
  完全 unbound payload 在 dispatcher 層即被拒，`result.success == False` → T1 第一個 `assertTrue(result.get('success'))`（`src/tests/test_project_decouple_frontend_endpoint.py:199-208`）紅。
- 第 2 缺：`src/controllers/ai_assistant.py:1228`
  ```python
  project = request.env['project.project'].sudo().create(vals)
  ```
  `sudo()` 讓 `create_uid = SUPERUSER`，且 controller **未呼叫** `project.sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])` → T1 follower assertion（`src/tests/test_project_decouple_frontend_endpoint.py:255-268`）紅。

**為何要分這兩層**：見 §5 Layered red。

### 3.2 T2 — cross-user ACL 對等 + creator-authority outcome

**Plan ref**: §Test scope ACL bullet（unbound project 建立後非 follower 看不到，且 creator 自己看得到）
**Outcome 鎖**：

1. T1 endpoint 已能成功 create（pre-condition；T2 setup 內 `assertTrue(create_result.get('success'))` 同樣會踩 T1 第一層紅，但 assertion 訊息把根因指回 T1）
2. **Alice（creator）** `with_user(alice).search([('id', '=', project_id)])` **找得到自己** 剛建的 unbound project
3. **Bob（cross-workspace internal user，非 Alice 同 workspace、非該 project follower）** 同樣的 `search` 回**空**

**對應的 production code 缺口**（紅在哪）：

- T2.alice：與 T1 第 2 缺同源 — `create_uid = SUPERUSER` + 缺 `message_subscribe` → ir.rule `project_rule_workspace_member`（`src/security/ir_rules.xml:65-78`）unbound clause 的 **dual-sub-leg**：
  ```
  '|', 
    ('message_partner_ids', 'in', [user.partner_id.id]),  ← follower sub-leg
    ('create_uid', '=', user.id)                          ← creator sub-leg
  ```
  兩條 sub-leg 對 Alice **同時 False** → Alice 自己 `search` 也拿不到。T2.alice assertion（`src/tests/test_project_decouple_frontend_endpoint.py:332-343`）紅。
- T2.bob：本質上**今日已綠**（single-leg ir.rule 對非 follower 一律拒），但作為**對等 deny regression lock** 留在 suite — 若未來有人把 unbound clause 放寬（例如錯誤地拓寬到 `base.group_user` 任何成員），T2.bob 立刻紅。

**Bob 為何擁有自己的 workspace W_bob**（setUp `src/tests/test_project_decouple_frontend_endpoint.py:134-136`）：

> 故意讓 Bob 不是「completely access-less」的 trivial user — 否則未來有人加錯 leg「any internal user with no access record」會在 trivial-empty Bob 身上 short-circuit pass 而不會被 T2.bob 抓到。這是 **針對 false-green 的設計反射**。

---

## 4. Coverage map — plan §Affected files 與 2 條 test 的對應

Plan §Affected files 列了 5 個改動點。本輪 unit scope **直接覆蓋 2 個**（controller），其餘 3 個**由 playwright-cli 在 implementation 階段驗**：

| Plan affected file | 本輪 unit test 覆蓋？ | 對應 |
|---|---|---|
| `src/controllers/ai_assistant.py:1038-1040`（移 dispatcher guard）| ✅ T1 (199-208) + T2.alice setup (312-318) | endpoint-level success 必須先成立才能 probe 後續 contract |
| `src/controllers/ai_assistant.py:1206-1242` `_create_project`（加 `message_subscribe`）| ✅ T1 (255-268) + T2.alice (332-343) | T1 直接 assert `message_partner_ids`；T2.alice 透過 `with_user().search()` 反射出 dual-leg 對 creator 必須通過 |
| `src/static/src/paas/components/modal/CreateProjectModal.js:124-126`（移 workspaceId 必填）| ❌ 不在本輪 | playwright-cli §TC1 + §TC5 |
| `src/static/src/paas/components/modal/CreateProjectModal.xml:41-63`（加「No workspace」option）| ❌ 不在本輪 | playwright-cli §TC5 |
| `src/static/src/paas/services/support_service.js` `createUnboundProject`（新 method）| ❌ 間接（T1 守 endpoint，service caller 是 forwarding-only）| playwright-cli §TC1 |

**Why this split**（reviewer 常問「為何不一次測完 frontend」）：本 repo 目前**無 OWL component test infra**（Hoot 未啟用、既有 modal 也無對應 component test）；plan §Test scope 把 OWL test 列為「新增」是**未來方向**，本輪 leader 拍板交付 backend HttpCase 為主、frontend 由 playwright-cli 驗收。本 doc §2.2 已明列「為何不測」與替代覆蓋路徑。

---

## 5. Layered red — T1 / T2 的分工與遞進

**核心觀察**：plan §Hidden trap 指出 D2=A 的修法**必須兩處同時改**（dispatcher guard + `message_subscribe`），否則只修一處的「半 fix」會留下不同模樣的紅。Tester 把這個「雙缺口」設計成 **layered red**：

```
                       ┌───────────────────────────────────────────┐
                       │  T1.assertTrue(result.success)            │
                       │  [Layer-1] dispatcher guard 拒 → 紅       │
                       └────────────────────┬──────────────────────┘
                                            │ guard 拿掉
                                            ▼
                       ┌───────────────────────────────────────────┐
                       │  T1.assertIn(alice.partner, msg_partners) │
                       │  [Layer-2] sudo().create 缺 subscribe → 紅 │
                       └────────────────────┬──────────────────────┘
                                            │ message_subscribe 補上
                                            ▼
                       ┌───────────────────────────────────────────┐
                       │  T1 全綠 → T2 可進行 ACL probe              │
                       └────────────────────┬──────────────────────┘
                                            ▼
                       ┌───────────────────────────────────────────┐
                       │  T2.alice / T2.bob 對等驗 dual-leg outcome │
                       └───────────────────────────────────────────┘
```

**為何不合併成一條 test**：

1. 若只寫「endpoint success」單條斷言，implementer 移完 guard 就會看到綠 — 但 `message_subscribe` 沒補（hidden trap 留下），使用者 reload 後看不到自己 project（plan §Browser TC1 異常驗證明標的回歸點）。**單斷言會誤綠**。
2. 若把 follower assertion 與 endpoint 成功合併在同一 `assertTrue`（複合斷言），紅燈訊息會混淆是哪個 layer 在炸。Tester 用兩個獨立 `assertIn` / `assertTrue`，**紅燈位置 = 哪個 layer 沒修**，implementer 不需要 attach debugger 就能定位。
3. T2 把 **outcome（Alice 看得到自己）** 從 T1 的 mechanism（follower subscribe）解耦 — 即使未來換 D2=B（`with_user` 而非 sudo+subscribe），T2 仍會綠（見 §6）；但 T1 follower assertion 會 fail（因為 D2=B 機制不用 follower）。這個 split 讓「outcome 鎖」與「mechanism 鎖」**各自獨立可演進**。

**Layered red 的可讀性收益**：reviewer 讀 failure trace 時，T1 layer-1 與 T1 layer-2 的訊息分別指向 `ai_assistant.py:1038-1040` 與 `ai_assistant.py:1228` —「修一處綠一條」遞進，幫 implementer 把雙缺口的因果鏈走完一輪。

---

## 6. Outcome lock vs mechanism lock（D2=A / D2=B 切換為何 T2 仍綠）

**Plan 內 D2 候選**（plan §D2）：

- **D2=A**（已拍板）：保留 `sudo().create()`，在 create 後 `project.sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])`
- **D2=B**（被 reject）：改用 `with_user(request.env.user).create()` 讓 `create_uid = user.id`
- **D2=C**（被 reject）：改 ir.rule 加 `project_access_ids` leg

**T1 與 T2 鎖的層級不同**：

| Assertion | 鎖什麼 | D2=A 通過？ | D2=B 通過？ | D2=C 通過？ |
|---|---|---|---|---|
| T1 `assertIn(alice.partner_id, project.message_partner_ids)` (255-268) | **D2=A 的 mechanism**（follower 路徑）| ✅ | ❌（D2=B 走 create_uid leg，不 subscribe）| ❌（D2=C 走 `project_access_ids` leg）|
| T2.alice `visible_alice.ids == [project_id]` (332-343) | **outcome**（creator 看得到自己） | ✅ | ✅（dual-leg `create_uid` sub-leg pass）| ✅（D2=C 新增 leg pass）|
| T2.bob `assertFalse(visible_bob)` (349-358) | **對等 deny outcome** | ✅ | ✅ | ✅（除非 D2=C 寫錯，例如 leg 拓寬到 `base.group_user`）|

**這是 deliberate test architecture**：

- T1 是 **mechanism-coupled test**（鎖 D2=A 的 follower 機制）。若 leader 未來決定切到 D2=B，T1 follower assertion **必須跟著改**（改為 `assertEqual(project.create_uid, alice)`），這個改動會是 plan 拍板層級的契約變更，**故意不對 implementer 隱形**。
- T2 是 **mechanism-agnostic outcome test**（鎖 creator authority + cross-user deny）。不論 D2=A / B / C，**outcome 不變**：creator 自己能讀、非 follower / 非 workspace member 不能讀。

**Tester 在 docstring 已明寫此意圖**（`src/tests/test_project_decouple_frontend_endpoint.py:60-72`）：

> *Test 2 is the load-bearing creator-authority test — it verifies the outcome (creator can read own project) regardless of which mechanism establishes the authority, so even if implementation later switches D2=B the contract assertion still locks.*

**Reviewer 對照點**：審 PR 時若看到 implementer 改了 D2 機制（從 `message_subscribe` 改成 `with_user`），**T1 必須跟著改 assertion**；T2 不需動。T1 改而 T2 沒動 → PR 是合法的 mechanism swap；T2 改了 → PR 觸碰了 outcome 契約，需要 leader 重新拍板。

---

## 7. Regression baseline — 既有 5 條 backend test 守什麼，本輪不重做

**File**: `src/tests/test_project_decouple.py`
**Class scope**:

| # | Test class :: method | 行 | 守的契約 | 與本輪 2 條 test 的關係 |
|---|---|---|---|---|
| B1 | `TestProjectDecouple::test_ac1_create_unbound_project_is_allowed` | `src/tests/test_project_decouple.py:46-59` | ORM 層 `project.create({'name': ...})` 無 `cloud_service_id` 可成功；`workspace_id` 空 | T1 endpoint create 的**下游**（endpoint 不過 ORM 直 create，B1 守 ORM-level；T1 守 endpoint-level）|
| B2 | `TestProjectDecouple::test_ac3_bound_project_workspace_id_unchanged` | `src/tests/test_project_decouple.py:69-86` | bound project `workspace_id` compute chain 不變 | 本輪**不動** bound path；plan §Affected files 也只動 unbound 分支 |
| B3 | `TestProjectUnboundACL::test_ac4_unbound_project_invisible_to_non_member` | `src/tests/test_project_decouple.py:170-184` | Bob（非 member）看不到 Alice 的 unbound（ORM-level） | T2.bob 的**上游**（B3 守 ORM-level deny；T2.bob 守 endpoint create 後的 deny）|
| B4 | `TestProjectUnboundACL::test_ac5_unbound_project_visible_after_member_add` | `src/tests/test_project_decouple.py:187-198` | Bob 加為 follower 後可看 P1 | **本輪不重做**：D2=A 不會自動把任何 non-creator 加為 follower；T2 不探「加 member 後」場景 |
| B5 | `TestProjectUnboundACL::test_ac6_bound_project_visible_to_workspace_member` | `src/tests/test_project_decouple.py:201-214` | bound project 對 workspace member 可見（dual-leg bound-leg regression）| 與本輪正交 — bound path 不動 |

**本輪 unit test 為何不重做 backend 5 條**：

1. backend 5 條測的是 **ORM 層 + ir.rule** 已存在的契約，全綠且穩定。本輪改的是 controller + frontend，**不動 ORM / ir.rule**（plan §1.C 明列「後端已完整支援，無需改動」）。
2. T2 雖然在 endpoint 之後做 `with_user().search()`，但只是「**順著驗 endpoint 引發的 ACL 效應**」— 不是重新驗 ir.rule 本身。ir.rule dual-leg 的 shape 由 backend baseline 守。
3. 若有人未來改 ir.rule，**backend 5 條會先紅**（fast-fail），T1/T2 才會跟著紅。debug 順序明確。

**反過來**（本輪 2 條紅，backend 5 條應該綠）—— 如果 T1/T2 紅而 backend 5 條也紅，那不是 controller 缺 `message_subscribe`，而是 ir.rule 被誤動：先去看 backend test 的 failure，再回 controller。

---

## 8. Future-proofing — false-green 風險點與 invariants 對照

### 8.1 若有人加新 layer / 改 controller route

| 變更類型 | 風險 | T1/T2 是否能抓 | 對策 |
|---|---|---|---|
| Controller route 加新 path 給 unbound create（例如 `/api/support/projects/unbound`）| 新 path 不走原 dispatcher → T1 路徑無覆蓋 | ❌（T1 寫死 `/api/support/projects`）| **必須**為新 path 補等價 T1+T2 |
| `_create_project` 抽 helper（例如 `_create_project_unbound`）| helper 內部走舊 sudo+無 subscribe 路徑 | ⚠️ 取決於 helper 是否被 endpoint 真實 call —— T1 走 endpoint，所以 helper-wrap **安全**（只要不換 endpoint route）| 同 `2026-05-20-project-detail-page-issue-48-tdd-red.md` 對 helper-wrap 的處理：endpoint-level test 自然 cover |
| ir.rule unbound clause 拓寬（例如 `base.group_user` 通過率）| T2.bob 立即紅（Bob 是 cross-workspace user）| ✅ | T2.bob 的 setUp 故意讓 Bob 擁有自己的 W_bob，避免 trivial-empty user 短路放行（見 §3.2 末段）|
| 換掉 `sudo().create()` 為 `with_user(user).create()`（D2=A → D2=B）| T1 follower assertion 紅，但 outcome（T2）綠 | ✅（mechanism 鎖紅、outcome 鎖綠）| 是合法的 mechanism swap；需 plan-level re-sign-off（plan §D2 已標 D2=B 為 reject）|
| 換掉 dual-leg（例如砍掉 `create_uid` sub-leg）| T2.alice 在 D2=B 下會紅；D2=A 下仍綠（follower 路徑生效）| ⚠️ | **必看** [[paas-workspace-member-acl-tightening]] §Bound vs unbound 雙腿 — 砍 sub-leg 屬 ACL pattern 違反，PR 需 leader 拍板 |
| Frontend `createUnboundProject` payload 加新欄位（例如 `is_personal`）| T1 不覆蓋新欄位 | ❌ | PR 必須補 endpoint-level test |

### 8.2 Invariants 對照（reviewer 改本區前必看）

- [[cloud-service-project-decouple]] §Invariants 7 條 — plan §3 已列「本 plan 全部不衝突」，**改 controller / ir.rule 前再對一次**。特別 #5（`base.group_user` Project/Task ACL + ir.rule）是 D2=A follower 機制的 anchor：plan §3 註記「follower 本身就是 unbound leg 的合法 entry」。
- [[paas-workspace-member-acl-tightening]] — workspace-member ACL pattern。本輪 controller `message_subscribe` 動作對齊 unbound leg follower 路徑；**改 leg shape 必違反此 pattern**。
- [[constraint-input-trust-expansion]] — controller subscribe `request.env.user.partner_id` 是 server-authoritative 來源（不是 attacker-controllable kw/context），符合 pattern 的「authority 來自可信來源」原則。**若未來 implementer 試圖從 kwargs 拿 `partner_id` subscribe**，這是 input-trust expansion 風險 — 違反 pattern。
- [[cross-workspace-task-data-leak]]（B1 pitfall）— sub-model 對等 leak 的根源案例。本輪雖只動 controller create，但 follower subscribe 是 `mail.thread` 通用機制，**未來加新 sub-model 應對等檢查**對等的 ACL 是否依賴 `message_partner_ids` chain。

### 8.3 隱性假設（implementer 改 production code 時要警覺）

1. **`@tagged('post_install', '-at_install')`**（`src/tests/test_project_decouple_frontend_endpoint.py:101`）：HttpCase 在 module 完全 install 後才跑 — ir.rule、ACL 已完整套用。Implementer 若在 install 階段試圖前置呼叫 endpoint，會看到不一致行為。
2. **使用者 login suffix 用 `uuid.uuid4().hex[:6]`**（`src/tests/test_project_decouple_frontend_endpoint.py:124-130`）：故意避開 worktree dev DB 沒 wipe 導致的 login 衝突。Implementer 若想 reproduce 紅，直接跑這個 test class 即可（不需 wipe DB）。
3. **Bob 擁有自己的 workspace W_bob**（`src/tests/test_project_decouple_frontend_endpoint.py:134-136`）：不是 trivial empty user。詳見 §3.2。**改 setUp 移除 W_bob = T2.bob 退化為 trivial green，false-green 風險升高**。
4. **`_post()` helper assert `response.status_code == 200`**（`src/tests/test_project_decouple_frontend_endpoint.py:150-154`）：Odoo JSON-RPC envelope 即使 business-level error 仍回 200，所以 `result.success == False` 走 `assertTrue(result.get('success'))` 抓而非 status code。
5. **T1 用 `Project.sudo().browse(project_id)` 讀 ORM**（`src/tests/test_project_decouple_frontend_endpoint.py:229-246`）：刻意繞 ACL（ACL 在 T2 才驗），ORM-level sanity 確認 row 真的 persist + cloud_service_id/workspace_id 都 falsy。Implementer 若 endpoint 默默 fallback 給某預設 workspace，T1 ORM 層斷言會抓到。

---

## 9. Reviewer 對照點（PR diff 對照導讀）

讀 PR diff 時對照本報告：

### 9.1 `src/controllers/ai_assistant.py`

- **L1038-1040** 必須整段移除（或改成允許 `not cloud_service and not workspace`）→ 對應 T1 (199-208)、T2 setup (312-318)
- **L1206-1242** `_create_project` 在 `sudo().create(vals)`（line 1228）**之後** 必須緊接：
  ```python
  project.sudo().message_subscribe(
      partner_ids=[request.env.user.partner_id.id],
  )
  ```
  → 對應 T1 (255-268)、T2.alice (332-343)
- **L43-58** `_check_project_access` 對 unbound 的 follower-only 假設**保持**（plan §1.B 末段佐證）→ 對應 T2.alice 必須能 read（D2=A follower 機制與 `_check_project_access` follower-only 對齊）

### 9.2 `src/security/ir_rules.xml`

- **L65-78** `project_rule_workspace_member` dual-leg domain **不動** → 對應 backend baseline B3/B4/B5 + 本輪 T2.alice/T2.bob
  - 注意 ir.rule 內部用 `cloud_service_ids`（M:N，agent-runtime-pod design §7.0.2 step 3 演進），與 controller 端 `cloud_service_id`（M2O related 名）並存。本輪不動此層

### 9.3 `src/static/src/paas/components/modal/CreateProjectModal.js` + `.xml`

- **L124-126** 必填 workspaceId 檢查移除（plan §2 Frontend 必改 2）→ 本輪 unit test **不覆蓋**，由 playwright-cli §TC1 / §TC5 驗
- **XML L41-63** 加「— No workspace — (Personal project)」option（plan §2 Frontend 必改 1）→ playwright-cli §TC5

### 9.4 `src/static/src/paas/services/support_service.js`

- 新增 `createUnboundProject(data)` method 走 `POST /api/support/projects`（無 path 參數）→ **HTTP shape 等價於 T1**；service caller 是 forwarding-only，T1 已守 endpoint contract

### 9.5 `src/tests/test_project_decouple.py`（backend baseline）

- 既有 5 條 test **不動**；若 PR 改了任何 backend test，先看 plan §3 invariants 對照表（plan §1.C 明列後端不需動）

---

## 10. Phase gate 自我確認 — 為何測這些 / 為何不測那些

> 這段是 phase-test-explainer 的 self-attest。reviewer / stakeholder 讀完 §1-§9 後，這 5 句話就是 doc 的「為何測這些 / 為何不測那些」整理。

**為何測這些**：
1. T1 守 plan D1=A + D2=A 的**雙缺口同改**契約（dispatcher guard 移除 + `message_subscribe` 補上），用 layered red 把「半 fix 誤綠」風險斷掉。
2. T2 守 D2=A 引發的**creator authority outcome**（自家看得到、跨用戶看不到），且鎖**outcome** 不鎖機制 — 未來 D2 切到 B/C 機制仍綠。

**為何不測這些**：
3. backend 5 條 test 已守 ORM + ir.rule dual-leg shape（baseline 穩定），本輪不重做，避免 test 矩陣冗余 + 重覆驗證造成的 maintenance noise。
4. OWL `CreateProjectModal` + service caller HTTP shape 由 **playwright-cli §TC1/TC5** 在 implementation 階段端到端驗收（本 repo OWL component test infra 未啟用），不是「跳過」而是「分到正確的覆蓋層」。
5. bound create path（ServiceDetailPage 入口）**未動**，無新 assertion 需求；既有 AC#3 + playwright-cli §TC3 已守 regression。

---

## 11. See also

- [[2026-05-28-decouple-project-from-workspace-frontend-plan]] — 本輪 source-of-truth plan（A+A+A signed-off）
- [[cloud-service-project-decouple]] — 後端解耦 decision（7 invariants、backend 5 條 baseline 的契約來源）
- [[paas-workspace-member-acl-tightening]] — workspace-member ACL pattern（unbound leg follower 機制 anchor）
- [[constraint-input-trust-expansion]] — controller subscribe authority 來源約束
- [[cross-workspace-task-data-leak]] — B1 pitfall（sub-model 對等 leak 教訓）
- `docs/archived/tracking/2026-05-20-project-detail-page-issue-48-tdd-red.md` — 同 plan 家族的 TDD red phase doc，可對比 idiom

---

## 12. Hand-off

**Audience**:
- **Implementer**：本報告告訴你「2 條 test 對應的 invariant / outcome 是什麼」— 動 controller code 時對照 §9 reviewer 對照點 + §8.3 fixture 隱性假設。layered red 順序：先過 T1 layer-1（dispatcher guard），再過 layer-2（`message_subscribe`），再讓 T2.alice / T2.bob 對等通過。
- **Reviewer**：本報告告訴你「2 條 test 守的失敗模式 + outcome lock vs mechanism lock 的差異」。PR diff 對照本檔 §9；ACL pattern / invariants 對照 §8.2。
- **Stakeholder / future onboarding**：讀 §1 TL;DR + §2 Scope + §10 Phase gate 自我確認，3 分鐘理解本輪測什麼、為什麼這樣切。

**Reasoning gap 收尾**：

- ✅ Plan-side：D1=A + D2=A + D3=A 已 signed-off（plan §Decision Sign-off）
- ✅ Test-side：T1 layered red 涵蓋雙缺口；T2 outcome lock 防 mechanism 切換失效
- ✅ Baseline-side：backend 5 條 test 與本輪 2 條 test 職責分工明確（§6）
- ⏸ Frontend OWL + playwright-cli：plan §Browser 驗證計畫 5 條 TC 已備，但本輪不執行（implementation 階段執行）
- ⏸ Pattern doc promote（plan Open Q #4）：留 reporter 階段判斷，不阻擋

本 doc 無 outstanding gap，下一階段建議直接進 **implementation**（移 guard + 補 `message_subscribe` + frontend modal 三點）。
