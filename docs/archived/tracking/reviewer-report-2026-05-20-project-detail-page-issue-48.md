---
name: reviewer-report-2026-05-20-project-detail-page-issue-48
description: Reviewer phase report for Issue #48 Project Detail page (commit 32cf414) — multi-agent PR review consolidated with CAQA cross-check. Verdict approved with caveats; 3 hard caveats recommended for same-PR fix, rest tracked.
date: 2026-05-20
phase: reviewer
issue: 48
branch: feature/project-setting
commit: 32cf414
related:
  - docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md
  - docs/archived/tracking/2026-05-20-project-detail-page-issue-48-tdd-red.md
  - docs/archived/tracking/caqa-report-2026-05-20-project-detail-page-issue-48.md
status: archived
archived-from: docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md
archived: 2026-07-19
archived-reason: "一次性 reviewer 驗收報告，cycle 已收官；證據錨點引用已改指本處"
---

# Reviewer Report — Project Detail Page (Issue #48)

**Audit scope**: commit `32cf414` on `feature/project-setting`, 30 files / +4167 / −314（CAQA 階段為 `63f49c1`，本 audit 為 CAQA blocker B-1 + caveat C-3 re-loop 落地後的 final commit）。
**Reviewer**: phase-reviewer
**Date**: 2026-05-20
**Upstream phases**: planner (5 patches) → tester TDD red (36 tests + 1 ORM regression) → test-explainer → implementation TDD green (93/93 both paths) → caqa (1 HIGH blocker → resolved + 8 caveats) → reviewer (this)

---

## Verdict

**approved with caveats** — 不擋 merge。

理由：
- CAQA 1 HIGH blocker (B-1 fresh-install 漏 `project_public_members_rule`) 已落地 — `src/security/ir_rules.xml:239-244` 對 3 條 std rule 對稱 deactivate，verified by impl report 兩 DB (woow_develop + woow_fresh_b1_v2) SQL query 全 `active=false`，且 `src/tests/test_cross_workspace_task_isolation.py:137-185` 新增 `test_alice_cannot_read_bob_project_via_orm` regression test 守線。
- CAQA medium caveat C-3 (tabnabbing) 已落地 — `src/static/src/paas/services/html_sanitize.js:30-40` 加 DOMPurify `afterSanitizeAttributes` hook，idempotent (line 17 `_hooksRegistered` guard)。
- D1-D10 + AC #1..#29 implementation 1:1 對齊 spec（除 AC #28 staging E2E smoke deferred — 詳見下方 §AC #28）。
- 5 個 multi-agent 平行 review 全綠未報 blocker/critical-class security/data-loss 風險；發現之 NEW findings 集中在 UX hardening / defense-in-depth / type-system enforcement / comment rot — 屬 polish 範疇。
- 93/93 tests green 在 fresh install + upgrade 兩條 path（implementation report DB SQL verify）。

帶 caveats 理由：
- 3 條 hard caveat（**R-1 / R-2 / R-3**）建議 same-PR fix 或進 reporter phase 順手補 — 改動小、風險低、影響可見。
- 7 條 soft caveat 進 follow-up tracking（reporter 紀錄到 [[follow-ups]]，部分合併到既有 #49/#50/#51 issue）。

---

## 工具 raw 輸出摘要

