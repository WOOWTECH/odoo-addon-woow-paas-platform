---
name: 2026-05-28-decuple-project-deliverables
phase: reporter (final addendum after F-DP-2 + Task.sudo unify)
team: decuple-project (feature agent pod)
reporter: decuple-project-reporter
scope: cycle 6 final deliverables — docs 7 件齊備 + commit message draft (5 code + 1 docs = 6 commits) + PR description draft + leader hand-off 清單
branch: fix/decuple-project
plan_ref: docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
test_report_ref: docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md
caqa_ref: docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md
reviewer_ref: docs/tracking/reviewer-report-2026-05-28-decuple-project-frontend.md
status: draft — leader 跟 user 對焦後執行 commit / push / open PR
created: 2026-05-28
updated: 2026-05-28 (final addendum — F-DP-2 close + Task.sudo unify + CLAUDE.md addendum)
---

# Cycle 6 Final Deliverables — Decouple Project Frontend + Controller 收尾 (含 F-DP-2 + Task.sudo unify)

> 本 doc 是 reporter phase 給 leader 帶給 user 對焦的最終 deliverables 包。**Cycle 6 走完 phase 序列 + F-DP-2 mini cycle + Reporter 兩輪沉澱**（cycle 6 initial reporter → CLAUDE.md addendum → F-DP-2 final addendum）。**Reporter 不執行** `git add` / `git commit` / `git push` / `gh pr create`（per task brief 嚴守紀律），等 leader 跟 user 對焦後執行。

---

## A. 7 件持久化 doc 路徑清單 + Final addendum 變化

| # | Doc | 路徑 | 本輪 final addendum 變化 |
|---|-----|------|------------------------|
| 1 | follow-ups.md（cycle 6 state line + F-DP-2 status + cross-cutting hint）| `docs/tracking/followups/follow-ups.md` | F-DP-2 改 ✅ CLOSED；State line 更新「accumulated 8 改動點 + 1 unify refactor + 15 test/TC」；cross-cutting hint 標 F-DP-2 closed |
| 2 | §Consequences cross-link + §See also 補連結 | `docs/decisions/refactor/cloud-service-project-decouple.md` | B1 family closure 從 2/4 → 4/5（80%）；剩 F-DP-1 唯一 P1 follow-up |
| 3 | ORM-vs-endpoint pattern doc | `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` | **大幅升級**：anti-pattern 三變體 family（A/B/C）+ perf justification 章節 + Why 變體 C 不只是 perf trade-off + 5 instance critical mass table（80% closure） |
| 4 | Phase-leader-brief-as-pointer pattern doc | `docs/decisions/patterns/phase-leader-brief-as-pointer-not-source.md` | 無變化（pattern 本質與 F-DP-2 無關）|
| 5a | Decouple-project frontend worktree-dev pitfalls（3 條 dev-env）| `docs/tracking/pitfalls/decouple-project-frontend-worktree-dev-pitfalls.md` | 無變化 |
| 5b | ORM-test-green endpoint-leak-blindness pitfall | `docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md` | **補三變體 family 章節**：cycle 6 timeline 從 9-step → 13-step（補 F-DP-2 step 10-13）+ Anti-pattern 三變體 family + 變體 C 的特殊性（不會 fail test、純 pattern 立論 audit catch）|
| 6 | CLAUDE.md（§Known patterns + Pitfall section addendum）| `CLAUDE.md` | 前一輪 addendum 已 land 3 pointer（2 patterns + 1 pitfall）；本輪不改（addendum 已對齊本 final state）|
| 7 | 本 deliverables doc（commit + PR draft + retro）| `docs/tracking/reports/2026-05-28-decuple-project-deliverables.md` | **本輪重生**：commit message draft 5 code + 1 docs = 6 commits / PR description 數字更新 8 / 15 / 3 / 含 CLAUDE.md addendum 處理 |

---

## B. Commit message draft — final addendum

### Commit 粒度拍板：**Option II (5 code commits + 1 docs commit = 6 commits)**

**理由（vs Option I/III/IV）**：

