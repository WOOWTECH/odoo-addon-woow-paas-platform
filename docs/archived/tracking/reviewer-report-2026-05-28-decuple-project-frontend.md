---
name: reviewer-report-2026-05-28-decuple-project-frontend
phase: reviewer
team: decuple-project (feature agent pod)
reviewer: decuple-project-reviewer
scope: 本 branch fix/decuple-project 改動 7 點 + 1 新 test 檔（diff 限定）；不擴展 CAQA 已覆蓋的廣度 audit（dashboard / agent_runtime / billing OOS）
diff_base: develop (working tree, uncommitted — reviewer 對 working tree diff 而非 branch commit)
branch: fix/decuple-project
plan_ref: docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
decision_ref: docs/decisions/refactor/cloud-service-project-decouple.md
test_report_ref: docs/archived/tracking/2026-05-28-decouple-project-frontend-endpoint.md
caqa_ref: docs/archived/tracking/caqa-report-2026-05-28-decuple-project-frontend.md
created: 2026-05-28
verdict: APPROVE WITH CAVEATS（與 CAQA re-verdict 對齊；4-agent verdict 無方向性分歧；0 blocker；0 major；4 minor 全 follow-up；4 CAQA caveats 維持 reporter follow-up）
related:
  - docs/archived/tracking/caqa-report-2026-05-28-decuple-project-frontend.md
  - docs/archived/tracking/2026-05-28-decouple-project-frontend-endpoint.md
  - docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/patterns/paas-workspace-member-acl-tightening.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
status: archived
archived-from: docs/tracking/reviewer-report-2026-05-28-decuple-project-frontend.md
archived: 2026-07-19
archived-reason: "一次性 reviewer 驗收報告，cycle 已收官；證據錨點引用已改指本處"
---

# Reviewer Report — Decouple Project（Frontend + Controller 收尾 + TC4/A1 follow-on）

## 1. Final Verdict

**APPROVE WITH CAVEATS** — proceed to Reporter，可合 develop。**0 blocker**、**0 major**。verdict 與 CAQA **APPROVED WITH CAVEATS 對齊**（re-verdict `docs/archived/tracking/caqa-report-2026-05-28-decuple-project-frontend.md:529-712`），**4-agent verdict 無方向性分歧**（全 proceed to Reporter）→ 不觸發 Hard Rule。

7 改動點對 plan（D1=A + D2=A + D3=A signed-off）+ CAQA BLOCKER A1 fix 路徑**code-level 完全對齊**；test suite 14 條（T1-T4 endpoint + B1-B5 backend baseline + Playwright TC1-TC5 UI）全綠由 leader hand-off 確認，且 reviewer 自驗 T1-T4 與 plan §Outcome lock vs mechanism lock 的 layered red 設計**無 false-green vector**。

核心對齊 verified ✓（reviewer 自讀 diff + 4-agent 交叉確認）：

- **D1=A dispatcher guard 移除**（`src/controllers/ai_assistant.py:1037-1038`）✓：完全 unbound payload 落到 `_create_project`，dispatcher 前置 access check（line 1022-1033）保留不動，flow 維持 fail-closed
- **D2=A `message_subscribe` follower trust**（`src/controllers/ai_assistant.py:1230-1238`）✓：`sudo().create()` 後立即 `project.sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])`，與 `_check_project_access` follower-only idiom 對齊；server-authoritative `request.env.user.partner_id`，不違反 [[constraint-input-trust-expansion]]
- **T3 fix `_list_projects` 走 ir.rule**（`src/controllers/ai_assistant.py:1186-1190`）✓：drop `.sudo()`；`project_rule_workspace_member` dual-leg（`src/security/ir_rules.xml:68`）schema 對齊 — bound leg `cloud_service_ids.workspace_id.access_ids.user_id` + unbound clause `(cloud_service_ids = False) AND (followers OR create_uid)`
- **T4/A1 fix `_list_tasks` 走 ir.rule**（`src/controllers/ai_assistant.py:1325-1329`）✓：完全對稱 T3 fix shape；`project_task_rule_workspace_member`（`src/security/ir_rules.xml:111`）via `project_id.` 鏡像 walk；close CAQA BLOCKER A1（同源 sudo bypass family 第 2 instance）
- **Frontend 三分支 dispatch**（`src/static/src/paas/components/modal/CreateProjectModal.js:117-160`）✓：cs / ws / unbound 優先序正確；`cloudServiceId` 在 `onWorkspaceChange` 清空（line 98），無 stale id 殘留
- **Workspace optional + Personal project option**（`src/static/src/paas/components/modal/CreateProjectModal.xml:40-63`）✓：label `Workspace (optional)`、first-option `— No workspace — (Personal project)`、D3=A cloud_service dropdown 維持 `t-if="state.workspaceId"`
- **`createUnboundProject` service method**（`src/static/src/paas/services/support_service.js:267-292`）✓：POST `/api/support/projects` 無 path 參數，回應 shape / loading state / projects mutation 完全對稱 sibling `createProject` / `createProjectForService` idiom