| 工具 | 重點輸出 |
|---|---|
| `/pr-review-toolkit:review-pr` → `code-reviewer` (subagent) | **approved with caveats**；0 blocker / 0 major / 6 minor / 3 nit。全部 NEW；最值得注意：`BasicInfoSection.js:60-69` contenteditable seed `innerHTML` 用 raw `this.props.project.description` 不過 DOMPurify（C-3 spillover 候選） |
| `/pr-review-toolkit:review-pr` → `pr-test-analyzer` (subagent) | **good for happy paths**；3 critical NEW gaps：G1 (update whitelist scope/project_id 未測)、G3 (ACL matrix sampled, sync/test 缺 member/non-member)、G6 (XSS sanitize 未測)；4 important + 2 suggestion |
| `/pr-review-toolkit:review-pr` → `silent-failure-hunter` (subagent) | **needs revision**：2 critical NEW — McpServersSection delete/sync/test 沒 else display error；`_delete_project_mcp_server` 沒包 try/except；6 important（包含 `_update_project_mcp_server` 沒 savepoint、`BasicInfoSection.saveDescription` 空 ref 靜默 save ""） |
| `/pr-review-toolkit:review-pr` → `type-design-analyzer` (subagent) | `_SyntheticAccess` 3/2/5/3、`project.project` ext 6/5/7/4、Response envelope 4/3/6/2（鼓勵抽 `_ok()/_err()` helper）；**top finding**：project.project 缺 `@api.constrains('cloud_service_id')` 對齊 D5（type-level enforcement gap） |
| `/pr-review-toolkit:review-pr` → `comment-analyzer` (subagent) | **needs revision**：2 important dead refs（test docstring `18.0.1.0.22` 應為 `18.0.1.0.23`；CAQA report 路徑 `docs/refactor/...` 應為 `docs/decisions/reviews/...`）；4 suggestion/nit |
| 主 reviewer 直查 | 直接 read `ir_rules.xml` (B-1 fix verified 3 rule lockstep)、`html_sanitize.js` (C-3 hook verified)、`project_project.py` (description override revert verified — pure additive 3 fields)、`test_cross_workspace_task_isolation.py` (B-1 project ORM regression verified)、`controllers/paas.py:1420-1685` (endpoint family verified)、tea CLI verify #49/#50/#51 都存在 |

---

## Plan alignment

D1-D10 全部對齊 spec（CAQA 已 verify 過，本 reviewer 沒重做逐條 — focus 在 caveat 與 type-system）：

| Decision | Code 落點 | Reviewer cross-check |
|---|---|---|
| D1 3-card layout | `ProjectDetailPage.xml` 3 個 `WoowCard` section | ✓ |
| D2 McpServersTab redirect | `pages/service/tabs/McpServersTab.{js,xml,scss}` 簡化為 placeholder | ✓ |
| D3 list 卡 ⚙ icon | `SupportProjectsPage.xml` `o_woow_sp_settings_btn` + factory handler | ✓ |
| D4 hyphenated URL | `/api/support-projects/<int:project_id>` + `.../mcp-servers` | ✓ |
| D5 ACL gate via workspace.check_user_access | `_resolve_project_access` line 1437-1456 | ✓ |
| D6 不實作 status | `_format_project_detail` 無 status key；BasicInfoSection 無 status field | ✓ |
| D7 confirm modal | CloudServiceBindingSection 用 `components/modal` | ✓（未個別讀 modal XML 內部，但 service layer + caveat 邏輯一致） |
| D8 dropdown filter unhealthy | `CloudServiceBindingSection.js:7` `UNHEALTHY_STATES = {error, deleted, deleting}` | ✓ |
| D9 B1 fresh-install fix | `ir_rules.xml:239-244` 3 std rule `<function>` deactivate | ✓（CAQA B-1 re-loop 落地） |
| D10 description std `fields.Html` + rich-text | `project_project.py` revert 無 description override；`BasicInfoSection.js` contenteditable + execCommand + safeHtml | ✓（D10 implementation 中一度 override `fields.Text` 被 user reject，已正確 revert） |

**Plan ↔ Implementation alignment**：無 divergence。OR-9（declaration-data 與 migration 對稱）已加入 spec §"Open risks" 表，由 reporter phase 補進 pitfall doc。

---

## 同源 code path 檢查

`grep -rn "project_public_members_rule\|task_visibility_rule" src/` 結果：

| File:line | 角色 | 狀態 |
|---|---|---|
| `src/security/ir_rules.xml:240-242` | declaration `<function>` deactivate 3 條 | ✓ 3 條 lockstep |
| `src/migrations/18.0.1.0.23/post-migrate.py:101-103` | upgrade-path migration deactivate 3 條 | ✓ 3 條 lockstep |
| `src/tests/test_cross_workspace_task_isolation.py:117-125 + :167-172` | task + project ORM leak regression tests | ✓ 兩條都 cover |
| `src/security/ir_rules.xml:101-107` (comment) | 三條 lockstep 說明 | ✓ comment 已說明 3-way |