| Option | 描述 | 評估 |
|--------|------|------|
| **(I)** 6 code commits（F-DP-2 fix + Task.sudo unify 各自獨立）| 最 bisect-friendly | ❌ Task.sudo unify 是 1 行 + 4 行 comment，分開 commit overengineered；且兩條都在同 method 同 diff context，分開違反 cohesion |
| **(II) Reporter 推薦** 5 code commits（原 4 + F-DP-2 fix 含 Task.sudo unify）+ 1 docs commit | 對齊 PR #75 4-commit TDD idiom + 加 cycle 6 final addendum 1 commit + docs 統一 1 commit | ✅ 最對齊 git log idiom + 同 method 改動 cohesion |
| **(III)** 5 commits（fix-class 合成 1 + F-DP-2 + Task.sudo unify scope-by-domain）| commit history readability | ❌ 合成 fix 失去 individual fix 的 bisect 能力；scope-by-domain 切會把 D2=A subscribe 切離 feat（feat 完整性破壞）|
| **(IV)** 2 commits（implementation 建議：fix + refactor）| commit log 最簡 | ❌ feat / fix / test / docs 全混 → bisect 完全失效；違反專案 git log idiom（每個 PR 多顆 commit 各自 prefix type） |

**Option II 具體拆分**（5 code + 1 docs）：

| # | Type | Subject |
|---|------|---------|
| 1 | feat | feat: allow unbound project creation from frontend modal (D1+D2+D3) |
| 2 | fix | fix: drop sudo in _list_projects to walk ir.rule dual-leg (TC4 follow-on, 變體 A) |
| 3 | fix | fix: drop sudo in _list_tasks to walk ir.rule dual-leg (CAQA A1, 變體 A) |
| 4 | fix | fix: api_support_stats drop sudo + Task.sudo unify (F-DP-2, 變體 B + C) |
| 5 | test | test: cover unbound project create + cross-tenant ACL + own-data stats endpoint |
| 6 | docs | docs: decouple project frontend cycle 6 reporter takeaways + CLAUDE.md addendum |

### 6 commits 草稿全文

#### Commit 1 — feat: 允許 unbound project 從前端 modal 建立（D1+D2+D3）

```
feat: allow unbound project creation from frontend modal

Plan §Decision Sign-off D1=A + D2=A + D3=A signed-off 2026-05-28.

Frontend (CreateProjectModal + support_service):
- CreateProjectModal.js:117-160 移除 !workspaceId hard guard + 三分支
  dispatch (cs / ws / unbound)；onWorkspaceChange 清空 cloudServiceId
  避免 stale
- CreateProjectModal.xml:40-63 workspace label 改為 "Workspace
  (optional)"；first option "— No workspace — (Personal project)"
- CreateProjectModal.xml:66 維持 t-if="state.workspaceId"
  （D3=A：無 workspace 隱藏 cloud_service dropdown）
- support_service.js:267-292 新增 createUnboundProject(data) method
  走 POST /api/support/projects（無 workspace / 無 cloud_service path）

Controller (ai_assistant.py):
- :1037-1038 移除 dispatcher 完全 unbound 拒絕 guard（D1=A）
- :1230-1238 _create_project sudo().create() 後加 sudo().message_subscribe
  (partner_ids=[request.env.user.partner_id.id])（D2=A 補 follower
  unbound leg，與 _check_project_access follower-only idiom 對齊）

Backend model + ir.rule 不動（cycle 1 cloud-service-project-decouple
refactor 已 ready dual-leg domain；本 commit 只動 controller 收尾 +
frontend UX）。

Related:
- docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
- docs/decisions/refactor/cloud-service-project-decouple.md §Invariants #5
- docs/decisions/patterns/paas-workspace-member-acl-tightening.md (dual-leg 對齊)
- docs/decisions/patterns/constraint-input-trust-expansion.md (subscribe authority server-authoritative)
```

#### Commit 2 — fix: `_list_projects` drop sudo（TC4 follow-on，變體 A over-access）

```
fix: drop sudo in _list_projects to walk ir.rule dual-leg

Playwright TC4 跨 user ACL 驗證時，Bob 切到 /woow#/projects 看到 Alice
建的 unbound project — root cause 是 _list_projects line 1186 用
.sudo().search(domain) 繞 project_rule_workspace_member ir.rule。
bare-URL POST /api/support/projects {action: 'list'} 在 domain=[]
時走 sudo()，回 全 db 所有 workspace 所有 user 的 project →
cross-tenant data leak（[[cross-workspace-task-data-leak]] B1 family
在 controller layer 的第 1 instance，anti-pattern 變體 A over-access）。

Fix:
- ai_assistant.py:1186-1190 drop .sudo() + 4 行 ir.rule walk 註解
  說明 dual-leg（bound via workspace member / unbound via follower
  or creator）+ bare-URL leak warning

ir.rule project_rule_workspace_member (ir_rules.xml:65-78) dual-leg
domain 已 ready；本 fix 讓 controller 真的 walk 它而非繞它。

Related:
- docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md (新 pattern, 變體 A)
- docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md
```

