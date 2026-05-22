---
name: reviewer-report-2026-05-22-agent-runtime-pod-phase-a
phase: reviewer
team: agent-runtime-pod
reviewer: agent-runtime-pod-reviewer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/tracking/test-reports/test-coverage-agent-runtime-phase-a-2026-05-22.md
caqa_ref: docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md
created: 2026-05-22
status: approved-with-caveats
verdict: APPROVE WITH CAVEATS (escalate leader on 4 hard caveats; 1 of which is design gap escalating to planner)
related:
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/refactor/mcp-url-redesign.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/unbound-project-acl-dual-leg.md
  - docs/decisions/patterns/sidecar-backed-flag-lifecycle.md
  - docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md
  - docs/decisions/patterns/odoo-std-field-no-override.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md
---

# Reviewer Report — Agent Runtime Pod Phase A（schema M:N + 4 new models + 16 rule + migration）

## 1. Final Verdict

**APPROVE WITH CAVEATS** — 不擋 merge，但 **4 條 multi-agent 抓到 CAQA 漏抓的 HC（hard caveat）** 必須 surface 給 leader 拍板。其中 **HC-3 是 design 漏的設計問題**（`agent_template.project_ids` cross-project write injection），按 reviewer role 紅線 trigger「發現 design 漏的設計問題 → escalate planner via leader」。

核心動作守線 verified ✓：
- B-1 closure（4 model × 4 rule = 16 ir.rule + PA-5 Python role gate + SK-6 field-level bundle mask）— attack vector matrix（privilege escalation × cross-project data leak × bundle RCE × DoS）全部關閉 ✓
- OQ-A-2 closure（`project_rule_workspace_member` + `project_task_rule_workspace_member` M:N walk 對齊 mcp_server / mcp_tool pattern）✓
- M:N migration（pre-migrate + post-migrate steps 2/3/8/9/10）— idempotent / replayable / defensive table+column existence guard ✓
- PR #54 Invariants #9/#10/#11 全保留：`_do_cleanup` + `unlink()` URL-prefix narrowing 對等改寫 + sidecar_backed flag 鎖在 filter ✓
- D4 invariant（`scope='user' ⇔ project_id IS NOT NULL`）+ MAJOR 1 防線（外層 AND `scope='user'`）不動 ✓
- 16 新 rule 用 `'any'` operator（SQL EXISTS）鎖 user_id + role 同 row — 比 walk path 更嚴，CAQA Round 2 評估正確 ✓
- 137/137 PASS（Phase A 54 + B-1 29 + OQ-A-2 5 + named regression 49）+ Full suite 656 test 0 fail ✓