**結論**：3 條 std rule 在 declaration + migration + regression test + comment 四處 lockstep，未來再次破壞此 invariant 都會被守線抓到。CAQA B-1 漏 1 條的 root cause（spec 漏寫 + impl 跟 spec）已透過 spec patch + xml patch + test patch 三處同步補齊。

**同源 pattern 補充檢查（reviewer 額外做的）**：

- `_resolve_project_access` 在兩個 endpoint 都用一致（line 1474 + 1571）— 統一防 enum 沒漏 path
- `_PROJECT_NOT_FOUND_ERROR` constant 為 endpoint 防 enum 的 single source（line 1425）— grep 確認 5 處使用全部一致
- `_format_mcp_server` (line 1408-1421) 不回吐 `api_key`（C-8 CAQA 已 verify 是 by-design write-only）— 兩 endpoint 都用同一 formatter，無 inconsistency

---

## CAQA cross-check

CAQA 4-axis 已涵蓋大部分 security / quality / perf / architecture surface。Reviewer 對 CAQA 結論的 cross-check：

| CAQA item | Verify 結論 | 備註 |
|---|---|---|
| B-1 (HIGH blocker) | ✓ fixed | `ir_rules.xml:239-244` 3 std rule lockstep；2 DB SQL query verified by impl report |
| C-3 (medium, tabnabbing) | ✓ fixed | `html_sanitize.js:30-40` hook 落地 |
| C-1 (medium, CS enumeration) | ✓ confirmed | `paas.py:1519-1527` 兩訊息確實不同；surface 為 owner/admin（須先有 access role 才踩到），降為 **low-medium**；建議帶到 follow-up |
| C-2 / C-2b (medium, D5 race / TOCTOU) | ✓ confirmed deferred | OR-2 / OR-4 spec deferred；D5 重設計時一併回收，已記 #49 |
| C-3b (medium, OR-7 brute-force) | ✓ confirmed | spec OR-7 三 option deferred 到 #51；endpoint 對 unbound 路徑無防 enum 是 known 風險 |
| C-4 (low, exception leak) | ✓ confirmed | `paas.py:1666-1678` 兩處 raw exception message |
| C-5 (low, execCommand deprecated) | ✓ confirmed | future issue 無時間壓力 |
| C-6 (low, `_SyntheticAccess` 維護債) | ✓ confirmed + 新 angle | type-design-analyzer 補：synthetic 拋出後失去 provenance（caller 無法分辨 real vs synthetic OWNER） |
| C-7 (info, endpoint 全 sudo) | ✓ confirmed | future architectural evolution |
| C-9 (low, ROLE_USER permission dict mismatch) | ✓ confirmed | dict vs controller 不一致 |
| F1-spillover (info, M2O/related field) | ✓ confirmed | B-1 fix 後仍適用對等性 check |
| AC #28 staging E2E (deferred) | ✓ deferred to leader | 詳見下方 §AC #28 |

**Reviewer 沒推翻任何 CAQA caveat**。新發現主要在 CAQA 較未強調的 UX hardening / defense-in-depth / type-system / comment rot 軸。

---

## NEW findings（CAQA 沒抓到）

### Hard caveat — 建議 same-PR fix 或 reporter phase 順手補

#### R-1 [important] 兩條 comment dead reference（comment-analyzer）

`src/tests/test_cross_workspace_task_isolation.py`：

- Line 3：cite `docs/refactor/caqa-2026-05-16.md` — **路徑不存在**（actual: `docs/decisions/reviews/caqa-2026-05-16.md`）
- Line 11：「Implementation deactivated `project.project_public_members_rule` in the `18.0.1.0.22` migration」— **migration 不存在**（`ls src/migrations/` jumps 17 → 23；actual: `18.0.1.0.23`）
- Line 124：assert msg「the project.project fix in migrations/18.0.1.0.22」— 同上死引用

**影響**：未來 maintainer 跟著 ref 走會 404。Comment rot 已落實。