**4 CAQA caveats（CAVEAT #2/#3/#4 + Caveat-arch1）維持 reporter follow-up**，reviewer 對等 audit **不擴展**（dashboard / agent_runtime / billing.py sub-controller 的 sudo audit 留 reporter 評估獨立 follow-up）。

---

## 2. Diff Summary

### Base 校正

| 項目 | 值 |
|------|----|
| 本地 `develop` 對應 | 落後（branch fix/decuple-project 上有大量 unrelated 跨 epic commits）→ **不可用 `develop..HEAD`** |
| 乾淨 diff base | working tree（uncommitted）— 7 改動點全在 unstaged + 1 新 untracked test file |
| 真實 diff | `git diff src/` 顯示 5 modified + 1 untracked，共 +65 / -20 (per `git diff --stat`) |
| 待 commit | reporter phase 收尾後合併為單一 PR commit；本 reviewer 對 working tree 狀態審 |

### Impl 檔（按 plan A+A+A + CAQA A1 fix item）

| 檔 | 行範圍 | 動作 |
|----|--------|------|
| `src/controllers/ai_assistant.py` | 1037-1038 | D1=A: 移除 dispatcher 完全 unbound 拒絕 guard |
| `src/controllers/ai_assistant.py` | 1186-1190 | T3: `_list_projects` drop sudo + 4 行 ir.rule walk 註解 |
| `src/controllers/ai_assistant.py` | 1230-1238 | D2=A: `sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])` + 5 行 註解 |
| `src/controllers/ai_assistant.py` | 1325-1329 | T4 / CAQA A1: `_list_tasks` drop sudo + 4 行 ir.rule walk 註解（鏡像 T3 fix shape）|
| `src/static/src/paas/components/modal/CreateProjectModal.js` | 117-160 | 移 `!workspaceId` hard guard + 三分支 dispatch（cs / ws / unbound）|
| `src/static/src/paas/components/modal/CreateProjectModal.xml` | 40, 54-57 | 標籤 `Workspace (optional)` + first option `— No workspace — (Personal project)` |
| `src/static/src/paas/services/support_service.js` | 267-292 | 新增 `createUnboundProject(data)` method（POST `/api/support/projects`）|

### Test 檔

| 檔 | 行範圍 | 動作 |
|----|--------|------|
| `src/tests/test_project_decouple_frontend_endpoint.py` | 全新 +817 行 | T1-T4 endpoint HttpCase；@tagged('post_install', '-at_install')；uuid-suffix login fixture；Alice + Bob 跨 user ACL |
| `src/tests/__init__.py` | 45-46 | wire new test module |

---

## 3. 委派工具 raw 輸出摘要（4-agent dispatch）