#### Commit 3 — fix: `_list_tasks` drop sudo（CAQA BLOCKER A1 close，變體 A）

```
fix: drop sudo in _list_tasks to walk ir.rule dual-leg (CAQA A1)

CAQA round 1 同源 sudo bypass scan 抓到 _list_tasks (line 1325) 與
_list_projects (line 1186) 完全同源 — 同 controller、同 endpoint family
(/api/support/tasks 與 /api/support/projects 對稱)、同 attack vector
(bare-URL POST + domain=[] sudo bypass)、同 ir.rule baseline
(project_task_rule_workspace_member dual-leg 已 ready)。

不修 = root-cause-correct closure 不完整；BLOCKER A1 阻 reviewer phase
進。

Fix:
- ai_assistant.py:1325-1329 drop .sudo() + 4 行 ir.rule walk 註解（完全
  鏡像 _list_projects fix shape）

ir.rule project_task_rule_workspace_member (ir_rules.xml:108-117)
dual-leg domain 已 ready：
  bound leg via project_id.cloud_service_ids.workspace_id.access_ids
  unbound leg via project_id.message_partner_ids OR project_id.create_uid

[[cross-workspace-task-data-leak]] B1 family controller layer 第 2
instance close（_list_projects close 是第 1 instance）；剩
_get_project_stages + api_support_stats 是 follow-up（F-DP-1 / F-DP-2）。

Related:
- docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md §1.1 BLOCKER A1
```

#### Commit 4 — fix: `api_support_stats` drop sudo + Task.sudo unify（F-DP-2，變體 B + C close）

```
fix: api_support_stats drop sudo + Task.sudo unify (F-DP-2)

User 拍板帶 F-DP-2 進本 PR。本 commit 同時 close anti-pattern family
變體 B (under-access) + 變體 C (same-method 半套狀態)，是 cycle 6
[[orm-vs-endpoint-acl-test-coverage]] 5 instance critical mass 的最後
2 個 close instance（family 在 controller layer 80% closure，剩
_get_project_stages F-DP-1 需 design）。

Fix landed:
- ai_assistant.py:972-978 drop sudo + 改 domain=[] 走 ir.rule dual-leg
  (project_rule_workspace_member)：bound (workspace member) + unbound
  (follower / creator) 兩條 leg 同時 walk →
  unbound project tasks 重新算入 stats（變體 B under-access close）
- ai_assistant.py:980-986 drop Task.sudo() unify：accessible_projects.ids
  已 ir.rule filter，task ir.rule (project_task_rule_workspace_member)
  透過 project_id walk 對等驗證（caqa mini re-verdict §2 對等性驗證），
  drop 後結果集對等且關閉 same-method 半套 anti-pattern（變體 C close）

註解設計（互引讓 future reader 看到「為什麼這 method 有兩段 sudo 都被
拔掉」一目了然）：
- :972-977 解釋變體 B vs A 對稱關係（under-access vs over-access）
- :980-983 解釋變體 C 為何需要 unify（perf-safe ≠ pattern-safe）

CAQA mini re-verdict APPROVED WITH CAVEATS (不變)；reviewer phase
略過 per leader+user 拍板。

Related:
- docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md §Mini Re-verdict
- docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md (變體 B + C critical mass)
- docs/tracking/followups/follow-ups.md F-DP-2 CLOSED status
```

#### Commit 5 — test: T1-T5 endpoint test（unbound + cross-tenant ACL + own-data stats）

```
test: cover unbound project create + cross-tenant ACL + own-data stats endpoint

新增 T1-T5 HttpCase endpoint test 守 controller layer ACL（補 backend
ORM test B1-B5 沒守的 endpoint layer，per
[[orm-vs-endpoint-acl-test-coverage]] pattern）。

T1: unbound project create + creator visibility (D1=A + D2=A
    mechanism-coupled — 鎖 message_subscribe 補 follower unbound leg)
T2: cross-user deny visibility (D2=A outcome-coupled — mechanism-
    agnostic; 未來 D2 切 D2=B 仍綠)
T3: _list_projects bare-URL 不 cross-tenant leak (TC4 follow-on
    fix 的 endpoint regression lock，變體 A)
T4: _list_tasks bare-URL 不 cross-tenant leak (CAQA A1 fix 的
    endpoint regression lock；shape 完全對稱 T3，變體 A)
T5: api_support_stats Alice own-data visibility >= 2 + Bob bare-stats
    = 0 反向 false-green 守（F-DP-2 fix regression lock，變體 B）

設計鎖：
- subset (⊆) 而非 strict equality 避免 baseline DB row 噪訊
- intersection check (assertFalse(alice_pids & bob_returned_ids))
  直接鎖跨用戶 leak
- 雙向同時鎖（owner-survive + cross-user-deny + over-correction
  防火牆）
- T5 用 assertGreaterEqual 避免 demo-data baseline coupling 但夠鎖
  own-data visibility
- T5 用 Bob bare-stats=0 反向斷言守 fix 過頭 drop user.id leg 整條變
  all-tasks (從變體 B 變變體 A 的 false-green 攻擊面)

@tagged('post_install', '-at_install')；uuid-suffix login fixture；
Alice + Bob 跨 user ACL fixture。

Related:
- src/tests/__init__.py 加 test module wire
- docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md
- docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md (T1-T5 對應變體 A/B 守線)
```