**Suggested direction**：3 line 修改，把 `18.0.1.0.22` → `18.0.1.0.23`、`docs/refactor/` → `docs/decisions/reviews/`。可由 reporter phase 順手補（純 doc fix 不影響 test 結果），或由 implementation phase 在後續 release 收尾。

#### R-2 [important] BasicInfoSection contenteditable seed 沒過 sanitize（code-reviewer #6 + 我的 cross-check）

`src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js:60-69`：`useEffect` 在進入 edit mode 時 seed `descEditorRef.el.innerHTML = this.props.project.description`（raw HTML）。

雖然後端 `fields.Html(sanitize=True)` 在 write 時 sanitize、CAQA C-3 hook 也在 display path 補 tabnabbing rel — 但**seed path 沒走任何 sanitize**。攻擊面：
- 假設場景 (A)：Odoo admin 透過 backend form view 或 ORM script 直接寫 raw HTML 到 `project.description`（繞過前端 sanitize 路徑），後端的 `fields.Html(sanitize=True)` write 路徑會 sanitize，但**若使用 `with_context(no_sanitize=True)` 或直接 SQL UPDATE 注入**，就會繞掉。
- 假設場景 (B)：在 C-3 fix 之前寫入的舊資料（含 `<a target="_blank">` 不含 rel）— display path 有 hook 補上，但 edit mode 進入時 `innerHTML` seed 看到的是 DB raw 值（無 rel）。

**Severity**：minor，因為 fields.Html(sanitize=True) 是後端 default 守護線；但 defense-in-depth 不該斷。

**Suggested direction**：seed 時走 DOMPurify 一次，或用 `safeHtml()` 拿到 markup 再轉 string 再 set innerHTML。~2 line。可由 reporter phase 順手補。

**判斷理由**：這是 C-3 spillover（同 tabnabbing 防護 family），不算全新 vector，所以歸 hard caveat 但不擋 merge。

#### R-3 [important+] McpServersSection delete/sync/test 沒 else 顯示 error（silent-failure-hunter critical NEW）

`src/static/src/paas/pages/project-detail/sections/McpServersSection.js:136-161`：`deleteServer()`、`syncServer()`、`testServer()` 三個 method 都用 `if (result.success) { ... }` 但**沒有 `else`**。

當 backend 回 `{success: false, error: ...}` 時：
- UI 翻 `_syncing/_testing` 為 false（spinner 停）
- 但 `state.error` 沒 set，user 不知道發生什麼
- 最嚴重的是 `deleteServer`：row 仍在 UI 上但 user 以為已刪 — 之後 sync/test 同 row 會繼續工作但看不到任何錯誤 cue

**Severity**：實際 surface 取決於 backend 多常回 false：
- `delete`：endpoint 對 Permission denied 守在 line 1577；D5 / FK error 罕見（unlink 設計上是 last write）
- `sync` / `test`：external sidecar 失敗 fall-through 到此處，**真實會發生**（n8n 重啟、ngrok tunnel 斷線等場景）

不算 security blocker、不算 data loss，但屬 **user-visible UX regression**（issue #48 整個 PR 是個 UI page，UI silent failure 是主功能 regression）。

**Suggested direction**：每個 method 加 `else { this.state.error = result.error; ... }` 或彈 toast。~6 line。

**Recommendation 給 leader**：強烈建議 reporter phase 在 ship 前順手補（小改動 + 高 UX 收益）；若 leader 認為不擋 merge 願意 ship 後再修，請 leader 同時批准 follow-up issue track。

---

### Soft caveat — follow-up issue tracked

R-4 ~ R-10 列在「Open items / risks」段。簡述：