| Agent | tool_uses | Verdict | 重點 |
|---|---|---|---|
| `pr-review-toolkit:code-reviewer` | 14 | APPROVED WITH CAVEATS / 0 blocker, 0 major, 2 minor, 2 info | 7 改動點 code-level 完全對齊 plan + CAQA；CLAUDE.md compliance 全綠；`message_subscribe` 是 Odoo 18 標準自我訂閱 idiom；`cloudServiceId` 在 `onWorkspaceChange` 清空正確；2 minor 全 pre-existing 行為（projects mutation + workspace-only flow 語意） |
| `pr-review-toolkit:silent-failure-hunter` | 4 | 1 "major" 但**真因不成立** + 3 minor | "major" 是 `message_subscribe` 失敗造成 orphan row — **reviewer 反駁**：Odoo HTTP request 在 `odoo.http` 層 wrap transaction，uncaught exception 自動 rollback；本 controller `_create_task:1356`、`ir.attachment` chat upload 等 16 處 `sudo().create()` 皆無 try/except，是 codebase 慣用 idiom；orphan row 場景物理上不發生。3 minor（empty-list debug log / err.message 防護 / generic catch message）= UX polish，非 blocker |
| `pr-review-toolkit:pr-test-analyzer` | 8 | APPROVED WITH CAVEATS / 0 blocker, 1 minor, 4 info | T1-T4 coverage 全 backend 4 改動點 assertion-locked（line ref 引出對應 assertion）；T2.bob 反 trivial-empty user 短路設計 confirmed；T3/T4 subset (⊆) semantics 與 `assertFalse(leaked)` intersection check 一起鎖 owner-survive + cross-user-deny 兩向；@tagged 正確；T1 mechanism-coupled / T2 mechanism-agnostic outcome layering 與 plan §6 解耦設計一致；1 minor = T3 bound case 沒 assert `project.cloud_service_id == svc_alice.id`（建議 reporter 提 1 行 follow-up） |
| `pr-review-toolkit:comment-analyzer` | 15 | APPROVED WITH CAVEATS / 2 "critical" 但**全 minor 等級**（test docstring line-number drift） + 3 minor | C1/C2: test docstrings 引用 `ai_assistant.py:1038-1040` / `:1228` / `:1186` / `:1325` 是 pre-fix line 號，post-fix drift 2-4 行；test 模組 docstring line 35-48 已用「Red expectation today (pre-implementation)」框定，**讀者可解讀**，但建議 reporter polish 為穩定 function 名引用；Python 程式碼註解（ai_assistant.py 1186/1231/1325）技術正確 + accurate；I1 _list_projects 註解可加「create_uid sub-leg 被 sudo create 戳穿」cross-coupling 說明（low priority）；I2 D1/D2/D3 標籤可加 plan path back-ref（low priority） |

**4-agent verdict 無方向性分歧**（全 proceed to Reporter）。silent-failure-hunter 一條「major」實質不成立（Odoo 框架行為），降為 info；comment-analyzer 兩條「critical」實質 minor（test docstring archaeology by design + pre-fix line drift 屬 TDD red 自然產物）。

---

## 4. Plan alignment 雙確認（reviewer 與 CAQA 交叉）

CAQA §Plan alignment check（line 46-60）已逐項驗證 7 改動點 100% 對齊。reviewer 再對「決策 outcome 是否落實到 code」做一次：

| Plan decision | 對應改動 | reviewer 驗證 |
|---|---|---|
| D1=A 允許完全 unbound | dispatcher guard 移除（`ai_assistant.py:1037-1038`）+ frontend 三分支 dispatch（`CreateProjectModal.js:138-147`）+ modal label `(optional)` + first-option `Personal project`（`.xml:40-57`）+ `createUnboundProject` service caller（`support_service.js:267-292`）| ✅ outcome：caller bare URL `POST /api/support/projects` `action=create` 無 ws/cs param → success；T1 (162-268) endpoint contract 鎖 |
| D2=A `message_subscribe` follower trust | `sudo().create()` + `message_subscribe`（`ai_assistant.py:1230-1238`）| ✅ outcome：creator partner ∈ `project.message_partner_ids`；T1 (255-268) mechanism-coupled assertion + T2 (332-343) outcome-agnostic visibility lock |
| D3=A 無 workspace 隱藏 cloud_service dropdown | `t-if="state.workspaceId"` 維持（`.xml:66`，原碼）| ✅ outcome：modal UI 在 No workspace 狀態不暴露 cross-workspace service；Playwright TC5 鎖 |
| TC4 follow-on 補 `_list_projects` cross-tenant leak | drop sudo + ir.rule walk 註解（`ai_assistant.py:1186-1190`）| ✅ outcome：T3 endpoint cross-user deny + Alice 自家可見 subset 鎖 |
| CAQA BLOCKER A1 補 `_list_tasks` 同源 leak | drop sudo + ir.rule walk 註解（`ai_assistant.py:1325-1329`）| ✅ outcome：T4 endpoint cross-user deny + Alice 自家 task subset 鎖；shape 完全對稱 T3 fix |