#### Commit 6 — docs: cycle 6 reporter takeaways + CLAUDE.md addendum

```
docs: decouple project frontend cycle 6 reporter takeaways + CLAUDE.md addendum

Cycle 6 完整 phase 序列（plan + tester + impl + caqa + reviewer +
reporter + F-DP-2 mini cycle）走完後的 durable 沉澱。

Pattern docs (new):
- docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
  controller 不可 sudo() 繞 ir.rule；ORM-level test 綠 ≠ endpoint
  ACL 安全。Anti-pattern 三變體 family（A over-access / B under-access
  / C same-method 半套狀態）+ 5 instance critical mass（cycle 6 close
  4 個）+ perf justification 章節（變體 C 為何不只 perf trade-off）
- docs/decisions/patterns/phase-leader-brief-as-pointer-not-source.md
  Brief 是導讀不是 source-of-truth；agent 接 brief 撞到與 plan / decision
  doc invariant / schema 不符時 trust source + 回報 leader 校正

Pitfall docs (new):
- docs/tracking/pitfalls/decouple-project-frontend-worktree-dev-pitfalls.md
  3 條 worktree dev-env 教訓（--http-port 避 8069 / OWL asset rebuild
  / docker compose restart）
- docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md
  Plan §1.C「後端已完整支援」掃描盲區 + backend ORM test 全綠 → 遮蔽
  controller layer sudo bypass；含 13-step cycle 6 timeline 與三變體
  family narrative

Decision doc updates:
- docs/decisions/refactor/cloud-service-project-decouple.md
  §Consequences 加 Cycle 6 follow-on closure 段（B1 family controller
  layer 4/5 instance closed）+ §See also 補 cycle 6 inputs

Follow-ups:
- docs/tracking/followups/follow-ups.md
  Cycle 6 state line + Index F-DP-1~F-DP-8 8 條（F-DP-2 CLOSED in
  PR；F-DP-1 P1 design follow-up；F-DP-3~F-DP-8 trivial polish follow-up）

CAQA + reviewer + test reports + plan landed:
- docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
- docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md
- docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md (含 mini re-verdict §717-901)
- docs/tracking/reviewer-report-2026-05-28-decuple-project-frontend.md
- docs/tracking/reports/2026-05-28-decuple-project-deliverables.md (本 deliverables doc)

CLAUDE.md addendum (cycle 6 final addendum 已 land):
- §Known patterns 補 [[orm-vs-endpoint-acl-test-coverage]] pointer
- §Known patterns 補 [[phase-leader-brief-as-pointer-not-source]] pointer
  (標明適用 maestri 多 phase pod 工作流；非 paas-platform 直接相關)
- Pitfall 持久化紀錄 補 [[orm-test-green-endpoint-leak-blindness]] pointer
```

---

## C. PR description draft — final addendum

### Title

```
fix: 允許 unbound project 從前端 modal 建立 + 收口 cross-workspace task leak family 4 instance（cycle 6 frontend + controller 收尾，含 F-DP-2）
```

### Target branch

`develop`（per CLAUDE.md git convention + git log idiom）

### Body（zh-tw 繁體中文，per CLAUDE.md git convention）