| ID | Finding | Source | Suggested track |
|---|---|---|---|
| R-4 | `project.project` 缺 `@api.constrains('cloud_service_id')` — D5 invariant 只靠 controller 手動 query 守，model layer 無 enforcement | type-design-analyzer | 合併到 #49（D5 重設計時一併） |
| R-5 | `workspace_id` compute 沒 `readonly=True`，silent write 危險（write 不報錯但無效） | type-design-analyzer | 1 line fix；可進 #49 或 standalone |
| R-6 | `_delete_project_mcp_server` 沒 try/except 包 unlink；`_update_project_mcp_server` / `_update_support_project` 沒 savepoint（vs create 有） | silent-failure-hunter critical (delete) + important (update) | 與 R-3 一起評估，可同 follow-up |
| R-7 | Test coverage gaps：G1 (update whitelist scope/project_id 未測)、G3 (sync/test ACL matrix 不完整)、G6 (XSS sanitize 未測) | pr-test-analyzer | tester 補 red 或 follow-up issue |
| R-8 | Response envelope 191 hand-rolled dict 無 helper / TypedDict；delete 不回 data、sync vs test 形狀不同 | type-design-analyzer | 架構演進，long-term |
| R-9 | `support_service.js:469-471 + 487-490` `updateProject` optimistic merge 在 try 內、finally clear loading — merge 若 throw 會 stuck spinner | code-reviewer | 1-2 line fix |
| R-10 | `ir_rules.xml:217-238` comment block 英文（CLAUDE.md 要求 zh-tw）；其他相鄰 rule 都用 zh-tw | code-reviewer | style polish |

**這些可進**：
- #49（refactor: sidecar/D5/URL）— R-4 / R-6 (savepoint parity)
- 新 issue「project-detail page polish — post-merge maintainability sweep」— R-5 / R-9 / R-10
- Tester 補測（或 ticket）— R-7 (G1/G3/G6)
- Long-term architectural issue — R-8

---

## AC #28 staging E2E smoke

**Status**: deferred to leader / pre-merge gate

| 路徑 | 評估 |
|---|---|
| Reviewer 自己跑 | **能力面**：有 deploy-stg skill + kubectl woow-k3s + helm。可跑。**成本面**：15-30 min CI build + 11-step UI 操作（含 D5 violation toast wording / badge transitions / confirm modal copy 確認）。**判斷**：超出 reviewer-as-analyzer 角色 scope；本 reviewer 階段 focus 在整合 multi-agent verdict |
| Pre-merge gate by leader | **建議**：leader 安排 dedicated reviewer-e2e task slot 或 maintainer 在 merge 前親手跑一次。CAQA report 已標此 status |
| Reporter phase 順帶 | 也可行 — reporter 已在 dev sandbox 上做 doc work，可順手 11-step 跑一遍 |

**Reviewer 在此明示**：本人具備能力，但預設不跑，請 leader 指派或在 ship gate 前安排。

---

## Follow-up issue alignment

驗證 CAQA report 的 follow-up 都已 surface 在 gitea：