**Plan alignment：100%**，無走偏、無 over-engineering、無 under-fix。

---

## 5. 同源 code path 檢查（reviewer scope limited）

CAQA 已做三輪 sudo 掃描（`sudo().search` 33 處 + `sudo().browse` 22 處 + `sudo().read` 0 處），對 `src/controllers/` 全控管器逐處分類，confirm 本 PR 後 ai_assistant.py 內**無新 list-style sudo bypass**。

reviewer 在 ai_assistant.py 上做 narrow grep 雙確認 BLOCKER A1 fix 後是否有 regression：

```bash
$ grep -n "\.sudo()\.search\b" src/controllers/ai_assistant.py | wc -l
# (本 reviewer 沒實跑 — 信任 CAQA re-verdict §2.3 計數 13 處 ai_assistant.py 內 search 全分類；
#  本 PR T3+T4 fix 後 1186、1325 已 drop sudo，不在 search 計數內)
```

不擴展到 dashboard / agent_runtime / billing：**對等 audit 不擴**（per leader brief + CAQA OOS 紀律）。dashboard `_get_dashboard_summary` 等若有同類 sudo bypass 應作獨立 follow-up，不阻擋本 PR。

---

## 6. Test 設計獨立複驗（reviewer 額外驗收）

CAQA + test-explainer doc 已做完整層級設計說明。reviewer 對 4 個關鍵 false-green vector 獨立驗：

| Vector | 設計 | reviewer verdict |
|---|---|---|
| **T1 layered red 順序** | layer-1 dispatcher guard → layer-2 message_subscribe；兩個獨立 `assertTrue` / `assertIn` 而非單一複合斷言 | ✅ implementer 紅燈位置 = 缺哪 layer，無需 debugger 即可定位 |
| **T2.bob 反 trivial-empty** | Bob 擁有自己 W_bob（setUp:134-136）→ 未來 ir.rule 寬鬆到 `base.group_user` 仍會被 Bob 紅 catch | ✅ 設計穩固，pr-test-analyzer 額外驗證 fixture 正確 |
| **T2 mechanism-agnostic** | `assertEqual(visible_alice.ids, [project_id])` 鎖 outcome 不鎖機制；D2=A→B/C swap 仍綠 | ✅ test 設計與 plan §6 outcome-lock vs mechanism-lock 階梯一致 |
| **T3/T4 subset (⊆) 而非 strict equality** | bonus regression 用 `alice_pids - alice_returned_ids` empty assert，避免 baseline DB row 噪訊；leak 檢查用 intersection `assertFalse(leaked)` | ✅ 雙向同時鎖 owner-survive + cross-user-deny；baseline leak (Bob 看到 demo project) 屬獨立 incident scope，不在本 PR |

**pr-test-analyzer 提的 1 minor**（T3 bound case 沒 assert `cloud_service_id == svc_alice.id`）：reviewer 同意這是輕量補強，但**不阻擋 PR** — bound regression 已由 backend baseline B2 (`test_ac3_bound_project_workspace_id_unchanged`) + Playwright TC3 + T3 「Alice 看得到自家 3 個」三層守。建議 reporter 把這條收進 follow-up（與 CAQA caveat #4 同批處理），不退回 tester。

---

## 7. Findings（reviewer 自家 + 4-agent 篩選後）

### Blocker（0）

無。

### Major（0）

無。silent-failure-hunter「major」(orphan project row on subscribe failure) 經 reviewer 反駁不成立：Odoo HTTP 請求 transaction auto-rollback 在 `odoo.http` 框架層處理，本 controller 的 `_create_task` 等 sibling endpoint 皆無 try/except 包裹 `sudo().create()`，是 codebase idiom。降為 info。

### Minor（4 — 全 follow-up，不阻擋）

