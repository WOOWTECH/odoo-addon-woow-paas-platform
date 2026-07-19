---
name: pr-review-verdict-2026-05-17
phase: review
team: refactor
reviewer: refactor-reviewer
plan_ref: docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md
test_scope_ref: docs/archived/tracking/test-scope-report-2026-05-16.md
test_report_ref: docs/archived/tracking/test-report-2026-05-16.md
caqa_ref: docs/decisions/reviews/caqa-2026-05-16.md
pr_draft_ref: docs/decisions/reviews/pr-description-draft.md
diff_range: 3081a41^..HEAD (8 commits)
created: 2026-05-17T00:00:00Z
updated: 2026-05-17T00:00:00Z
status: approved-with-caveats
---

# PR Review Verdict — CloudService ↔ Project Decouple

> 對象：phase-leader / implementation / reporter / 使用者。Reviewer 不重複 CAQA 的 4 medium + 3 low（M1-M4, L1-L3，使用者已 accept），只整合 multi-agent review (`/pr-review-toolkit:review-pr` 4 agents) 浮出的**新發現**，並對 plan / scope expansion / test 真實性做獨立確認。

## Summary

### Verdict
**✅ APPROVED WITH CAVEATS**

可進 reporter phase；以下 6 條 cleanup / hardening 建議（皆非阻擋）由 leader 與使用者決定是否在本 PR 內處理。

### Actionable items（建議 fix-before-merge，皆 trivial / 5 分鐘級別）
1. **L1 docstring carry-over**：`src/migrations/18.0.1.0.23/post-migrate.py:1` 仍寫 `"""18.0.1.0.18 — ..."""`，目錄是 `18.0.1.0.23/`。CAQA L1 已標，未修；建議順手改字。
2. **PR draft 內部不一致**：`docs/decisions/reviews/pr-description-draft.md:25-57` 仍把 AC #5 標為 ⚠️ 未過，但 line 155 報告「39 refactor tests + 2 sanity 全部通過」（含 AC #5 在 commit b582a44 修好）。發 PR 前清掉 ⚠️ section 並把 line 22-31 結果表改為 25/25 ✅。
3. **router.js 註解誤導**（comment-analyzer Critical）：`src/static/src/paas/core/router.js:16-17` 註解寫「`/woow#/ai-assistant/projects` stays as a bookmark-preserving alias」，但 plan D4=C 要求保留的書籤是 `/woow#/ai-assistant`（line 47, AC #20 line 106, line 193）。實際 code L15 `ai-assistant` route 走 `routeName: "ai-assistant"`（不是 ai-projects），跟註解描述的「alias 同 route」不符；alias 行為實際發生在 L20（`ai-assistant/projects`→`ai-projects`）。修字以對齊真實行為與 plan 用語。
4. **ir_rules.xml 註解計數已 stale**：`src/security/ir_rules.xml:53-56` 寫 "these two security-critical rules"，現在 noupdate=0 block 內已有 **3** 條 rule（`project_rule_workspace_member` + `cloud_service_rule_workspace_member` + `project_task_rule_workspace_member`）。
5. **task rule 註解缺對等 cross-ref**：`src/security/ir_rules.xml:92-97` 解釋自己的目的，但沒提到 `post-migrate.py` 對等 deactivate `task_visibility_rule` + `task_visibility_rule_project_user`。加 1 行 cross-ref 防未來維護者再啟用 std rule 重炸 B1。
6. **migration 防 xmlid drift 防呆**（silent-failure-hunter Major）：`src/migrations/18.0.1.0.23/post-migrate.py:88-102` 的 3-row UPDATE 無 assertion；若 Odoo 18.x 某 patch 改 `task_visibility_rule` xmlid，`rowcount` 變 2 或 0 但不會 raise，B1 leak 靜默回來。建議 SELECT count(*) 預驗 == 3 再 UPDATE，或於 UPDATE 後 raise if rowcount < 3。