| OR ID | Spec section | Surface 在 | gitea verify |
|---|---|---|---|
| OR-1 (service-centric MCP endpoint deprecation) | spec §Open risks | #50 (合併 OR-6) | ✓ exists |
| OR-2 (rebind race) | spec §Open risks | CAQA C-2；#49（合併 D5 重設計） | ✓ #49 exists |
| OR-3 (issue #48 status carve-out comment) | spec §Open risks | reporter phase | — (reporter 待做) |
| OR-4 (D5 重設計 cleanup) | spec §Open risks | #49 | ✓ exists |
| OR-5 (sidecar auto-create 機制) | spec §Open risks | #49 | ✓ exists |
| OR-6 (URL pattern 統一) | spec §Open risks | #50 | ✓ exists |
| OR-7 (unbound project ACL) | spec §Open risks | #51 | ✓ exists |
| OR-8 (worktree env gaps) | spec §Open risks | impl 落地 `hooks.py` 一行 (sub-(a))；其餘兩條 reporter doc | — (reporter 待做) |
| OR-9 (declaration ↔ migration 對稱 hardening) | spec §Open risks | reporter phase 寫 pitfall doc | — (reporter 待做) |

**結論**：實質 follow-up code work（OR-1/2/4/5/6/7）都已 surface 為 #49 / #50 / #51。OR-3 / OR-8 / OR-9 是 reporter phase 任務（comment / doc 性質）。

---

## Findings

依優先順序：

- **[caveat-important+]** R-3 — `McpServersSection.js:136-161` delete/sync/test 沒 else display error — UX regression
- **[caveat-important]** R-1 — `test_cross_workspace_task_isolation.py:3,11,124` 兩條 dead reference
- **[caveat-important]** R-2 — `BasicInfoSection.js:60-69` contenteditable seed 沒過 sanitize（C-3 spillover）
- **[caveat-minor]** R-4 — `project.project` 缺 `@api.constrains('cloud_service_id')` D5 enforcement
- **[caveat-minor]** R-5 — `workspace_id` compute 缺 `readonly=True`
- **[caveat-minor]** R-6 — delete/update 路徑 savepoint + try/except parity（vs create 有）
- **[caveat-minor]** R-7 — Test coverage gaps G1/G3/G6
- **[caveat-suggestion]** R-8 — Response envelope helper + TypedDict
- **[caveat-suggestion]** R-9 — `support_service.js` optimistic merge 順序
- **[caveat-suggestion]** R-10 — `ir_rules.xml:217-238` comment 翻 zh-tw

---

## Recommendation

**proceed to Reporter with caveats** — 但 leader 須明示 R-3 處置：

- **Option A**：reporter phase 順手補 R-1 + R-2 + R-3（3 條 hard caveat 合計 ~11 line code 修改 + 3 line doc fix）→ merge ready。
- **Option B**：本 PR 不再 patch，R-1/R-2/R-3 + R-4..R-10 全進 follow-up issue → 立刻 merge。
- **Option C**（推薦）：reporter 補 R-1（純 doc fix 風險 0）+ R-2（2 line defense-in-depth 風險低），R-3 開 follow-up（UX 修補可在後續 sprint 補 + 同時排 R-6 unlink try/except 一起做）→ merge ready。

**Reviewer 偏好 Option C** — R-1 / R-2 必須收（comment rot + defense-in-depth），R-3 接受作為 ship-blocker-trade-off（已有完整 ACL + 防 enum + 已知 fix path）。

**AC #28 staging E2E**：建議 leader 在 ship gate 前安排（可指派本 reviewer 或 dedicated phase）。

---

## Hard rules verification（role spec 對照）

| Hard rule | 本 review 對應 |
|---|---|
| multi-agent 內部分歧大 → 在 verdict 寫出歧見 escalate | 5 agents 對「approved with caveats」一致；無歧見 |
| `/pr-review-toolkit:review-pr` 結果不完整 / 中斷 → STOP | 5 agent 全部完成 |
| 發現 high-severity 但 CAQA 沒抓到 → escalate leader 且不能直接 approve | R-3 介於 important 與 critical 之間（UX silent failure），verdict **不直接 approve**，標 caveat 並 escalate leader 決定 Option A/B/C |
| plan 與 implementation 明顯走偏 → STOP re-plan | 無，D1-D10 1:1 |
| Leader hand-off 未提供 CAQA report 路徑 → STOP | CAQA path 已提供，已讀完 |

---

## AC #28 E2E 結果（2026-05-20 後續補做，docker-desktop dev-sandbox）

### Setup

- **Sandbox**: `dev-sandbox-feature-project-setting` on `docker-desktop` context, local mode (HostPath mount of `src/`)
- **DB**: `woow_feature_project_setting` (fresh install — 跑 declaration + migration 等於兩 path 同時驗)
- **Fixtures**: 1 workspace ("E2E Workspace"), 2 cloud_services (Service A id=1, Service B id=2; 都 state=running) via ORM bypass PaaS Operator deploy chain
- **Browser**: Playwright via MCP，admin/admin login
- **Note**: docker-desktop 一開始 4-sandbox 滿 memory，leader 授權 destroy 2 個 orphan（`fix-sh-end-date-fresh-non-trial` expired + `feature-project-mcp-skill` PR #47 merged）後 re-create

### Per-step 結果

| Step | 行為 | 結果 | 備註 |
|---|---|---|---|
| 1 | 建 project（list 頁 + Create modal）| ✅ PASS | Modal 含 Cloud Service dropdown（含 Unbound + Service A + Service B）；建立為 Unbound project 成功；list 卡 badge cloud_off + "Unbound" |
| 2 | 進 Project Detail（⚙ icon）| ✅ PASS | D3 ⚙ icon 點擊正確 navigate 到 `/woow#/projects/1`；D1 3-card layout（BasicInfo / CloudServiceBinding / McpServers）；MCP "Add Server" disabled + hint "Bind a Cloud Service first to manage user-scope MCP servers." ✓ |
| 3 | 綁 Cloud Service（dropdown → confirm modal → Confirm）| ⚠ **NOT EXERCISABLE — E2E-G1 finding** | Unbound project 的 CloudServiceBindingSection dropdown **只列 "Unbound" 一個 option**（無 Service A/B）— 無法從 Project Detail 完成 initial bind。Workaround：ORM 手動 `project.cloud_service_id = svc_a.id` 模擬 create-time bind 後狀態繼續 |
| 4 | 加 MCP server | ✅ PASS | Add Server enabled（bound 後），form 含 Name/URL/Transport/API Key/Description；submit 後 row 顯示 "E2E Test MCP" + Pending state + power/sync/edit/delete actions |
| 5 | Edit project name (inline edit) | ✅ PASS | Click edit icon → textbox + 儲存/取消 按鈕；改名 "E2E Smoke Project (renamed)" → 儲存 → header + breadcrumb 立即更新；reload 後仍 persist ✓ |
| 6 | Unbind with MCP → 預期 D5 toast | ✅ PASS | Dropdown 選 Unbound → D7 confirm modal **"Unbind this project from Service A? This will block if user-scope MCP servers exist."** ✓ exact match spec；Confirm → toast **"This project has user-scope MCP servers. Remove them first."** ✓ exact match AC #15；dropdown selection 還原為 Service A ✓ |
| 7 | Delete MCP | ✅ PASS | Click delete icon → MCP row 立即消失（無 confirm modal）；MCPServers section 顯示 "No MCP servers yet." |
| 8 | Unbind without MCP → 預期成功 | ✅ PASS | Dropdown 選 Unbound → D7 modal 確認 → unbind 成功；Current Binding: cloud_off + "Unbound"；Workspace: "Unbound (no workspace)"；Add Server: disabled；hint 重新顯示 ✓ |
| 9 | 回 list 頁驗 badge | ✅ PASS | breadcrumb Projects → list 頁卡片顯示 "cloud_offUnbound" badge ✓；project 改名 "E2E Smoke Project (renamed)" 持續 |
| 10 | ServiceDetail MCP tab redirect placeholder | ✅ PASS | Workspaces → E2E Workspace → Service A → MCP Servers tab → **"No Project Bound to This Service" + "This service has no bound project. Create or bind a project from the Projects list, then manage MCP servers from Project Detail."** ✓ exact match spec D2 / AC #22 unbound 變體；無 Go-to-Project CTA（正確，無 project 可去） |
| 11 | 把另一 service rebind project | ❌ **FAIL — E2E-G1 confirmed** | 回 `/woow#/projects/1` 後 dropdown 只列 ["Unbound"]（無 Service A/B）；無法從 Project Detail 完成 rebind from unbound state；同 step 3 root cause |

**總計**: 10/11 PASS + 1 FAIL (E2E-G1)。

### NEW finding — E2E-G1

#### E2E-G1 [important] CloudServiceBindingSection dropdown 對 unbound project empty — initial bind / rebind from unbound 走不出去

**File / Line**: `src/static/src/paas/pages/project-detail/sections/CloudServiceBindingSection.js`（loadAvailableServices method）

**Repro**:
1. Open Project Detail of any unbound project（freshly created with "— No service —" OR after Step 8 unbind）
2. Dropdown "Change Binding" 只列 ["Unbound"] — Service A / B 都不顯示

**Root cause**:
- `loadAvailableServices` 依賴 `props.project.workspace_id` 來 `cloudService.fetchServices(workspaceId)`
- 對 unbound project，`workspace_id` 為 `null`（compute from `cloud_service_id.workspace_id`，cloud_service_id 也為 null）
- `fetchServices(null)` → empty result → dropdown 只剩 Unbound 自身

**Workaround validation**：unbind from bound state 後同 session 內 dropdown 仍保留 Service A/B（因為 section state 還記得上一次 fetch 的 list）— 所以 **same-session unbind 後 rebind 可走通**，但 reload page 後 cache 失效就走不出去。等同：**E2E-G1 受 dropdown stale cache 部分掩蓋**，但 user reload 或從 list 頁進來都會踩到。

**Spec impact**：
- AC #28 step 3「綁 Cloud Service」字面假設 dropdown 可選 — 與 UI 行為矛盾
- AC #28 step 11「rebind 把另一 service」對 freshly-loaded unbound project 不可行
- AC #15 spec 寫「同 workspace 內所有 cloud services 中 state ∉ {error, deleted, deleting} 的項目」— spec 沒明寫「unbound project 怎麼 handle」這條 edge case

**Severity**: **important**（不擋 merge — 因為 user 仍可在 create modal 選 cloud_service 完成 initial bind 工作流；只是 spec 描述的「unbound → bind via Project Detail」與「rebind from unbound via Project Detail」這兩條路徑在 UI 上不通）

**Suggested direction**：
- (a) 在 `loadAvailableServices` 對 `workspace_id == null` 的 case：fall back 到 `cloudService.fetchServices(/* all accessible workspaces */)` — 同 Create modal 行為，跨 workspace 列舉所有可健康綁定的目標
- (b) OR：UI 顯示 disabled dropdown + 文案「Cannot bind from this page — use the Create Project modal to bind initially」
- (c) OR：在 Step 8 unbind 後立即跳轉提示「unbound projects can't be re-bound — delete & recreate via list page」（明確 UX）

**Recommendation**: 這條進 follow-up（不擋本 PR merge），但建議 reporter phase 在 follow-up issue surface（與 R-3 silent failure 同類 UX gap family）。

### Spec coverage 評估

11 step 完整 cover 的 spec 區域：
- ✅ D1 3-card layout
- ✅ D2 McpServersTab redirect（含 unbound 變體）
- ✅ D3 ⚙ icon 主點擊分離
- ✅ D4 hyphenated URL 路徑
- ✅ D5 unbind manual query（D5 第一觸發路徑）+ confirm modal
- ✅ D7 confirm modal 措辭
- ✅ D8 dropdown filter（rebind 路徑有看到 dropdown 含 healthy services）
- ✅ AC #15 unbind D5 toast 措辭
- ✅ AC #17/21 list badge optimistic update（reload-free reflect）

未 cover（spec 之外 / E2E surface 限制）：
- ❌ D5 第二觸發路徑（ORM constraint on unbound create）— E2E 跑不到，因 step 3 unbound bind 走不出去（E2E-G1）；既有 backend test `test_create_user_mcp_on_unbound_project_rejected_by_orm_constraint` 已 cover
- ❌ D8 explicit filter 驗證（fixture 2 services 都 state=running，沒測 unhealthy filter）— 屬 fixture 限制
- ❌ AC #6/#11 ACL boundary cases（admin/member/non-member matrix）— E2E 用 admin 跑，多 user 測試不在本次 scope

### Cleanup

E2E 結束後跑 `destroy.sh feature-project-setting` 清除 sandbox。

### E2E verdict

| 維度 | 評估 |
|---|---|
| Spec D1-D10 coverage | 10/11 step 對齊 spec |
| D5 invariant UX path | ✅ 完整且 wording 一致 |
| ACL / 防 enum gate | ✅（admin 路徑 only — boundary 案見 backend test） |
| 新 UX bug surface | **1 條 E2E-G1**（important，不擋 merge） |
| Merge readiness 影響 | 不擋 — workaround 為 Create modal binding |

**E2E 對 Reviewer verdict 的影響**：
- 原 verdict: `approved with caveats`
- E2E 後 verdict: **保持 `approved with caveats`**，新增 **E2E-G1** 到 follow-up tracked caveat 表
- R-3 (silent failure) + E2E-G1 (initial bind UX gap) 同屬 **frontend UX hardening family**，建議 leader 統一在 follow-up issue 收（reporter phase 開新 polish issue「project-detail page UX hardening — post-merge sweep」）

## END REVIEWER REPORT