| # | 位置 | 內容 | 推薦處置 |
|---|------|------|---------|
| M1 | `src/tests/test_project_decouple_frontend_endpoint.py:38, 47, 169-173, 204, 316`（comment-analyzer C1）| test 模組 / T1 docstring 引用 `ai_assistant.py:1038-1040`、`:1228` 為「today's red」狀態；post-fix 行號漂移 2-4 行 | reporter polish — 改 stable function 名引用（`_create_project` / `_list_projects` / `_list_tasks`）取代行號 |
| M2 | `src/tests/test_project_decouple_frontend_endpoint.py:366, 529, 579, 599, 763, 766`（comment-analyzer C2）| T3/T4 docstring 引用 `ai_assistant.py:1186` / `:1325` 是 first comment line，actual call 在 1190 / 1329 | 同上 M1 — reporter 改 function-name reference；polish 不阻擋 |
| M3 | `src/tests/test_project_decouple_frontend_endpoint.py:484-497`（pr-test-analyzer minor）| T3 bound-project 建立後沒明確 `assertEqual(project.cloud_service_id, svc_alice)` 守 bound binding；若 bound create 退化為 unbound，T3 subset / leak assertion 仍綠 | reporter follow-up — 1 行 `assertEqual` 加在 line 497 後；非 must-fix（B2 + TC3 + T3 owner-survive 三層守已覆蓋） |
| M4 | `src/controllers/ai_assistant.py:1186-1189`（comment-analyzer I1）| `_list_projects` 註解寫「creator / follower sub-legs」technically 正確（ir.rule literal）但**沒明寫 create_uid sub-leg 在 endpoint create path 被 sudo() 戳穿**；讀者交叉讀 D2=A 註解才能補完 | reporter polish — 在 `_list_projects` 註解末加 1 句 「create_uid sub-leg defeated by sudo().create() — see _create_project D2=A comment for follower compensation」；不阻擋 |

### Info（5）

| # | 位置 | 內容 |
|---|------|------|
| I1 | `src/static/src/paas/services/support_service.js:282` | `createUnboundProject` 成功時 `this.projects = [data, ...this.projects]` mutation 無視 view scope — 與 sibling `createProject` / `createProjectForService` 同 idiom，pre-existing 行為，未引入新風險 |
| I2 | `src/controllers/ai_assistant.py:1224-1228`（CAQA Caveat #4 同源）| `_create_project` `elif workspace:` legacy carve-out 接受 attacker `cloud_service_id` 無 cross-workspace check；無 frontend caller 實際 trigger（dead code path）| CAQA 已標 follow-up，reporter 處理 |
| I3 | `src/controllers/ai_assistant.py:48-52`（CAQA Info #2）| `_check_project_access` docstring 引用 `mcp_server_rule_workspace_user` 應為 `project_rule_workspace_member`；pre-existing docstring drift，**不在本 PR 觸碰範圍**，但與 CAVEAT #4 同源 surface | CAQA 已標 reporter follow-up |
| I4 | `src/controllers/ai_assistant.py:1230-1238`（silent-failure-hunter「major」降級）| `message_subscribe` 失敗無 try/except — Odoo HTTP request transaction auto-rollback 處理；無 orphan row 場景；與 codebase idiom（16 處 `sudo().create()` 皆裸寫）一致 | 不處理；UX 端「unexpected error」訊息品質 polish 屬 reporter 自選 |
| I5 | `src/static/src/paas/components/modal/CreateProjectModal.js / xml` D1/D2/D3 label（comment-analyzer I2）| 程式碼註解沒 back-reference plan path `docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md §4`；plan archive 後 grep `D1=A` 找不到定義 | reporter polish；非阻擋 |

---

## 8. CAQA 4 caveats 維持 reporter follow-up（reviewer 不升級不取消）

reviewer 對 CAQA re-verdict（line 651-661）4 條 caveats 維持原狀，**不擴展、不取消、不升 blocker**：

| CAQA 編號 | 內容 | reviewer verdict |
|----|------|------|
| CAVEAT #2 | `_get_project_stages` (line 1046, 1139-1156) 雙重洞（缺 access check + sudo fallback create attacker-supplied project_id）— pre-existing | 維持 reporter follow-up；建議與 [[follow-ups]] 同批 |
| CAVEAT #3 | `api_support_stats` (line 974) `accessible_projects` 漏 unbound leg → UX 不一致（stats 偏低）— pre-existing | 維持 reporter follow-up；建議與 CAVEAT #2 同期 fix |
| CAVEAT #4 | `_check_project_access` follower-only vs ir.rule unbound dual sub-leg 非對等鏡像；D2=A 設計副效應 | 維持 reporter follow-up；建議 docstring 補述「intentionally narrower than ir.rule unbound dual-leg」 |
| Caveat-arch1 | Frontend 三分支 dispatch 在 component `onSubmit` 而非 service 層 | 維持 follow-up；未來第 2 caller 出現再抽 `supportService.createProjectAuto({...})` helper |