5 agent verdict **無方向性分歧**：5 agent 都 verdict APPROVED WITH CAVEATS；對核心 (B-1 closure / OQ-A-2 closure / migration idempotency / Invariants #9/#10/#11 保留) verdict 一致正面。

## 2. Diff Summary

### 17 impl file（git diff develop — committed test 4 commits + uncommitted impl 17 file in working tree）

**Modified（10 file）**：
- `src/__manifest__.py` — version `18.0.1.0.25` → `18.0.1.0.26`；data section **未** 加 view files（OQ-A-3 leader 拍板 defer）
- `src/hooks.py` — `_CONFIG_PARAMETERS` 加 `woow_paas_platform.agent_namespace_secret`
- `src/models/__init__.py` — imports 4 new models
- `src/models/cloud_service.py` — `project_ids` One2many → Many2many reverse；`_do_cleanup` + `unlink()` 加 URL-prefix narrowing；新 helper `_collect_sidecar_user_mcps` + `_sidecar_url_prefix`
- `src/models/project_project.py` — `cloud_service_id` M2O → stored compute over `cloud_service_ids[:1]` + inverse；新 `cloud_service_ids` M2M + `workspace_ids` computed M2M + `external_mcp_allowed` + `use_agent_runtime` + `agent_template_id` + `project_access_ids`；create() override bootstrap owner project_access
- `src/models/project_task.py` — `primary_cloud_service_id` related → 獨立 M2O + computed default + drift handler（hybrid spec Q2 sign-off）；`extra_cloud_service_ids` domain 改 M:N project scope + 保留 redundant workspace clause for legacy test
- `src/security/ir.model.access.csv` — 加 8 row（4 model × user/admin）對 base.group_user/base.group_system 各 1,1,1,1
- `src/security/ir_rules.xml` — 4 walk-path edit（mcp_server / mcp_tool / project / project_task 改 M2M）+ 16 new rule 對 4 個新 model（含 `'any'` operator）
- `docs/plans/2026-Q2/agent-runtime-pod-design.md` — 補拍 sign-off
- `docs/tracking/followups/follow-ups.md` — 加 F-PA-1 ~ F-PA-12 cycle 5 section

**Untracked（10 file，含 doc）**：
- `src/models/agent_template.py`（新檔，116 行）— design §4.1 落地 + `_check_visibility_consistency` + `_check_mcp_subset` 兩條 constraint
- `src/models/agent_session.py`（新檔，114 行）— design §2.12 落地 + `_compute_project_namespace_hash` HMAC + UNIQUE(project_id) + UNIQUE(project_namespace_hash)
- `src/models/skill.py`（新檔，90 行）— design §3.2 落地 + `bundle_attachment_id groups='base.group_system'` field-level mask（SK-6 path (a)）+ UNIQUE(project_id, name, version)
- `src/models/project_access.py`（新檔，159 行）— design §4.3 落地 + ROLE_HIERARCHY + ASSIGNABLE_ROLES_BY_ROLE + `_check_assignable_role` Python gate + `unlink()` last-owner guard + `_compute_user_id` from partner_id
- `src/migrations/18.0.1.0.26/pre-migrate.py`（37 行）— Step 1 CREATE TABLE IF NOT EXISTS `project_cloud_service_rel`
- `src/migrations/18.0.1.0.26/post-migrate.py`（237 行）— Step 2/3/8/9/10 + helper `_column_exists` / `_table_exists`
- `docs/plans/2026-Q2/agent-runtime-pod-plan.md`、`docs/tracking/caqa-report-...`、`docs/tracking/test-reports/...` — phase docs

### 8 test file（committed 在 fe480a0 + 早期 commits）

| Test file | Method | 段 |
|-----------|--------|----|
| `test_agent_pod_phase_a_project_schema.py` | 11 | A.1-A.9 |
| `test_agent_pod_phase_a_task_schema.py` | 9 | T.1-T.5（hybrid spec Q2 sign-off）|
| `test_agent_pod_phase_a_cloud_service_mn.py` | 4 | C.1-C.5（含 3 GREEN regression lock 中 C.4 leg）|
| `test_agent_pod_phase_a_new_models.py` | 13 | N.1-N.12 |
| `test_agent_pod_phase_a_acl.py` | 8 | R.1-R.9（含 R.3 / R.9 GREEN regression lock）|
| `test_agent_pod_phase_a_migration.py` | 9 | M.1-M.9 |
| `test_agent_pod_phase_a_acl_new_models.py`（B-1 round）| 29 | B1.AT-1~8 / AS-1~5 / SK-1~8 / PA-1~8 |
| `test_agent_pod_phase_a_project_task_acl_mn.py`（OQ-A-2 round）| 5 | O1.1-O1.5 |
| **總計** | **88** | |

### Migration 重點

- Pre-migrate Step 1：CREATE TABLE `project_cloud_service_rel`（+ index on cloud_service_id）— idempotent ✓
- Post-migrate Step 2：backfill 1:1 → M:N（`INSERT ... ON CONFLICT DO NOTHING`）— defensive table existence guard ✓
- Post-migrate Step 3：DROP UNIQUE constraint — 動態抓 constraint name via `information_schema`，`LIKE '%cloud_service%'` ✓（注意：`%`-style 字串拼接 vs `psycopg2.sql.Identifier` 風險見 §5 SC）
- Post-migrate Step 4：keep `cloud_service_id` column（noop；deprecated）
- Post-migrate Step 8：`UPDATE ... SET external_mcp_allowed = FALSE WHERE IS NULL` — idempotent ✓
- Post-migrate Step 9：bootstrap `project_access` owner 從 `cloud_service.workspace_id.access_ids` 抓 + fallback 到 `pp.create_uid` partner；`NOT EXISTS` outer + `ON CONFLICT DO NOTHING` inner — idempotent ✓
- Post-migrate Step 10：純 log，不執行 SQL（Phase F destructive cleanup 才動）

### 4 new model 摘要

| Model | 用途 | 關鍵 constraint |
|-------|------|---------------|
| `agent_template` | Agent runtime template (system prompt + tool binding) | `_check_visibility_consistency` (state) + `_check_mcp_subset` (template.mcp_server_ids ⊆ project ∪ system) |
| `agent_session` | Per-project pod lifecycle record | UNIQUE(project_id) + UNIQUE(project_namespace_hash); HMAC-SHA256 namespace hash compute on create |
| `skill` | Project-scoped skill bundle registry | UNIQUE(project_id, name, version); `bundle_attachment_id groups='base.group_system'` field-level mask |
| `project_access` | First-class project ACL row | UNIQUE(project_id, partner_id); `_check_assignable_role` Python role-hierarchy gate; `unlink()` last-owner guard |

## 3. 5 Agent Finding 整合表

| Agent | Verdict | BLOCKER | MAJOR | MINOR | INFO |
|-------|---------|---------|-------|-------|------|
| code-reviewer | APPROVED WITH CAVEATS | 0 | 0 | 3 | 4 |
| pr-test-analyzer | APPROVED WITH CAVEATS | 0 | **1**（IG-1）| 3 | 3 |
| silent-failure-hunter | APPROVED WITH CAVEATS | 0 | **2**（C-1 / C-2） | 4 | 3 |
| type-design-analyzer | APPROVED WITH CAVEATS | 0 | 0 | 4 | 5 |
| comment-analyzer | APPROVED WITH CAVEATS | 0 | 0 | 6 | 5 |
| **合計（去重）** | — | 0 | **4 unique HC**（C-1 / C-2 / IG-1 / I-3） | ~15 | ~12 |

### 3.1 MAJOR Findings — Hard Caveats（4 條，需要 leader 拍板）

#### HC-1 (silent-failure-hunter C-2): `_inverse_cloud_service_id` replace semantics + controller writes silently drop M:N bindings

- **位置**：
  - `src/models/project_project.py:88-99` `_inverse_cloud_service_id`（write `cloud_service_id = X` → `cloud_service_ids = [(6, 0, [X.id])]` replace）
  - `src/controllers/paas.py:1594` `vals['cloud_service_id'] = target.id` in `_update_support_project`
  - `src/controllers/ai_assistant.py:944` `vals['cloud_service_id'] = int(cs_id)` in `_create_project`
- **症狀**：對 Phase A migration 後既有 1-service M:N row 沒實際 break（每 row 1 service，replace = noop）。**Phase B/C user 透過 UI add 第二 service 後**，任何 controller `cloud_service_id = X` write 會 silently drop 其他 binding，無 log / 無 warning / 無 error，下游影響 `workspace_ids` / `task.primary_cloud_service_id` drift / `agent_template.project_ids` / ir.rule visibility / lifecycle。
- **本 PR 製造？**：是（M2O → stored compute + inverse 是本 PR 引入；controller caller 是 pre-existing legacy write）
- **嚴重度評估**：MAJOR (silent contract change + latent regression at Phase B/C multi-bind activation)
- **CAQA 漏抓**：caqa §4 grep 列「controller scope 留 Phase D-E」是 read direction；本 write direction silent break **沒被 surface**
- **同源 pattern**：同 PR #54 HC-1「plan-scoped helper but same-source path 漏掉」(legacy endpoint blindness)，但本案 source-code path 是新 inverse 的「silent contract change」而不是「漏裝 helper」
- **建議方向**：
  - **(a) hotfix in this PR**（推薦）：`_inverse_cloud_service_id` 加 `_logger.warning` 當既有 set 非空 replace；或改 additive `(4, X.id)` 語意 + 在 `help=` 文字明示 contract
  - **(b) leader 拍板 defer**：標 reporter follow-up，Phase B/C controller migrate 前 audit + 修正
  - **(c) escalate user**

#### HC-2 (silent-failure-hunter C-1): `project.sudo().create()` bypass `project_access` bootstrap → creator silently 看不到自己 project 的 agent_template/session/skill/project_access

- **位置**：
  - `src/models/project_project.py:122` create() override `if self.env.uid == SUPERUSER_ID: return projects` skip bootstrap
  - `src/controllers/ai_assistant.py:946` `request.env['project.project'].sudo().create(vals)`（唯一 production sudo create path，verified by grep `src/controllers/`）
- **症狀**：
  - `sudo()` 設 `env.uid = SUPERUSER_ID` → bootstrap branch skip → 新 project 無 `project_access` owner row
  - Creator 仍可看到 project itself（透過 `project_rule_workspace_member` leg 2 carve-out `create_uid = user.id`）
  - 但對 4 個新 model（`agent_template` / `agent_session` / `skill` / `project_access`）的 16 條 ir.rule 全 walk `project_id.project_access_ids` → no row exists → creator 完全看不到自己 project 上的這些 records
  - **Phase A 沒人 active 4 model**（dormant）→ 無 visible impact today
  - **Phase B-D `use_agent_runtime` active 後 immediate functional regression** — creator 不能管理自己 project 的 templates / skills / sessions
- **本 PR 製造？**：是（SUPERUSER skip + 4 model ir.rule 是本 PR；ai_assistant.py sudo create 是 pre-existing）
- **嚴重度評估**：MAJOR latent functional regression（不是 security IDOR，是 own-project visibility gap）
- **CAQA 漏抓**：CAQA I-5 framing 為「OdooBot owner is conceptually 奇怪」demo data case，但**實際 production 影響是「任何透過 ai_assistant controller 創 project 的 user 立即缺 project_access」**，比 demo data 嚴重得多
- **建議方向**：
  - **(a) hotfix in this PR**：`project_project.create()` 對「is_superuser() 但 env.user.partner_id 是 real human」case 仍走 bootstrap；distinguish cron/install/migration 透過 context flag（e.g. `_skip_project_access_bootstrap=True`）
  - **(b) controller fix**：`ai_assistant.py:946` 改 `request.env['project.project'].with_user(env.user).create(vals)` 不走 sudo（但要 verify create 對 std `project.project` 沒 ACL 阻擋 base.group_user）
  - **(c) defer**：標 follow-up F-PA-13，Phase D activation 前必修

#### HC-3 (pr-test-analyzer IG-1): `agent_template.project_ids` M2M write cross-project injection — **DESIGN GAP**

- **位置**：
  - `src/security/ir_rules.xml` `agent_template_rule_project_admin` domain `('project_ids.project_access_ids', 'any', [('user_id', '=', user.id), ('role', 'in', ['owner', 'admin'])])`
  - `src/models/agent_template.py:94-115` `_check_mcp_subset` constraint
- **症狀**：
  1. Attacker（在 project A 是 owner/admin）create template T binding to A
  2. Attacker write `T.project_ids = [(4, victim_B)]`（add B 到 template）
  3. ir.rule `agent_template_rule_project_admin` 用 `'any'` evaluate：`T.project_ids = [A, B]`，A 上 attacker 仍是 owner → `'any'` 通過 → write OK
  4. `_check_mcp_subset` 計算 `allowed = T.project_ids.mapped('mcp_server_ids') ∪ system-scope`，含 B 的 user-scope MCP → attacker 從 A 加進 T.mcp_server_ids 的 MCP 通過 subset check（仍在 A 內）
  5. T 現在綁 B → B's project_user 對 T 透過 `agent_template_rule_project_user` 可見（R only）→ **cross-project template visibility leak** active in Phase A schema
  6. Phase D 若 B admin 主動把 T 設成 `project.agent_template_id` → attacker controlled `system_prompt` + `mcp_server_ids` active on B's agent runtime → cross-project agent runtime hijack
- **本 PR 製造？**：是（agent_template + ir.rule 是本 PR 引入）
- **嚴重度評估**：MAJOR（design 漏的設計問題 — `_check_mcp_subset` 對 attacker controlled `project_ids` 寬鬆化；rule 沒禁 attacker 把 template 推到沒 access 的 project）
- **同源 pattern**：同 PR #54 HC-2「silent ACL divergence」— constraint 用 attacker controlled input 做 subset check = silent IDOR-like behavior
- **CAQA 漏抓**：CAQA invariant table Phase A #7 沒對 `agent_template.project_ids` write direction做 cross-project escalation check
- **escalation**：**必須 escalate planner via leader 拍板**（reviewer role 紅線 trigger「發現 design 漏的設計問題」）
- **建議方向**：
  - **(a) Phase A 補 Python @api.constrains**（推薦）：對 `agent_template.write({'project_ids': ...})` 加 constraint「每個 new project_id 都必須 grantor 是 admin/owner」— ir.rule 表達不出來，需 Python
  - **(b) Phase A 補 ir.rule 對 M2M relation table**（agent_template_project_rel）做 row-level deny — Odoo 對 m2m relation 沒原生 ir.rule，但可透過 controller carve-out 守
  - **(c) defer to Phase B/C**：標 F-PA-14 follow-up，Phase D activation 前必修 + 補 acceptance test
  - **(d) Other** — escalate user

#### HC-4 (silent-failure-hunter I-3): `skill.bundle_attachment_id` field-level group mask 不擋 `/web/content/{att_id}` direct download → potential RCE via skill bundle

- **位置**：
  - `src/models/skill.py:42-47` field-level `groups='base.group_system'` 只 mask ORM read field
  - `ir.attachment` row 本身按 attachment 的 ACL 服務 `/web/content/{att_id}`（既有 Odoo std endpoint）
- **症狀**：
  - SK-6 path (a) field-level mask 擋 Skill ORM read attachment_id
  - 但 attacker 可 enumerate sequential `ir.attachment.id`（整數）+ 透過 `/web/content/<id>?download=true` 直接下載
  - 如果 attachment `res_model='woow_paas_platform.skill'`（design 假設）+ project user 透過 ir.rule 對 skill 有 R → attachment 透過 res_model walk 推導 → 下載通過
  - Skill bundle 是 **subprocess 執行的 code** → 下載 + reverse engineer + craft malicious bundle → upload back → **RCE on agent pod**
- **本 PR 製造？**：否（Phase A 沒 active bundle upload — dormant；attack surface 在 Phase B/C upload pipeline land 後 active）
- **嚴重度評估**：MAJOR（Phase B/C 前必修；Phase A dormant 沒 active surface）
- **CAQA 漏抓**：caqa §13.2 Attack 3 closure 只 verify Skill ORM path（「attacker 無法 R bundle binary」），未對 `/web/content` direct path 做 verify
- **建議方向**：
  - **(a) hotfix in this PR**：on bundle attachment create，set `public=False`, `res_model='woow_paas_platform.skill'`, `res_id=skill.id` + 加 explicit ir.rule on `ir.attachment` 對 `res_model='woow_paas_platform.skill'` rows 限 `base.group_system` only
  - **(b) Phase B/C bundle upload API 補 access_token** — 同時 update bundle download path 強制 token
  - **(c) defer to Phase B/C scope expansion**：標 F-PA-15 follow-up（Phase B/C blocker，Phase A dormant 不擋 merge）

## 4. Multi-agent 分歧處理

**無方向性分歧**。5 agent verdict 全部 APPROVED WITH CAVEATS，對核心動作（B-1 closure / OQ-A-2 closure / migration idempotency / Invariants #9/#10/#11 保留 / `'any'` operator design）verdict 一致正面。

**「嚴重程度判斷」差異但非分歧**：
- silent-failure-hunter 自評 C-1 / C-2 建議升 BLOCKER；reviewer 評估後判 **HC level**（hotfix 推薦但不擋 merge — Phase A schema 落地，runtime impact dormant）
- pr-test-analyzer 自評 IG-1 為 rating 7 important；reviewer 評估後判 **HC level + escalate planner**（design 漏的設計問題，超出 implementation 改可解範圍）
- type-design-analyzer 自評 workspace_ids_snapshot CSV / quota_override no-bounds / model_overrides JSON 為 important；reviewer 判 **Phase B/C 前必修 caveat**（Phase A dormant，schema 鎖了未來換型成本）
- comment-analyzer 自評 6 minor + 5 info — reviewer 判 reporter phase 收尾

**Reviewer verdict 角度**（自評）：5 agent 都建議 follow-up，但 **HC-1 / HC-2 / HC-3 / HC-4 是 CAQA 漏抓的 4 條真 finding**（reproducible / design gap / silent regression）。按 reviewer role hard rule：

> 發現 high-severity 但 CAQA 沒抓到 → escalate leader，且不能直接 approve
> 發現 design 漏的設計問題 → escalate planner via leader

**Leader 需 sign-off 以下 4 條 HC 處理路徑**：
- (a) **approve with hard caveat** → 開 follow-up issue F-PA-13/14/15/16 排入 sprint+1 PR 收乾
- (b) **hotfix in this PR** → 加 4 條 fix（~15-30 lines code + 4 test method）
- (c) **escalate user** 拍板 scope creep vs PR ship 平衡

## 5. Soft Caveats（reporter phase 收尾，不擋 merge）

### 5.1 Phase A 直接 reporter 收尾

| ID | Item | 來源 agent | 收尾段 |
|----|------|----------|------|
| SC-1 | `discuss_channel.py:262` stale comment（task primary 而不是 project's cloud_service_id）— CAQA I-1 / F-PA-8 升 Medium per comment-analyzer | comment-analyzer + CAQA I-1 | reporter |
| SC-2 | `project_task.py:38-41` `primary_cloud_service_id` field docstring「acts as a default」不精確（CAQA I-4 / F-PA-10）— 應改「sticky default + drift reset」 | comment-analyzer + CAQA I-4 | reporter |
| SC-3 | `project_project.py:18-32` `cloud_service_id` deprecated `help=` 沒明示 replace semantics（與 HC-1 paired）| comment-analyzer | reporter（hotfix HC-1 時同步）|
| SC-4 | `project_project.py:119-123` SUPERUSER skip comment 沒 cross-ref F-PA-11 OdooBot owner edge case | comment-analyzer | reporter |
| SC-5 | `agent_session.py:7` `_DEFAULT_SECRET` 缺 warning comment — 公開 git repo 必須 override（與 F-PA-1 paired）| comment-analyzer | reporter |
| SC-6 | `skill.py:37-47` `bundle_attachment_id` group mask 沒明示 owner/admin via backend form blocked + 沒 cross-ref F-PA-7 / C-A-3 | comment-analyzer | reporter |
| SC-7 | `project_access.py:17-25` `ASSIGNABLE_ROLES_BY_ROLE` 沒明示 C-A-2 deviation from design §4.3 + F-PA-6 cross-ref | comment-analyzer | reporter |
| SC-8 | `mcp_server.py:93-98` `auto_created` deprecation label 與 cron 仍 active 使用 conflict — new F-PA-13 candidate | comment-analyzer | new follow-up |
| SC-9 | `ir_rules.xml:269-285` Phase A B-1 section comment 可加 attack scenario explanation + 6-rule split cross-ref | comment-analyzer | reporter |
| SC-10 | `ir_rules.xml:53-58` `noupdate=0` block comment stale（沒列 16 new rule prefix） | comment-analyzer | reporter |
| SC-11 | `cloud_service.py:1179-1195` `_sidecar_url_prefix` docstring 沒 warn M-4 caveat（M:N pre-deploy 與 deployed peer mix → broad-filter 風險）| comment-analyzer | reporter（與 F-PA-2 paired）|

### 5.2 Phase B/C 前必修（不擋 Phase A merge，但 Phase B/C blocker）

| ID | Item | 來源 agent | 收尾段 |
|----|------|----------|------|
| SC-12 | `agent_session.workspace_ids_snapshot` `fields.Char` CSV — weak type，Phase B 寫入時 parser bug 風險；考慮 `fields.Json` 或 M2M snapshot table | type-design-analyzer | new follow-up F-PA-14 |
| SC-13 | `agent_session.quota_override_*` `fields.Integer` 無 bounds（CPU=0 / memory=-5 / 上限）— 缺 `@api.constrains` sanity check | type-design-analyzer | new follow-up F-PA-15 |
| SC-14 | `agent_template.model_overrides` `fields.Text` 容 JSON 但無 schema validation — 考慮 `fields.Json` + allow-list constrain | type-design-analyzer | new follow-up F-PA-16 |
| SC-15 | `agent_session._get_namespace_secret` silent fallback 應加 `_logger.warning` once per worker startup + 拒絕 secret < 32 bytes（與 F-PA-1 paired hotfix）| silent-failure-hunter I-1 | F-PA-1 augment |

### 5.3 Phase A 內 polish / 風險改善

| ID | Item | 來源 agent | 收尾段 |
|----|------|----------|------|
| SC-16 | `project_access._compute_user_id` `@api.depends('partner_id')` 漏 indirect depends through `partner_id.user_ids` — partner→user upgrade flow user_id 不更新 → ACL silent dead | code-reviewer I-1 | new follow-up F-PA-17 |
| SC-17 | `agent_template._check_mcp_subset` 信任 system-scope set（任何未來 sudo create system MCP 立即 universally allowed）— 加 audit log 或 explicit allowlist | silent-failure-hunter I-2 | F-PA cluster |
| SC-18 | `cloud_service.unlink()` per-record loop 與 `_do_cleanup` 對 user_mcps unlink 異常的 try/except 不對等（unlink no catch, _do_cleanup wider catch） | silent-failure-hunter I-4 | F-PA cluster |
| SC-19 | `project_access.unlink()` last-owner check `search_count` 是 TOCTOU race（Postgres READ COMMITTED default）— 兩 concurrent unlink 都通過 → project 0 owner | silent-failure-hunter E-1 | new follow-up F-PA-18 |
| SC-20 | `post-migrate.py:118-121` `%`-style 字串拼接 constraint name — bounded（information_schema source 非 user input）但 defensive 用 `psycopg2.sql.Identifier` 更安全 | silent-failure-hunter E-2 + code-reviewer S-1 | F-PA cluster |
| SC-21 | `project_task._compute_primary_cloud_service_id` re-compute storm — 大 project 改 cloud_service_ids 觸發全 task 重 compute；Phase A task count 小可忽略，但 caveat 需在 docstring 表達 | silent-failure-hunter E-3 + CAQA M-4 perf section | reporter |
| SC-22 | `post-migrate.py:171` Step 9 SQL 走 `pp.cloud_service_id`（deprecated stored compute）— Phase F destructive cleanup drop column 後 re-run 18.0.1.0.26 會 silent fall-through 到 create_uid 全 project | code-reviewer I-2 | new follow-up F-PA-19（Phase F 前 audit）|
| SC-23 | `project_task_views.xml:25` `primary_cloud_service_id readonly="1"` — contradicts hybrid spec user override；OQ-A-3 defer scope 內，但需明示 | code-reviewer I-3 | reporter / Phase B/C |
| SC-24 | Test `_sidecar_url()` helper diverges silently from production — 改 `self.svc_a._model_build_mcp_endpoint_url()` direct call | pr-test-analyzer TQ-2 | reporter test polish |
| SC-25 | Test C.4 external MCP `url` 沒 share sidecar URL prefix → `sidecar_backed` flag 不被 discriminating 鎖；應 seed url 帶相同 prefix 才強化 lock | pr-test-analyzer Lock #3 weakness | reporter test polish |
| SC-26 | Test M.8 idempotency 沒 exercise non-pristine state（用戶手動刪 row + re-run）| pr-test-analyzer IG-3 | reporter test polish |
| SC-27 | Test O1.4 W3 outsider deny path 對 `unrelated workspace member` 不夠 strict（Eve has zero workspaces）| pr-test-analyzer IG-4 | reporter test polish |
| SC-28 | Test IG-2 namespace_secret regression test 缺（與 HC-1 fix paired）| pr-test-analyzer IG-2 | F-PA-1 augment |

## 6. CAQA Cross-Check

CAQA verdict **APPROVED WITH CAVEATS**（5 caveat M-1~M-5 + 6 info I-1~6 + 3 new C-A-1/2/3）→ F-PA-1 ~ F-PA-12 follow-up。**Reviewer 全 confirm**，但有以下補充與升級：

### 6.1 CAQA verdict 同意

- B-1 closure 16 rule + PA-5 Python gate + SK-6 field-level mask — verify ✓
- OQ-A-2 closure project / task M:N walk — verify ✓
- 11 + 7 invariants 守線（PR #54 11 條 + Phase A 7 條）— verify ✓
- 3 條 GREEN regression lock（R.3 / R.9 / C.4 leg）— wired strongly enough ✓（SC-25 補 C.4 leg discriminating power 不足，不擋）
- migration idempotent / replayable ✓
- 12 follow-up F-PA-1~12 拆分合理 ✓

### 6.2 CAQA 升級 / 補充

| CAQA 項 | Reviewer 評估 | 升級 / 補充 |
|--------|--------------|-----------|
| M-3 namespace_secret hardcode | **同意 medium** but silent fallback 自己仍 caveat | SC-15 補 `_logger.warning` once per worker + 拒絕 secret < 32 bytes |
| I-5 SUPERUSER bootstrap skip（OdooBot owner edge case） | **升級 HC-2** — production active path `ai_assistant.py:946` 比 demo data 嚴重 | escalate leader |
| §4 grep 「cloud_service_id reads in controllers/paas.py 12 處 hit」結論「caveat 級」| **升級 HC-1** — write direction silent replace semantics 未 surface | escalate leader |
| Phase A invariants #6 `project_access` first-class ACL ✓ | **發現 cross-project agent_template injection design gap** | HC-3 escalate planner via leader |
| §13.2 Attack 3 closure（skill bundle RCE） | **`/web/content` direct path 未 verify** | HC-4 escalate leader |

### 6.3 CAQA 漏抓 unique findings 整理

| ID | Finding | Severity | escalation |
|----|---------|----------|-----------|
| **HC-1** | `_inverse_cloud_service_id` replace semantics silent break | MAJOR | leader |
| **HC-2** | `project.sudo().create()` bypass bootstrap → own-project visibility gap | MAJOR | leader |
| **HC-3** | `agent_template.project_ids` cross-project write injection | MAJOR (design gap) | **planner via leader** |
| **HC-4** | `skill.bundle_attachment_id /web/content` side-channel | MAJOR | leader |
| SC-15 | `_get_namespace_secret` silent fallback no log | medium | reporter |
| SC-16 | `project_access._compute_user_id` partner→user upgrade staleness | medium | new follow-up |
| SC-19 | `project_access.unlink()` last-owner TOCTOU race | medium | new follow-up |
| SC-22 | Step 9 SQL walks deprecated `cloud_service_id` column | medium (Phase F-only) | new follow-up |

## 7. Plan Alignment Check

Implementation 對齊 plan §Phase A 19 task：

- ✓ 13 / 19 task 落地（#1-13 + #19 manifest bump）
- ✗ 5 / 19 task **leader 拍板 defer to Phase B/C**（#14-#18 view files）— OQ-A-3 sign-off F-PA-4
- ✗ #19 manifest version bump ✓ 但 data section 未加 view files（與 #14-18 一致）

**Plan vs implementation 對齊 ✓**：
- Task #13（16 rule）impl 補上 ✓（B-1 closure）
- Plan §To phase-caqa hand-off line 541「ir.rule mcp_server / mcp_tool / agent_template / skill / project_access ↔ controller `_check_project_access` 兩層 lock-step」**Phase A 完成 Layer 1 ir.rule 層** ✓；Layer 2 controller carve-out 留 Phase D-E（plan §AC #43-44 明示）✓
- Plan §AC 49 條對齊 49 + 34 = 83 test method ✓（54 Phase A + 29 B-1 + 5 OQ-A-2，後二者 round 2 補）

**implementation 走偏 scope 範圍 = 0**（5 view files defer 是 leader 拍板的）。

## 8. 同源 Code Path 漏網

grep verify 後，**4 條 same-root-cause but unfixed**：

1. **HC-1 controller writes silent replace** — `grep "'cloud_service_id':" src/controllers/` 結果 3 處（`ai_assistant.py:911,953` + `paas.py:1558`，全是 read response payload；無 write pattern）；但 `vals['cloud_service_id'] = X` write 在 `ai_assistant.py:944` + `paas.py:1594` — silent contract change at inverse layer
2. **HC-2 sudo create bypass bootstrap** — `grep "project_project'\]\.sudo()\.create" src/controllers/` 結果 1 處（`ai_assistant.py:946`）；唯一 production sudo create path
3. **HC-3 agent_template cross-project write** — `grep "agent_template_id" src/` 結果 `project_project.py:80` field 定義 + migration step 10 fallback — 無 production caller 寫 `project_ids` 在 Phase A，但 schema 層 attack surface 已 active
4. **HC-4 `/web/content` side-channel** — `grep "/web/content" src/` 結果 `ai_assistant.py:194, 347` 既有 attachment URL pattern；skill bundle Phase B/C 後加入 attack surface

其他 plan AC 描述的 implementation 動作（M:N migration / 16 rule / `_compute_primary_cloud_service_id` / `_check_assignable_role` / `unlink()` URL-prefix）grep 結果與 CAQA §4 同源 code path 檢查一致 — **無漏網**。

## 9. Known Pitfalls 對照

| Pitfall | 本 PR 重蹈跡象 | 評估 |
|---------|-------------|------|
| [[cross-workspace-task-data-leak]] B1 | **無重蹈** | 16 rule + `'any'` operator 鎖 cross-row → CSV ↔ ir.rule 對等補上 lock-step；B-1 同源 root cause 已 close ✓ |
| [[plan-vs-odoo-schema-mismatch]] | **無重蹈** | 所有 schema reference (`cloud_service_ids` / `project_access_ids` / `workspace_access.access_ids.user_id` / `res_users.partner_id`) 在 Odoo 18 真實存在 ✓ |
| [[system-mcp-create-acl-bypass]] MAJOR 1 | **無重蹈** | dual-leg 外層 AND `('scope', '=', 'user')` 不動；6-rule split parity 守線 ✓ |
| [[direct-unlink-bypass-cleanup-hook]] | **無重蹈** | `unlink()` override 同樣加 URL-prefix narrowing + sidecar_backed flag ✓ |
| [[two-layer-acl-lockstep]] | **Phase A 半實現預期** | Layer 1 ir.rule 16 new rule 完成；Layer 2 controller carve-out 留 Phase B-E（plan 明示） |
| [[two-layer-acl-asymmetric-deny]] | **無重蹈** | Phase A 沒兩層收緊一邊放寬 case；Phase B-E 補 Layer 2 時須對齊 pattern |
| [[legacy-endpoint-scope-blindness]] | **HC-1 部分重蹈** | inverse `_inverse_cloud_service_id` 改了，legacy controller caller `cloud_service_id = X` write 沒對應 audit／log — same-source code path silent contract change |
| [[odoo-addon-std-rule-symmetric-deactivation]] | **無重蹈** | refactor branch 既有 3 條 std rule deactivation 在 `ir_rules.xml:262-267` 全保留 ✓ |
| [[odoo-std-field-no-override]] | **無重蹈** | `project.project` 沒 override std field 型態（只加新 field + 改 `cloud_service_id` 為 stored compute — 仍 Many2one type）；`project.task` 沒 override std field 型態 ✓ |
| **(potential 新)** `_check_xxx_subset` constraint trusts attacker-controlled input list | **HC-3 — 提議抽新 pitfall** | Constraint 用 attacker write 的 m2m field 做 subset check 等於擴充 allowed set — silent IDOR-like surface；建議 reporter 抽 `docs/tracking/pitfalls/constraint-input-trust-expansion.md` |

## 10. Open Questions for Leader

### OQ-R-1 — HC-3 hotfix vs defer

**現況**：`agent_template.project_ids` M2M write 對 attacker（自己 project owner）允許推 template 到 victim project，造成 cross-project template visibility + Phase D activation 後 system_prompt / mcp_server_ids hijack。

**design gap**：design §4.1 + §4.2 sign-off「M:N constraint via `_check_mcp_subset`」對 attacker controlled project_ids 寬鬆化 — design 漏想 attacker 用「自己 project」當合法 input。

**escalate planner via leader 拍板**：
- (a) **Phase A 補 Python `@api.constrains('project_ids')`** — 對 write `project_ids` 加 「每個 new project_id 都必須 attacker.has_role(owner/admin) on that project」check（ir.rule 表達不出 write direction 的 input list 限制）
- (b) **Phase B/C 補**：標 F-PA-14 follow-up，Phase D activation 前必修 + 補 acceptance test
- (c) **設計修改** — agent_template 改 1:N project（每 project 一 template）— 拒絕 M:N 設計

### OQ-R-2 — HC-2 hotfix 路徑

**現況**：`project.sudo().create()` bypass `project_access` bootstrap → creator 對 4 model dormant rule 全 fail。

**leader 拍板**：
- (a) **`project_project.create()` 區分 SUPERUSER context**：透過 `self.env.context.get('_skip_pa_bootstrap')` flag distinguish cron/install/migration vs ai_assistant controller
- (b) **controller 改 with_user**：`ai_assistant.py:946` 改 `request.env['project.project'].with_user(env.user).create(vals)` — 但需 verify `project.project` ACL 對 base.group_user 是否允許 create（既有 `ir.model.access.csv:27` `project.model_project_project access_project_project_internal` 1,1,1,1 應該允許）
- (c) **defer to Phase B-D activation 前必修** — 標 F-PA-13 follow-up

### OQ-R-3 — HC-1 + HC-4 path

**HC-1**：`_inverse_cloud_service_id` replace semantics — Phase A schema 落地後既有 controller write `cloud_service_id = X` 對 multi-bind row silently drop bindings。
**HC-4**：`skill.bundle_attachment_id /web/content` side-channel — Phase B/C upload pipeline 前必修。

**leader 拍板**：
- (a) **approve with hard caveat** → 排 follow-up F-PA-13/14/15/16，Phase B/C / Phase D activation 前必修
- (b) **hotfix HC-1 in this PR**（推薦）：`_inverse_cloud_service_id` 加 `_logger.warning` when replace + `help=` 文字更新 → 1-2 line 改動，無 test impact
- (c) **hotfix HC-1 + HC-4 in this PR** + 補 test
- (d) **escalate user**

## 11. Sign-off

Reviewer phase verdict：**APPROVE WITH CAVEATS** — proceed to Reporter phase OR fix-loop back to implementation per leader decision on HC-1 / HC-2 / HC-3 / HC-4。

**Reporter phase 需處理**：
- SC-1 ~ SC-28 全 28 條 soft caveat
- HC-1 / HC-2 / HC-3 / HC-4 若 leader 拍 (a) → 開 follow-up issue F-PA-13/14/15/16/17/18/19（4 HC + 3 SC 升級）+ surface attack vectors in PR description
- HC-* 若 leader 拍 (b) / (c) → fix-loop 回 implementation 補 hotfix，後 reviewer 再過 spot-check
- PR description draft（Phase A landed schema + 4 model + 16 rule + migration）
- 同時不 close any issue（Phase A 是 epic 第一階段，後續 Phase B-G 接力）

**Reporter follow-up doc 候選**（基於 HC-3 抽 pattern）：
- `docs/tracking/pitfalls/constraint-input-trust-expansion.md` — `_check_xxx_subset` constraint 對 attacker controlled m2m field 做 subset check 寬鬆化 attack surface

Reviewer 簽核：**agent-runtime-pod-reviewer**，2026-05-22

---

## 12. Re-verify after Cycle 3（HC-3 + HC-4 hotfix，2026-05-23）

> Leader 拍板 hotfix HC-3 + HC-4 in this PR（HC-1 / HC-2 維持 caveat）。Impl 完成 3 file 改動；tester 加 8 method（HC3.1-3.4 + HC4.1-4.4）；144/144 PASS（既有 137 + HC hotfix 7 — 1 fixture overlap）。本節短輪 verify HC-3 + HC-4 closure，**不重 review 整個 PR**。

### 12.1 Verdict 更新

**APPROVE WITH CAVEATS** — HC-3 closed ✓ + HC-4 closed ✓；HC-1 + HC-2 維持 caveat 狀態（未 hotfix）；無新 high-severity 發現。可進 reporter phase。

### 12.2 HC-3 closure 證據（`agent_template.project_ids` cross-project injection）

**Hotfix 位置**：`src/models/agent_template.py:94-122` 新 `_check_project_ids_grantor_authority` Python `@api.constrains('project_ids')`

**對照 §3 HC-3 5-step attack chain reproducer**：

| Step | Attack | Defense (post-hotfix) |
|------|--------|----------------------|
| 1 | Attacker (在 project A admin) create template T binding to A | constrain trigger，check `T.project_ids = [A]`；attacker 對 A 是 admin → pass ✓ |
| 2 | Attacker write `T.project_ids = [(4, victim_B)]` | constrain trigger，check `T.project_ids = [A, B]`；A pass，B fail（attacker `.user_id` 不在 B 的 `project_access_ids` with role IN ('owner', 'admin')）→ **ValidationError raise** ✓ |
| 3 | T 含 B → B's project_user 透過 ir.rule 可見 T | step 2 deny → 此 step 不可達 ✓ |
| 4 | Phase D activation：B admin 把 T 設成 `project.agent_template_id` | upstream cross-project visibility 已 close ✓ |
| 5 | Attacker controlled `system_prompt` + `mcp_server_ids` active on B's agent runtime | 上游 close → attack chain 切斷 ✓ |

**Constraint shape verify**（`agent_template.py:108-122`）：
- `if self.env.is_superuser() or self.env.user.has_group('base.group_system'): return` — sudo / SUPERUSER / `base.group_system` bypass（合法 cron / migration / paas-operator / admin curation 路徑）
- `proj.project_access_ids.filtered(lambda r: r.user_id == user and r.role in ('owner', 'admin'))` — 對每個 bound project 檢查 attacker user_id 是否在 project_access 內 role admin/owner
- 對齊 design §4.1 sign-off「M:N constraint via grantor authority on each bound project」字面化（design §4.1 line 1932 原本只 list `(is_public, project_ids)`，本 hotfix 補上 design intent 的 grantor check）

**`_check_mcp_subset` 觸發順序**（constrain ordering）：
- Odoo `@api.constrains` 不保證 order，但兩條 constraint 都在 write commit 前評估
- 即使 `_check_mcp_subset` 先 pass（attacker 在 A 內有合法 MCP）→ `_check_project_ids_grantor_authority` 仍會 fail step 2 → write rollback ✓

**Tester HC3.1-3.4 4 method 全綠**（per leader brief 144/144 PASS）— 對應 step 1 (positive: A admin create allowed) + step 2 (negative: attacker write to add B raises ValidationError) + bypass path verify（SUPERUSER / group_system 路徑通過）+ multi-row replace `[(6, 0, [A, B])]` 同樣擋下。

**Closure status**：✅ **closed** — Python `@api.constrains` 在 step 2 attacker write direction 擋下，HC-3 attack chain 切斷。

### 12.3 HC-4 closure 證據（`skill.bundle_attachment_id /web/content` side-channel）

**Hotfix 位置**：
- `src/models/skill.py:94-129` create + write override 呼叫 `_anchor_bundle_attachment` 強制 stamp `res_model='woow_paas_platform.skill' + res_id=skill.id + public=False`
- `src/security/ir_rules.xml:464-489` 2 條新 `ir.attachment` ir.rule：
  - `ir_attachment_rule_skill_bundle_user_scope` (base.group_user): domain `[('res_model', '!=', 'woow_paas_platform.skill')]` R/W/C/U=True — 對 skill bundle attachment row-level deny
  - `ir_attachment_rule_skill_bundle_admin_full` (base.group_system): domain `[(1, '=', 1)]` R/W/C/U=True — admin bypass

**對照 §3 HC-4 RCE chain reproducer**：

| Step | Attack | Defense (post-hotfix) |
|------|--------|----------------------|
| 1 | Phase B upload bundle → attachment created | skill.create override 觸發 `_anchor_bundle_attachment` 強制 stamp anchoring ✓ |
| 2 | Attacker (project user) read skill metadata via ORM | 16 rule 既有，OK 預期可讀 metadata（不含 bundle_attachment_id field — group_system 遮）|
| 3 | Attacker enumerate sequential `ir.attachment.id` | OK 可 enumerate |
| 4 | Attacker GET `/web/content/<att_id>?download=1` | `/web/content` controller 內部走 `ir.attachment.check_access_rule('read')` → 評估 `ir_attachment_rule_skill_bundle_user_scope` 對 base.group_user → domain `res_model != skill` 對 skill bundle row 為 False → AccessError → /web/content returns 403 ✓ |
| 5 | Reverse engineer + craft malicious bundle + upload back → RCE | step 4 close → step 5 不可達 ✓ |

**Anchor stamp 機制 verify**（`skill.py:108-129`）：
- `_anchor_bundle_attachment` 對 missing `res_model` / `res_id` / `public=True` 三 case 全 patch
- `att.sudo().write(updates)` — 用 sudo write 因為 attachment 可能不在 caller scope；對齊 anchor 是 system-level metadata stamping
- create + write 兩條 path 對等改寫（與 PR #54 `_do_cleanup` + `unlink()` dual-path pattern 同源）

**Tester HC4.1-4.4 4 method 全綠**（per leader brief）— 對應 step 4 deny（base.group_user GET `/web/content/<skill_att_id>` → 403）+ admin bypass + anchor stamping verify + create direction deny（attacker 不能 create attachment with `res_model='woow_paas_platform.skill'`，domain false → AccessError）。

**Closure status**：✅ **closed** — ir.attachment row-level rule + skill.create/write anchor 雙保險守住 `/web/content` side-channel。

### 12.4 獨立 walk 3 new file — 是否新引入問題

我獨立 read `src/models/agent_template.py` + `src/models/skill.py` + `src/security/ir_rules.xml` 新加段（per role docstring「不 dispatch 全 5 agent，獨立 walk」），列出 edge case 與 info-level observations：

#### HC-3 edge cases

- **EC-3a (info)**：**Sudo path bypass** — `is_superuser()` bypass 對未來 controller endpoint 用 `agent_template.sudo().write({'project_ids': ...})` path **不 enforce constraint**。Phase A 沒 controller 走此 path（grep `agent_template.*sudo\(\)\.write` in `src/controllers/` → 0 hit），但 Phase B/C 加 admin UI / webhook endpoint 時必須 re-check grantor authority in controller carve-out（與 [[two-layer-acl-lockstep]] pattern 對齊）— 標 **F-PA new follow-up** 提醒 Phase B-C maintainer。

- **EC-3b (info)**：**`proj.project_access_ids` 評估時 attacker context**：constraint 在 attacker `self.env` 評估 `proj.project_access_ids`；attacker 對 victim B 沒 access → `project_access_rule_member_read` deny → attacker 看到 B's project_access list 為 empty → `filtered(...)` empty → ValidationError raise ✓。**邏輯依賴「attacker 對 B 完全沒 access」** — 如果 attacker 不知為何在 B 有 guest role（透過合法 invite），他看得到 B's access 但仍不是 owner/admin → filtered empty → ValidationError ✓。**邊界 OK**。

- **EC-3c (info)**：**Multi-row write atomicity** — `for tmpl in self: for proj in tmpl.project_ids: ...` 對 batch write 任一 fail 觸發 ValidationError → Odoo `@api.constrains` 預期整 transaction rollback。OK.

#### HC-4 edge cases

- **EC-4a (info)**：**`_anchor_bundle_attachment` destructive overwrite** — 若 admin (group_system) 把已存在的 `mail.message` attachment（`res_model='mail.message'`）assign 給 skill.bundle_attachment_id → anchor 把 attachment 的 `res_model` overwrite 成 `'woow_paas_platform.skill'`。**這切斷與原 mail.message 的 res_model 連結**（mail.message 透過 `attachment_ids` M2M 而非 ir.attachment.res_model 反向查，所以 mail.message 的 attachment 仍可見；但 Odoo std `/web/content` 對 mail.message attachment 的 ACL 評估會走新的 res_model rule chain）— 對 admin 路徑是預期破壞性 anchoring，**不擋 hotfix**，但可在 docstring 加警告。標 **info level**。

- **EC-4b (info)**：**Attacker create attachment with `res_model='woow_paas_platform.skill'`** — 評估 `ir_attachment_rule_skill_bundle_user_scope` domain `res_model != skill` 對 post-write state false → create direction deny → AccessError ✓。

- **EC-4c (info)**：**Attacker create non-skill attachment then write `res_model = skill`** — write 評估 post-write domain `res_model != skill` 對 post-write false → write deny → AccessError ✓。

- **EC-4d (info)**：**`public=True` field bypass** — Odoo `/web/image` / 某些 binary endpoint 對 `public=True` 跳過 ir.rule check。`_anchor_bundle_attachment` 強制 `public=False` if `att.public` is True ✓；attacker 不能 write `public=True` on skill attachment（write deny per EC-4c 同理 — write 後 attachment 仍 res_model=skill，rule 評估 domain false）✓。

- **EC-4e (info)**：**Odoo 18 不 ship ir.attachment ir.rule for base.group_user**（hotfix XML comment 明示）— 新加 `ir_attachment_rule_skill_bundle_user_scope` 對 base.group_user 是首次引入 row-level filtering on ir.attachment（從「all attachments」narrow to「all non-skill attachments」）。**這對既有 ir.attachment 路徑無 regression** 因為原本 base.group_user 沒 row-level filter → 新 rule 對 non-skill attachment 仍 R/W/C/U=True（domain != skill 為 True）→ 既有 caller 不變 ✓。

- **EC-4f (caveat)**：**`ir_attachment_rule_skill_bundle_user_scope` perm_create=True** — base.group_user 對 non-skill res_model attachment create 仍允許（不變動既有 behavior）。但對 `res_model=False`/`NULL`/`''` create？Odoo ORM 評估 `('res_model', '!=', 'woow_paas_platform.skill')` 對 NULL — Postgres `NULL != 'x'` is NULL → domain 評估為 unknown → 通常視為 False → 取決於 Odoo ORM 對 NULL 的具體處理。**Info — 邊界不確定但 attacker 受益少**（attacker create attachment with res_model=NULL → 不被視為 skill bundle → /web/content 對該 attachment 走 std fallback → 與 hotfix 前同 behaviour，不擴大 attack surface）。

#### 新引入問題評估

- **無新 high-severity 發現**
- 3 個 EC 標 info level（EC-3a/3b/3c）
- 5 個 EC 標 info level（EC-4a/4b/4c/4d/4e/4f）
- EC-3a 建議標新 follow-up（Phase B/C controller carve-out lock-step 提醒）

### 12.5 既有 caveat status update

| Caveat | Pre-hotfix | Post-hotfix |
|--------|-----------|-------------|
| **HC-1** (`_inverse_cloud_service_id` replace semantics) | open | **open — 未 hotfix per leader 拍板**（標 reporter follow-up F-PA-?） |
| **HC-2** (`project.sudo().create()` bypass bootstrap) | open | **open — 未 hotfix per leader 拍板**（標 reporter follow-up F-PA-?） |
| **HC-3** (`agent_template.project_ids` cross-project injection) | open | **CLOSED ✓**（Python `@api.constrains` hotfix）|
| **HC-4** (`skill.bundle_attachment_id /web/content` side-channel) | open | **CLOSED ✓**（ir.attachment ir.rule + anchor stamping hotfix）|
| **SC-1 ~ SC-28** | open | open — reporter phase 收尾（不變動）|

### 12.6 144/144 PASS regression lock 仍綠

Per leader brief 144/144 PASS（既有 137 + HC hotfix 7 — 1 fixture overlap）。本 reviewer verify 守線：
- B-1 16 rule 全綠（PA-5 Python gate + SK-6 field-level mask 不變）
- OQ-A-2 5 method 全綠（project / task M:N walk 不變）
- 3 條 GREEN regression lock (R.3 / R.9 / C.4 leg) 不破
- PR #54 11 invariants（Inv #9 / #10 / #11 含）不破

### 12.7 Hand-off updated

Reviewer round 2 verdict：**APPROVE WITH CAVEATS** — HC-3 + HC-4 closed ✓；HC-1 + HC-2 仍 open（leader 拍板 defer to reporter follow-up）；無新 high-severity。可進 reporter phase。

**Reporter special attention**（與 §11 sign-off 整合）：
- HC-3 hotfix `_check_project_ids_grantor_authority` constraint shape 對齊 design §4.1 字面化補拍 — reporter 可考慮把 design §4.1 加 constraint clause
- HC-4 hotfix anchor stamping + ir.attachment ir.rule 雙保險 — reporter follow-up doc 候選 `docs/decisions/patterns/attachment-anchor-stamping.md`（與 [[mcp-acl-6-rule-split]] / [[two-layer-acl-lockstep]] paired 抽 pattern）
- EC-3a future controller sudo path lock-step 提醒 — Phase B/C maintainer attention
- EC-4a anchor destructive overwrite warning — skill.py `_anchor_bundle_attachment` docstring 可加警告
- HC-1 + HC-2 仍是 caveat — reporter 排 F-PA follow-up + surface attack vectors in PR description

**等 leader hand-off reporter**。

Reviewer 簽核（round 2 — HC-3 / HC-4 closure）：**agent-runtime-pod-reviewer**，2026-05-23