```markdown
## Summary

Cycle 6 — Decouple Project Frontend + Controller 收尾（plan A+A+A signed-off 2026-05-28）。本 PR 完成 8 改動點 + 1 unify refactor + 順手 close 4 個 `cross-workspace-task-data-leak` B1 family 在 controller layer 的 sudo bypass instance（anti-pattern 三變體 A/B/C 全 cover），補 T1-T5 HttpCase endpoint test 守 controller layer ACL。

- **D1=A**：允許完全 unbound project（無 workspace + 無 cloud_service）
- **D2=A**：controller create 後 `message_subscribe` self user 為 follower，補 ir.rule unbound leg
- **D3=A**：modal 無 workspace 狀態隱藏 cloud_service dropdown，不暴露 cross-workspace 語意

Cycle 6 收口同 controller 內 same-family sudo bypass 4 instance：playwright TC4 catch `_list_projects` leak → CAQA grep 同源 scan catch `_list_tasks` BLOCKER A1 → 1 行 fix 對稱 close → CAQA round 2 第三輪 scan 撈出 `api_support_stats` 漏 unbound leg → user 拍板帶 F-DP-2 進本 PR → tester 補 T5 + impl drop sudo + Task.sudo unify（同 method 變體 B + C 同期 close）。

## 8 改動點 + 1 unify refactor 明細

### Implementation（5 modified files + 1 new test file）

| 檔案 | 行範圍 | 動作 | Anti-pattern 變體 |
|------|--------|------|------------------|
| `src/controllers/ai_assistant.py` | 1037-1038 | **D1=A**：移除 dispatcher 完全 unbound 拒絕 guard | — |
| `src/controllers/ai_assistant.py` | 1186-1190 | **TC4 follow-on**：`_list_projects` drop sudo + 4 行 ir.rule walk 註解 | A: over-access |
| `src/controllers/ai_assistant.py` | 1230-1238 | **D2=A**：`sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])` + 5 行註解 | — |
| `src/controllers/ai_assistant.py` | 1325-1329 | **CAQA BLOCKER A1**：`_list_tasks` drop sudo + 4 行 ir.rule walk 註解（鏡像 T3 fix shape） | A: over-access |
| `src/controllers/ai_assistant.py` | 972-978 | **F-DP-2**：`api_support_stats` drop sudo + 改 `domain=[]` 走 ir.rule dual-leg | B: under-access |
| `src/controllers/ai_assistant.py` | 980-986 | **Task.sudo unify**：drop Task.sudo() + comment 補述 unify 對等性 | C: same-method 半套 |
| `src/static/src/paas/components/modal/CreateProjectModal.js` | 117-160 | 移 `!workspaceId` hard guard + 三分支 dispatch（cs / ws / unbound） | — |
| `src/static/src/paas/components/modal/CreateProjectModal.xml` | 40, 54-57 | label `Workspace (optional)` + first option `— No workspace — (Personal project)` | — |
| `src/static/src/paas/services/support_service.js` | 267-292 | 新增 `createUnboundProject(data)` method（POST `/api/support/projects` 無 path 參數） | — |

### Test（new file）

| 檔案 | 內容 |
|------|------|
| `src/tests/test_project_decouple_frontend_endpoint.py` | T1-T5 HttpCase；`@tagged('post_install', '-at_install')`；uuid-suffix login fixture；Alice + Bob 跨 user ACL（1014 行） |
| `src/tests/__init__.py:45-46` | wire new test module |

## 15 條 test/TC 全綠

- ✅ **T1-T5 endpoint** (HttpCase) — 新增覆蓋 controller layer ACL + own-data visibility
  - T1: unbound create + creator visibility (D1=A + D2=A mechanism-coupled)
  - T2: cross-user deny visibility (D2=A outcome-coupled mechanism-agnostic)
  - T3: `_list_projects` bare-URL 不 cross-tenant leak (TC4 follow-on regression lock, 變體 A)
  - T4: `_list_tasks` bare-URL 不 cross-tenant leak (CAQA A1 regression lock, 變體 A)
  - T5: `api_support_stats` Alice own-data visibility ≥ 2 + Bob bare-stats = 0 反向 false-green (F-DP-2 regression lock, 變體 B + 防 over-correct 變體 A)
- ✅ **B1-B5 backend baseline** (test_project_decouple.py)
  - B1: cross-workspace task isolation regression
  - B2: bound project workspace_id unchanged
  - B3-B5: 既有 unbound create + ACL
- ✅ **Playwright TC1-TC5 UI** (browser-driven)
  - TC1: 完全 unbound create
  - TC2: select workspace + 無 service
  - TC3: bound regression（ServiceDetailPage 入口）
  - TC4: 跨 user ACL 隔離（catch `_list_projects` leak → 觸發 TC4 follow-on fix）
  - TC5: modal UI 樣態（D3=A cloud_service dropdown hide）

## 3 caveats + reporter follow-up

CAQA mini re-verdict APPROVED WITH CAVEATS（不變）標 3 條 caveat（F-DP-2 已 close），**reporter 已開 follow-up entries**（see [[follow-ups]] cycle 6 段）：

- **CAVEAT #2 → F-DP-1** `_get_project_stages` (line 1046, 1139-1177) 雙重洞（read leak + write surface via fallback）— pre-existing；`cross-workspace-task-data-leak` family 剩 1 instance；medium severity；需 design（移 `_ensure_default_stages` 到 explicit write endpoint）
- **CAVEAT #4 → F-DP-3** `_check_project_access` follower-only 與 ir.rule unbound dual sub-leg 非對等鏡像 — D2=A 設計副效應；info；docstring 補述
- **Caveat-arch1 → F-DP-4** Frontend 三分支 dispatch 在 component `onSubmit` — rule of three，第 2 caller 出現再抽

`cross-workspace-task-data-leak` B1 family 在 controller layer 進度（**5 instance 4 closed = 80%**）：
- ✅ `_list_projects` (本 PR TC4 follow-on close, 變體 A)
- ✅ `_list_tasks` (本 PR CAQA BLOCKER A1 close, 變體 A)
- ✅ `api_support_stats` accessible_projects (本 PR F-DP-2 close, 變體 B)
- ✅ `api_support_stats` Task.sudo unify (本 PR 同期, 變體 C)
- ⏸ `_get_project_stages` (F-DP-1 P1 follow-up, 需 design)

## Cross-link

- [[2026-05-28-decouple-project-from-workspace-frontend-plan]] — plan signed-off A+A+A
- [[cloud-service-project-decouple]] §Consequences — cycle 6 follow-on closure 已補 cross-link（B1 family closure 4/5 instance）
- [[orm-vs-endpoint-acl-test-coverage]] — **新 pattern doc，cycle 6 driver**（升級為 5 instance critical mass + 三變體 family + perf justification）
- [[phase-leader-brief-as-pointer-not-source]] — 新 pattern doc（maestri leader brief drift 紀律）
- [[decouple-project-frontend-worktree-dev-pitfalls]] — 新 pitfall（3 條 dev-env 教訓）
- [[orm-test-green-endpoint-leak-blindness]] — 新 pitfall（plan + test scope blindness，含三變體 family narrative）
- [[follow-ups]] cycle 6 段（8 條 F-DP-1~F-DP-8；F-DP-2 ✅ CLOSED in PR）
- CLAUDE.md §Known patterns — 已補 3 條 pointer（cycle 6 final addendum）

## Test plan

- [x] Backend `-u woow_paas_platform --test-enable` full suite green（15/15 全綠 leader 確認）
- [x] Playwright TC1-TC5 browser-driven 全綠
- [x] CAQA round 2 + Mini Re-verdict APPROVED WITH CAVEATS（0 blocker / 3 caveats / 5 info / 2 OOS follow-up）
- [x] Reviewer 4-agent verdict 無方向性分歧 APPROVE WITH CAVEATS（initial round）；F-DP-2 mini cycle **reviewer 略過** per leader+user 拍板
- [ ] Merge develop 後 deploy stg 驗 1 次 frontend modal unbound create + cross-user visible 規律
```