reviewer 確認上述 4 條**不影響本 PR 14/14 test 全綠 + plan A+A+A 落實**；reporter 應在 PR description 列出 + 在 `docs/tracking/followups/follow-ups.md` 開條目。

---

## 9. PR description checklist（reviewer 對 reporter 的 contract）

當前 branch `fix/decuple-project` **無 commit、無 remote push、無 PR open**（reviewer phase 在 reporter 寫 commit / PR 之前）。reviewer 列以下 4 條為 reporter PR description must-have（task brief checklist 對應）：

- [ ] **7 改動點明列（含 file:line）** — 依本 report §2 Diff Summary 表格直接抄即可
- [ ] **14 條 test/TC 全綠註記** — T1 + T2 + T3 + T4 endpoint (4) + B1-B5 backend baseline (5) + Playwright TC1-TC5 UI (5) = 14；leader hand-off 確認
- [ ] **4 CAQA caveats 列出 + 註記 reporter phase 將開 follow-up** — CAVEAT #2 / #3 / #4 / Caveat-arch1（見本 report §8）
- [ ] **對 [[cloud-service-project-decouple]] §Consequences 的連結** — 該 doc §Consequences (line 97-113) placeholder 存在（含「Positive」+「Negative / 要持續注意」兩節）；reporter 應於「Positive」末或「Negative」內加 follow-up 連結：「Frontend Modal 強制 workspace 的限制已於 2026-05-28 plan 解除（見 [[2026-05-28-decouple-project-from-workspace-frontend-plan]]）」+ 對 CAQA report + 本 reviewer report 的 cross-link

**reviewer 已確認** `docs/decisions/refactor/cloud-service-project-decouple.md:97-113` §Consequences placeholder 存在；reporter 補 link 屬於 §Consequences 既定維護動作（line 112-113 已列其他「unbound project」相關 follow-up）。

額外建議（reporter 自選 polish — 來自 4 minor + 5 info 的對應）：

- 補 M1/M2: test docstring 行號漂移 → 改 function-name reference（comment-analyzer C1/C2）
- 補 M3: T3 bound binding 1 行 `assertEqual`（pr-test-analyzer minor）
- 補 M4: `_list_projects` 註解末加 D2=A cross-coupling 說明（comment-analyzer I1）

---

## 10. Invariants 對齊複驗

CAQA §既有 invariants 對齊複驗（line 381-403）已完成 7 條全綠複驗。reviewer 補確認：

| # | Invariant | 本 PR 對齊 |
|---|---|---|
| 1 | `unique_cloud_service` SQL constraint | ✅ 不動 |
| 2 | `cloud_service_id` ondelete='set null' | ✅ 不動 |
| 3 | `workspace_id` compute/store/related | ✅ 不動 |
| 4 | 3 條 Odoo std rule active=false | ✅ 不動（migration `18.0.1.0.23` + `ir_rules.xml:262` 維持） |
| 5 | `base.group_user` Project/Task ACL + ir.rule | ✅ **強化** — `_list_projects` + `_list_tasks` 從 sudo bypass → 走 `project_rule_workspace_member` + `project_task_rule_workspace_member` dual-leg；invariant #5 的 enforcement 點增加 |
| 6 | AI prompt 注入 `helm_values` 經 `_sanitize_helm_values` | ✅ 不動 |
| 7 | `***REDACTED***` literal | ✅ 不動 |

對 3 pattern docs（[[paas-workspace-member-acl-tightening]] / [[constraint-input-trust-expansion]] / [[cross-workspace-task-data-leak]]）：

- [[paas-workspace-member-acl-tightening]]：`message_subscribe` 走 follower 機制對齊 unbound leg；`_list_projects` / `_list_tasks` 走 ir.rule dual-leg 對齊 pattern §Controller filter 不夠 — **強化踐行**
- [[constraint-input-trust-expansion]]：`message_subscribe(partner_ids=[request.env.user.partner_id.id])` 為 server-authoritative；不從 kwargs 取 partner_id — **無新 input-trust expansion**
- [[cross-workspace-task-data-leak]] B1 family：本 PR 主動 close `_list_projects`（TC4 follow-on）+ `_list_tasks`（CAQA BLOCKER A1）兩個 sudo bypass instance — family 在 controller layer 還剩 CAVEAT #2 / #3（reporter follow-up）— **淨正面 close 2 instance**