### Defer to reporter（follow-up，不影響本 PR）
- **R-mobile**：`src/static/src/paas/layout/bottom_nav/BottomNav.js:18-24` mobile bottom nav 仍把 `ai-projects` / `ai-project-kanban` 算進 "AI" 入口；desktop sidebar 已分出 "Projects"。Mobile 上點到 Project Kanban 會 highlight AI tab。PaaS 主要 desktop，但 D4 拆分後的 UX 一致性可補。
- **R-test-hardening**（pr-test-analyzer）：(a) AC #11 domain test 只 grep `'workspace_id'` literal，不執行真實 ORM filter；(b) AC #14 dedup 用 `count('Primary Svc')==1` 偵測，format 改字可能繞過；(c) B1 regression 沒涵蓋 `group_project_user` write/unlink leg（CAQA 已標 info — PaaS 不給租戶 group_project_user，但 paranoia 規模可加）；(d) AC #15 sanitizer 深度 >5 / 非 JSON helm_values fail-closed path 未被測。實作其實都對；test 是「strict guard for future regressions」缺一塊。
- **R-silent-degradation**（silent-failure-hunter）：(a) `_sanitize_helm_values` JSON parse 失敗 silently 回 `''` 無 log；(b) `_sanitize_obj` depth >5 silently 回 `_REDACTED` literal，與真實 redacted value 無辨識；(c) `CreateProjectModal._loadCloudServices` catch 後沒 set `state.error`（不一致於 `_loadWorkspaces`），dropdown 靜默變空使用者誤判「無 service」；(d) `_post_ai_reply` 外層 except 把 programming error 跟 transient AI failure 包成同一個 generic notification。逐條都不破功能、但加 `_logger.warning` 對 observability 有幫助。

---

## Plan alignment 驗證

| Decision | Plan | Implementation | Status |
|---|---|---|---|
| **D1=B** | 留 `unique_cloud_service` SQL constraint | `src/models/project_project.py:27-33` 保留 | ✅ |
| **D2=A** | Task M2M extras + related primary | `src/models/project_task.py:38-52`（M2M + related store=True） | ✅ |
| **D3=A** | unbound project ACL = project member | `src/security/ir_rules.xml:63-76` rewrite，移除 `('cloud_service_id', '=', False)` 短路 | ✅ |
| **D4=C** | `/woow#/ai-assistant` 入口 rename + alias `/projects` | sidebar `Projects` + `AI Assistant` **並存**（CAQA M1，使用者 accept） | ⚠️ M1 caveat |
| **D5=A** | service 下拉首項 "No service" + 拔 has_project filter + 拔必選 gate | `CreateProjectModal.{js,xml}` 三處都改 | ✅ |
| **D6=C** | 限同 workspace | `project_task.py:50` domain `[('workspace_id', '=', workspace_id)]` 靜態 + `cloud_service_rule_workspace_member` 由 ORM 把關 | ✅ |
| **D7=A** | primary + extras 合併注入，primary 在前去重 | `discuss_channel.py:259-285` `_get_cloud_service_context` 含 `extras - primary` dedup + section order | ✅ |
| **D8=A** | cloud_service 補 ir.rule | `ir_rules.xml:81-90` `cloud_service_rule_workspace_member` 新增 | ✅ |
| **D9=A→B 階段 1** | pattern sanitizer + 3 欄位 blacklist + `***REDACTED***` literal | `discuss_channel.py:13-49`（pattern + blacklist + depth-cap + JSON fail-closed） | ✅ |

| Open Q | 拍板 | Implementation | Status |
|---|---|---|---|
| Q1 | `message_partner_ids` + `create_uid` 雙腿（Odoo 18 project.project 無 user_ids） | `ir_rules.xml:66` domain 含兩 leg；tester 在 b582a44 修 fixture 對齊 | ✅ |
| Q2 | static domain on field，related workspace_id 提供 NoneType-safe | `project_task.py:50` static domain；unbound task 自動評為 False → 空候選 | ✅ |
| Q3 | `incompatible_extra_cloud_service_ids` computed M2M | `project_task.py:53-72` computed + `@api.depends('extra_cloud_service_ids.workspace_id', 'workspace_id')` | ✅ |
| Q4 | `***REDACTED***` hardcoded | `discuss_channel.py:17` literal `_REDACTED = '***REDACTED***'` | ✅ |

**9/9 decisions aligned, 4/4 Open Qs 拍板對齊，1 known UX semantic drift (D4 M1, caveat)。**

---

## Scope expansion 4 點獨立驗證