---

## D. 整體 Deliverables 清單（給 leader 帶給 user 對焦）

### D.1 Code branch

- **Branch**：`fix/decuple-project`（已存在，local-only）
- **狀態**：5 modified + 1 untracked test file + 11 untracked docs，**未 commit / 未 push / 未 PR**
- **Diff stat**：`ai_assistant.py +30 -10` + frontend 改動 + `+1014 行` new test file（per `git diff --stat`）

### D.2 Docs landed（7 件）

- ✅ `docs/tracking/followups/follow-ups.md` — append cycle 6 state + Index 8 rows + 8 詳細 entries + F-DP-2 ✅ CLOSED status
- ✅ `docs/decisions/refactor/cloud-service-project-decouple.md` — §Consequences `Cycle 6 follow-on closure` 段（B1 family 4/5 closed）+ §See also 補 cycle 6 inputs
- ✅ `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` — 新 pattern doc（5 instance critical mass + 三變體 family + perf justification）
- ✅ `docs/decisions/patterns/phase-leader-brief-as-pointer-not-source.md` — 新 pattern doc（maestri brief drift 紀律）
- ✅ `docs/tracking/pitfalls/decouple-project-frontend-worktree-dev-pitfalls.md` — 新 pitfall（3 條 dev-env）
- ✅ `docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md` — 新 pitfall（plan + test scope blindness + 13-step cycle 6 timeline + 三變體 family 章節）
- ✅ `CLAUDE.md` — §Known patterns 補 3 pointer（前一輪 addendum landed）
- ✅ `docs/tracking/reports/2026-05-28-decuple-project-deliverables.md` — 本 deliverables doc（commit + PR draft 全文 + retro 觀察）