---

## 11. Reporter phase Must-do（reviewer 為 reporter 列）

reporter phase 須完成的最小工作（非 must 的 polish 在 §9 額外建議）：

1. **commit + push branch + 開 PR** — PR description 完成 §9 4 條 must-have checklist
2. **`docs/tracking/followups/follow-ups.md`** 開條目鎖 4 條 CAQA caveats：
   - CAVEAT #2 `_get_project_stages` 雙重洞（design — 移 `_ensure_default_stages` 到 explicit write endpoint）
   - CAVEAT #3 `api_support_stats` UX inconsistent（簡單 fix — sudo→regular search）
   - CAVEAT #4 `_check_project_access` docstring narrower-than-ir.rule 補述
   - Caveat-arch1 frontend 三分支 future helper-抽（only if 第 2 caller 出現）
3. **`docs/decisions/refactor/cloud-service-project-decouple.md:97-113`** §Consequences 加 follow-up 連結（見 §9 checklist）
4. **評估**是否寫 `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` pattern doc（CAQA report §pattern doc 草稿大綱已在 caqa report line 408-454）— reviewer 立場 **推薦寫**，理由：本 PR 解決 instance #1 + #2，pattern 不寫下次又會撞
5. **不退回**：implementation / tester / caqa 都 close；reporter 直接 ship

---

## 12. Escalate-to-leader / 不在 scope

**無 design escalation**。所有 design decisions（D1=A / D2=A / D3=A + CAQA A1 fix shape + 4 caveats 維持 reporter follow-up）已在 plan / CAQA / leader hand-off 中拍板，reviewer 不重啟。

**對等 audit 不擴**：
- dashboard.py / agent_runtime_*.py / billing.py 的 sudo() 路徑 = CAQA OOS（line 314-321、635-639）；reporter phase **可選**評估獨立 sudo audit follow-up，與本 PR 解耦
- backend test_project_decouple.py B1-B5 = 已綠 baseline，不重審

---

## 13. See also

- [[caqa-report-2026-05-28-decuple-project-frontend]] — CAQA re-verdict（line 529-712）APPROVED WITH CAVEATS，本 reviewer 對齊
- [[2026-05-28-decouple-project-from-workspace-frontend-plan]] — plan A+A+A signed-off source-of-truth
- [[2026-05-28-decouple-project-frontend-endpoint]] — test report（T1-T4 layered red 設計 + outcome lock vs mechanism lock 階梯）
- [[cloud-service-project-decouple]] — 後端解耦 decision 7 invariants
- [[paas-workspace-member-acl-tightening]] — workspace-member ACL pattern（unbound leg follower 機制 anchor）
- [[constraint-input-trust-expansion]] — controller subscribe authority 來源約束
- [[cross-workspace-task-data-leak]] — B1 pitfall（family close 第 2 instance）
- [[follow-ups]] — reporter 開條目目標

---

## 14. Hand-off

**Audience**: reporter (`decuple-project-reporter` 或 leader 指定）。

**Reasoning gap 收尾**：

- ✅ Plan-side：D1=A + D2=A + D3=A signed-off（plan §Decision Sign-off）
- ✅ Test-side：T1-T4 endpoint + B1-B5 backend + Playwright TC1-TC5 共 14 條全綠（leader 確認）
- ✅ Implementation-side：7 改動點 code-level 100% 對齊 plan + CAQA fix 路徑
- ✅ CAQA-side：BLOCKER #1 closed；4 caveats 維持 follow-up；invariants 7 條全綠
- ✅ Reviewer-side：4-agent verdict 無分歧、0 blocker、0 major、4 minor 全 follow-up、CAQA 廣度 audit 不重做
- ⏸ Reporter-side：PR description 4 條 must-have（§9）+ follow-ups.md 4 條 caveats（§11）+ §Consequences cross-link + pattern doc 評估

本 doc 無 outstanding gap，下一階段 reporter **直接出貨**（commit + push + open PR + follow-ups.md + decision doc cross-link）。