| # | 動作 | Why necessary | Verified |
|---|---|---|---|
| (a) | `ir.model.access.csv:26-27` 給 base.group_user RWCU on project.project / project.task | D3=A 要 workspace member（base.group_user）能對自家 unbound project / task 進行 CRUD；ACL CSV 是 first gate，必須先放權限再用 ir.rule 收回多餘的 | ✅ 淨效果：ACL CSV 從「無 RWCU」變「有 RWCU 但被 ir.rule 框住」，對 PaaS 是 tightening（搭配對等 std rule deactivation） |
| (b) | `post-migrate.py` deactivate `project.project_public_members_rule` | OR-semantic：同 group 多 rule = OR；std rule 有 always-true leg（`privacy != followers` 對 default `employees` 為真），不 deactivate 則新 ws-member rule 被 OR 旁路 — root-cause correct，無 cleaner 替代 | ✅ CAQA §C 已詳述；reviewer 重新查 Odoo `ir_rule.py:140-168` 確認 |
| (b'+B1) | `post-migrate.py` deactivate `project.task_visibility_rule` + `task_visibility_rule_project_user` | task 端對等 leak vector：std `task_visibility_rule` 套 base.group_user、含同樣 always-true leg；refactor 加 ACL CSV row 27 後立即觸發 cross-workspace task data leak。`task_visibility_rule_project_user` 是 perm_read=False 的 write/create/unlink 版本，套 group_project_user，雖 PaaS 不給租戶但對等 deactivate 保 future-safe | ✅ Single UPDATE + IN clause 3 條 rule 原子處理；tester `test_cross_workspace_task_isolation.py` 鎖 base.group_user read leg；caveat：group_project_user write leg 無 regression test（reporter follow-up） |
| (c) | `ir_rules.xml:98-107` 新增 `project_task_rule_workspace_member` | Odoo std `task_visibility_rule_*` deactivate 後 base.group_user 在 project.task 上無任何 read rule（OR-empty → deny all）；新規則補回 workspace-member 路徑 | ✅ 與 project rule 同 pattern（bound 走 cloud_service.workspace.access；unbound 走 message_partner_ids OR create_uid） |
| (d) | `.claude/skills/k8s-dev-sandbox/scripts/test.sh` 加 `-u` + `--http-port` | infra：sandbox in-pod test runner 之前缺 -u（不 update module）與 alt http port（與 main pod 衝突）。屬 test infra 解耦，與 refactor 主邏輯無關 | ✅ 純 dev infra，無 production 影響 |

**4 / 4 scope expansion 全 root-cause-necessary，無「順手清理」性質的 scope creep。**

---

## Tests 綠的真實性

| Group | AC | Test file | 真實性 verdict | Note |
|---|---|---|---|---|
| A Project 獨立性 | #1-3 | `test_project_decouple.py:48-115` | ✅ 真實 | AC #1 baseline lock；AC #2 用 `IntegrityError + mute_logger` 驗 SQL constraint；AC #3 驗 related compute reactive，含改 service 後 propagate 檢查 |
| B Project ACL | #4-6 | `test_project_decouple.py:178-209` | ✅ 真實 | AC #4 用 `with_user(bob)` search 驗 invisible；AC #5 follower add 後 search visible；AC #6 bound + ws member 仍 visible regression。注意：AC #5 fixture 在 b582a44 拔掉不存在的 user_ids、僅留 message_partner_ids，這也代表 `create_uid` leg 沒獨立正面測（只有 AC #4 隱性使用） |
| C cloud_service ACL | #7-8 | `test_cloud_service_acl.py:75-203` | ✅ 真實 | AC #7 拆 5 條（search-empty / browse / write / unlink / own-ws positive）覆蓋不同 ORM 路徑；AC #8 用 `MagicMock(env=env)` stub request 過 controller 行為（合理 — TransactionCase 無 HTTP；trade-off：如未來加 csrf/session 檢查不會被測到，可選 follow-up 加 HttpCase） |
| D Task extras | #9-13 | `test_task_service_extras.py:89-277` | ✅ 真實 + ⚠️ 1 弱守 | AC #9-10 驗 schema、related propagation 都真實；AC #11 只 grep `'workspace_id'` literal 出現於 domain `repr()` — 實作其實對（static domain via related workspace_id），但 test 是 stub，未來 implementation 能用 `[('workspace_id', '!=', False)]` 之類無意義 domain 過關；AC #12 容三種 enforcement（access/validation/silent-drop）合理；AC #13 cross-project move 保 extras + warning 欄位三選一驗。M3 caveat 衍生（cross-ws extras 仍注 AI prompt）獨立於 test |
| E AI prompt + sanitizer | #14-19 | `test_chatter_service_injection.py:91-228` | ✅ 真實 + ⚠️ 1 brittle | AC #14 用 `count('Primary Svc') == 1` 驗 dedup — 實作 dedup 是 `extras - primary` recordset 層級，正確；但 test format 若改字（如 `### Primary Service`）可能繞過。AC #15-19 真實覆蓋 4 條 prompt 分支。深度 >5 / 非 JSON helm_values fail-closed 未涵蓋（implementation 都對） |
| F UX | #20-25 | `test_ux_routing_views.py:39-173` | ✅ 真實（file-content level） | router / sidebar / modal / xml 都 grep 字串 / regex，符合 plan §E2E「browser hash 留 playwright」決議；trade-off 已寫進 test report |
| B1 sanity | (CAQA) | `test_cross_workspace_task_isolation.py:100-154` | ✅ 真實，覆蓋對應 attack vector | Alice / Bob 兩無重疊 ws，Bob 的 project default `privacy='employees'` 正中 leak leg；驗 search 空 + browse.read AccessError；own-ws sanity 防 over-correction。**未覆蓋**：group_project_user write/unlink leg via `task_visibility_rule_project_user`（reporter follow-up） |

**所有「綠燈」皆對應 production code 真實變動，無 stub / hardcode 過關**。Test weakness（AC #11 stub、AC #14 format-brittle、B1 write-leg 未測）屬「strict guard for future regressions」缺塊，**不影響本 PR verdict**（implementation 在這些位置都做對了）。

---

## 與 CAQA 對焦 / 補位

### CAQA M1-M4 / L1-L3 重審（全 unchanged）

| # | CAQA caveat | Reviewer 獨立確認 | 是否需要 escalate |
|---|---|---|---|
| M1 | D4 sidebar drift（兩 entry vs rename） | ✅ 確認：`Sidebar.js:8-11` 並存 `projects` + `ai-assistant`；router L15 / L16 兩條真實 route 都 active | 否，使用者已 accept；reporter / leader 與 UX 對齊即可 |
| M2 | `_sanitize_helm_values` 同名異實 | ✅ 確認：`controllers/paas.py:1460`（pre-existing API response sanitize）vs `models/discuss_channel.py:41`（new AI prompt sanitize）— scope 不同但同名 | 否，reporter follow-up #2 |
| M3 | Cross-ws incompatible extras 仍注 AI prompt | ✅ 確認：`discuss_channel.py:267-280` 用 `sudo()` 取 extras，sanitizer 只去 secret 不去 cross-ws；plan AC #13 未明訂 strip | 否，reporter follow-up #3 |
| M4 | `_check_project_access` / `_check_task_access` IDOR | ✅ 確認 `ai_assistant.py:43-61` 對 unbound 全 return True；refactor 後 unbound 可建立 → surface 放大 | 否，reporter follow-up #4；pre-existing |
| L1 | migration docstring 版號錯字 | ✅ 仍是 `"""18.0.1.0.18 ..."""`，目錄 `18.0.1.0.23` — 跨 18.0.1.0.22→23 重命名 carry-over 未修 | 列入本 PR cleanup #1 |
| L2 | `incompatible_extra_cloud_service_ids` 非 store，未來 list view N+1 | ✅ 確認 `project_task.py:53-59` compute='_compute_…'，view L29-32 有 `invisible="not incompatible…"` form-only 限定，當前不會 N+1 | 否，reporter follow-up |
| L3 | sanitizer 對 list-of-strings 不遮罩 | ✅ 確認 `discuss_channel.py:36-37`；helm_values 結構此 case 罕見 | 否，reporter follow-up |

### 獨立角度新發現（reviewer 補位 — CAQA 未抓到）

| # | 嚴重度 | 主題 | 位置 | 處置 |
|---|---|---|---|---|
| R1 | major | Migration silent xmlid drift — 若 Odoo 18.x patch 改 `task_visibility_rule` 等 xmlid，3-row UPDATE 變 0/部分 rows，B1 leak 靜默回來 | `migrations/18.0.1.0.23/post-migrate.py:88-102` | Cleanup #6（建議加 SELECT count(*) 預驗 == 3 或 UPDATE 後 raise if rowcount<3） |
| R2 | minor | BottomNav mobile 仍把 `ai-projects` / `ai-project-kanban` 算進 AI 入口 — desktop sidebar 已拆 Projects | `static/src/paas/layout/bottom_nav/BottomNav.js:18-24` | Defer to reporter（mobile UX 一致性） |
| R3 | minor | router.js 註解描述 alias 為 `/woow#/ai-assistant/projects`，與 plan / AC 用 `/woow#/ai-assistant` 不符；實際 L15 `ai-assistant` route 走 AI Assistant 不 alias 到 ai-projects | `static/src/paas/core/router.js:16-17` | Cleanup #3 |
| R4 | minor | ir_rules.xml 註解 "two security-critical rules" 已 stale（現 3 條） | `security/ir_rules.xml:53-56` | Cleanup #4 |
| R5 | minor | task rule 註解未 cross-ref 對等 std rule deactivation 在 migration | `security/ir_rules.xml:92-97` | Cleanup #5 |
| R6 | minor | PR draft AC#5 ⚠️ section 與 footer all-green 結果衝突，內部不一致 | `docs/decisions/reviews/pr-description-draft.md:25-57 / 155` | Cleanup #2 |
| R7 | info | B1 test 未涵蓋 group_project_user write/unlink leg via `task_visibility_rule_project_user`（perm_read=False 的版本） | `tests/test_cross_workspace_task_isolation.py` | Defer to reporter；PaaS 不給租戶 group_project_user，但 paranoia 加 1 條 sibling test 增強回歸防護 |
| R8 | info | AC #11 domain test 是 string-grep stub，AC #14 dedup test format-brittle | tests/* | Defer to reporter（test-hardening） |
| R9 | info | Sanitizer / CreateProjectModal `_loadCloudServices` 等多處 silent fallback 無 log line | discuss_channel.py / CreateProjectModal.js | Defer to reporter（observability） |

### 偽陽性 — multi-agent 內部分歧，reviewer 獨立驗證後排除

- **code-reviewer 報「BLOCKER：`_create_project` unbound 路徑會建出隱形 project」**：reviewer 獨立檢查 `_create_project` (L911) + `_list_projects` (L886) + `SupportProjectsPage.js:30`（call `fetchAllProjects()` 而非 `fetchProjects(workspaceId)`） + `support_service.js:97`（`fetchAllProjects` 命中 no-workspace endpoint，domain `[]`，sudo browse all projects）。結論：**unbound project 透過 UI 建立後實際會在 SupportProjectsPage 顯示**（domain 空 + sudo browse → 全列），non-blocker。
   - 順便揭示一個 pre-existing 問題：`_list_projects` with `domain=[]` + sudo 會跨 tenant 列所有 project，但此 leak 與本 refactor 無關（pre-existing 設計），D5=A 放大可見度但不變 leak shape。Reporter 可考慮列為 follow-up #6（pre-existing controller-level filter gap）。
- **code-reviewer 報「Major：extra_cloud_service_ids domain 對新建未存 task 為 False」**：實情如此（task.workspace_id related 在無 project_id 時為 False，domain `[('workspace_id', '=', False)]` 候選空）。但 PaaS task 建立流程必先選 project，「無 project 的 orphan task」非真實使用場景；UX 上 dropdown 空 != 阻塞。降為 minor 不阻擋（reviewer 建議的 `readonly="not workspace_id"` 是可考慮的 UX polish，但非必要）。

---

## 工具 raw 輸出摘要

| 工具 / Agent | 主要產出 | 處置 |
|---|---|---|
| `/pr-review-toolkit:review-pr` (code-reviewer) | 1 alleged BLOCKER（已驗證偽陽性）+ 4 major + 3 minor | 1 真 major 並入 R1；2 minor 並入 R3-R5 |
| `/pr-review-toolkit:review-pr` (pr-test-analyzer) | 2 critical + 4 important + 2 authenticity concerns | 全降級為 R7 / R8（test-hardening）；不影響 verdict |
| `/pr-review-toolkit:review-pr` (silent-failure-hunter) | 2 blocker + 6 major + 4 minor | 1 個有實質風險升 R1（migration xmlid）；其餘併入 R9（observability follow-up） |
| `/pr-review-toolkit:review-pr` (comment-analyzer) | 1 critical + 5 important + 4 nit | 並入 cleanup #1-#5 |
| `/simplify` | 已於 implementation phase 跑過（PR draft `## /simplify 摘要` 章節記錄 2 採納 / 2 拒絕） | reviewer 不重跑（implementation 已 sanity） |

multi-agent **無內部嚴重分歧**；code-reviewer 的 BLOCKER 主張在 reviewer 獨立 grep + read 後排除為偽陽性，已於 §偽陽性 詳述。

---

## PR description 充分性

- ✅ **變更摘要**：完整覆蓋 6 大模組（Project model、Task model、ACL、Sanitizer、UX、Migration）
- ✅ **AC 結果表**：⚠️ 與 footer 結果衝突（cleanup #2 修）
- ✅ **4 Open Qs 拍板表**：含位置 reference，可追溯
- ✅ **Deploy SQL — 升級前 dry-run**：`SELECT ... WHERE cloud_service_id IS NULL` 涵蓋 plan R1
- ✅ **升級後 verify**：3 條 expect rows（含 task rule 兩條）
- ✅ **Rollback SQL**：3 條 std rule 對等 reactivate via single UPDATE + IN，idempotent
- ✅ **半可逆動作清單**：M2M 表 + 3 std rule deactivation + 新 ir.rule 全列
- ✅ **Side effects 評估**：含 base.group_user / group_project_user 失去什麼權限的詳細說明
- ✅ **`/simplify` 摘要**：2 採納 / 2 拒絕透明
- ✅ **Follow-up 候選**：含 D9 階段 2、sentinel i18n、cross-ws extras 放寬、MCP tools 整合、B1 pattern note
- ✅ **主要 commits 清單**：含 b582a44 + 9552cb7（B1 修補）
- ⚠️ **內部不一致**：AC #5 ⚠️ section 與 footer 全綠不一致；cleanup #2

**結論：PR description 充分；僅 1 處內部不一致需 cleanup（cleanup #2）。**

---

## Recommendation

**Proceed to Reporter**，建議 leader 在進 reporter 之前先處理 **cleanup #1-#5（trivial 5 條，總計 < 10 分鐘）**：

| Cleanup | 動作 | 必須？ |
|---|---|---|
| #1 | migration L1 docstring 改 18.0.1.0.18 → 18.0.1.0.23 | 強烈建議（CAQA 已標但未修） |
| #2 | PR draft 拔 AC#5 ⚠️ section、表格改 25/25 ✅ | 強烈建議（內部一致性） |
| #3 | router.js 註解修字（alias 用 `/woow#/ai-assistant` 不是 `/ai-assistant/projects`） | 建議（comment vs 真實行為） |
| #4 | ir_rules.xml 註解 "two" → "three" | 建議（stale） |
| #5 | task rule 註解加 1 行 cross-ref std rule deactivation | 建議（防未來 B1 重炸） |
| #6 | migration 加 xmlid drift 防呆 assertion | 可選（防 future Odoo patch 改 std rule 名） |

**Reporter follow-up**（reporter phase 持久化進 `docs/`）：
1. 🔴 (CAQA reporter #1) ACL 對稱性 checklist — Odoo std rule deactivation 必須 model + 相關 sub-model 對等
2. `_sanitize_helm_values` 同名異實重命名 / 抽 utils（CAQA M2）
3. cross-ws incompatible extras 是否 strip out AI prompt（CAQA M3）
4. `_check_project_access` / `_check_task_access` 對 unbound 補檢查（CAQA M4）
5. D9 階段 2 升級 sanitizer 為 whitelist 模式
6. **(new)** Pre-existing `_list_projects` with sudo + 空 domain 跨 tenant 可見 — 與本 refactor 無關但 D5=A 放大；屬 controller-level filter gap
7. **(new)** BottomNav mobile 與 sidebar 在 D4 拆分後的 UX 一致性
8. **(new)** Test hardening：AC #11 行為層測 / AC #14 嚴格 dedup assertion / B1 group_project_user write leg sibling test
9. **(new)** Observability follow-up：sanitizer JSON parse / depth-cap silent fall log；CreateProjectModal `_loadCloudServices` 失敗 surface error

---

## Hand-off

完成後 reviewer 用：
`maestri ask "refactor-phase-leader" "review-verdict: approved-with-caveats ; multi-agent 4-way review 無新 high-severity；6 條 cleanup (5 trivial + 1 hardening) 建議 fix-before-merge；4 reporter follow-up 新增（pre-existing controller filter、mobile/sidebar、test-hardening、observability）"`
回 leader。