### D.3 Phase artifacts landed（4 件 untracked，作為 commit 6 docs 一併進）

- ✅ `docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md`
- ✅ `docs/tracking/test-reports/2026-05-28-decouple-project-frontend-endpoint.md`
- ✅ `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md`（含 mini re-verdict §717-901）
- ✅ `docs/tracking/reviewer-report-2026-05-28-decuple-project-frontend.md`

### D.4 Pending — 待 leader 跟 user 對焦後執行

- [ ] `git add` 全部 8 modified + 11 untracked = 19 files
- [ ] `git commit` × 6（per §B 草稿；Option II 拍板）
- [ ] `git push -u origin fix/decuple-project`
- [ ] PR → develop（per §C 描述）

### D.5 Out-of-scope follow-up（已 standalone open 在 follow-ups）

- ⏸ F-DP-1 `_get_project_stages` 雙重洞（P1，需 design）— B1 family 剩 1 instance
- ⏸ F-DP-3~F-DP-4 + F-DP-5~F-DP-8 reporter polish + future evolution（全 trivial）
- 註：F-DP-2 ✅ CLOSED in this PR

---

## E. Reporter retro 觀察（本 final addendum cycle 對焦）

### E.1 Cycle 6 完整 timeline 跨 8 phase 跑得怎樣

**整體節奏（含 F-DP-2 mini cycle）**：
plan → tester (T1-T2 red) → impl (D1+D2+D3 + frontend) → playwright TC4 fail → tester (T3 red) → impl (TC4 follow-on) → CAQA round 1 BLOCKED → tester (T4 red) → impl (BLOCKER A1 fix) → CAQA round 2 → reviewer → reporter initial → CLAUDE.md addendum → **user 拍板帶 F-DP-2** → tester (T5 red) → impl (F-DP-2 fix + Task.sudo unify) → CAQA mini re-verdict → **reviewer 略過 (leader+user 拍板)** → reporter final addendum

**節奏好處（vs cycle 6 initial）**：
- F-DP-2 mini cycle 加 1 個 variant C 洞察進 pattern doc → 立論從 4 instance 升級為 5 instance 三變體 family（pattern doc completeness 大幅提升）
- Same-method anti-pattern (variant C) 是 cycle 6 originally 看不到的（initial F-DP-2 caveat 只標 under-access）；F-DP-2 fix landing 過程中 caqa mini re-verdict option (b) Task.sudo unify 才把 variant C surface
- Reviewer skip 對 F-DP-2 mini cycle 很合適：4-agent verdict 不會 surface 新 variant C 洞察（reviewer 是 cross-agent verdict aggregation，洞察出在 caqa option (b) reasoning），略過省 1 round 但 0 loss

**節奏負擔**：
- F-DP-2 帶進本 PR 是 user 拍板，但 cycle 6 initial reporter 已寫過「reporter 建議帶 F-DP-2」(deliverables doc §5.1 D.2)，所以是順 reporter recommendation 走 — 沒走偏
- Task.sudo unify 是 implementation 自 flag + caqa option (a/b/c) 三選一，最終 leader+user 拍板 (b) 收口；過程清晰但有 ~10 分鐘討論「(a) perf justification doc 還是 (b) unify」decision making cost

### E.2 reporter 個人觀察 — anti-pattern 三變體 narrative 增益分析

**問題**：本 addendum cycle 對「anti-pattern 三變體」narrative 增益是否值得（vs. 上輪兩變體已夠）？

**結論**：**強烈值得，不只值得，是 pattern doc 立論 integrity 的 cornerstone**。

**理由分析**：

| 維度 | 兩變體版本（cycle 6 initial reporter） | 三變體版本（final addendum） |
|------|---------------------------------|---------------------------|
| **Pattern 立論強度** | A + B = 「sudo() 漏 leg = leak/bug」對立軸 | A + B + C = 「不信任 ir.rule 的 3 種症狀」family taxonomy；root cause-correct 描述 |
| **Reviewer audit checklist 涵蓋** | 抓 leak（A）+ 抓 own-data 不見（B） | 三變體都抓；額外抓「同 method 內留 sudo() 沒明確 perf justification」 |
| **Future code reader 理解** | 「sudo() 不好」二元 | 「sudo() 在 ir.rule-protected model 上預設不可，要留必須在 comment 明寫 why」三層 nuance |
| **Perf 直覺處置紀錄** | 隱含於 (b) unify decision | 顯式 § Perf justification 章節：「即使 ACL-safe 也應 unify；perf 真有需求走 prefetch/batch」 |
| **Pattern lint future-proof** | A/B 抓主要 attack surface | A/B/C 抓所有 sudo() 在 ir.rule-protected model 上的 boundary 模糊 |

**Variant C 的特殊重要性**：它是**唯一一個沒有測試守得到的 anti-pattern variant**。A (cross-tenant leak) 有 HttpCase cross-user test；B (own-data invisibility) 有 own-data assertion test；C (same-method 半套) 不會 fail test（accessible_projects.ids 已 filter，後續 sudo 在 ACL 角度 safe），它只能靠 reviewer + pattern doc 立論一致性 audit catch。**Pattern doc 不寫 variant C，未來 reviewer 看到 sudo() 純 perf 用途會誤判合法 — pattern lint 失效**。

**自我評估**：若我前一輪只寫 A/B，這輪 F-DP-2 cycle 跑出 Task.sudo unify 時，會被 implementation 的 perf 直覺說服「accessible_projects 已 ir.rule filter，留 Task.sudo() 純 perf 用途」→ 不會 catch variant C → pattern doc 立論被同 method 內違反例稀釋而不自知。**這次 addendum 把 variant C 顯式抽出來，pattern doc 的 future-proof 才完整**。

### E.3 補課項 / 遺漏

- 本 final addendum reporter 沒做：
  - **CLAUDE.md 補變體 C 強調**：前一輪 CLAUDE.md addendum 寫 pattern pointer 時只引用「controller layer 不可 `.sudo()` 繞 ir.rule」核心 invariant，沒明寫三變體。Pattern doc 升級後，可選評估是否在 CLAUDE.md pointer 簡述補一句「三變體 A/B/C」。Reporter 不自動改，提議 leader 對焦 user 決定。
  - **Task.sudo unify 是否值得單獨開條目**：本 final addendum 把 Task.sudo unify 收進 F-DP-2 同期 close（同 method、同 logical fix），沒單開 F-DP-9 條目。Trade-off：開條目 = 強調 variant C 獨立性 + 對齊 cycle 6 8+1 改動點 narrative；不開 = 把 F-DP-2 closure 寫成 atomic + 同 method cohesion。Reporter 偏好不開（同 method cohesion > variant 獨立性），但 leader 可拍板。

### E.4 整體 cycle 6 回顧

- **完整 deliverables**：7 docs landed + 4 phase artifacts + CLAUDE.md addendum + 本 deliverables doc = **12 docs + 1 code branch fix/decuple-project**
- **B1 family closure rate**：**80% (5 instance 4 closed)**，剩 1 instance 需 design follow-up
- **Pattern doc 立論完整度**：5 instance critical mass + 三變體 family + perf justification + reverse example + correct example + endpoint test 設計鎖 — **pattern lint future-proof 達標**
- **Anti-pattern coverage**：A over-access (cross-tenant leak) + B under-access (own-data invisible) + C same-method 半套 (boundary 模糊) — controller layer sudo bypass family 三個 surface 全 cover
- **15 條 test/TC 全綠 + 0 blocker + 0 major + 3 caveats follow-up**：reviewer phase 略過 per leader+user 拍板（mini cycle skip 對齊 phase pod 紀律：value 在 caqa option (b) reasoning，不在 reviewer cross-agent verdict）

---

## F. Hand-off summary（給 leader）

**結論**：cycle 6 reporter **final addendum phase 完成**。
- 7 件持久化 doc 全 landed（含本 deliverables doc + 前輪 CLAUDE.md addendum 共 8 件）
- 8 條 follow-up entries 開好（F-DP-2 標 ✅ CLOSED in PR）
- §Consequences cross-link 補好（B1 family 4/5 instance closed）
- 2 個新 pattern doc + 2 個新 pitfall doc 寫好（含三變體 family + perf justification）
- commit + PR description draft 重生（5 code + 1 docs = 6 commits per Option II）

**leader 需執行**（跟 user 對焦後）：
1. ✅ 拍板 multi-commit vs squash（**已拍**：Option II 5 code + 1 docs）
2. ✅ 拍板要不要本 PR 帶 F-DP-2 simple fix（**已拍**：帶）
3. ✅ 拍板要不要 Task.sudo unify（**已拍**：option (b) 同期）
4. ✅ 拍板 CLAUDE.md 補 pattern pointer（**已拍**：前一輪 addendum landed）
5. [ ] 跟 user 對焦 PR title + 是否需要修改 / commit + push + open PR

**reporter 不執行**：`git add` / `git commit` / `git push` / `gh pr create`（per task brief 紀律守線）。

**Cycle 6 收尾**：等 leader 跟 user 對焦最終 ACK 後執行 commit/push/PR。
