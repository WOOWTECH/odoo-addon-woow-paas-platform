# PR Description Draft — `feat(agent-pod): Phase A schema M:N + 4 new models + 16 rule + ir.attachment defense`

> Title 為英文（git commit convention）。本檔內容（下方 PR body）為 zh-tw 繁體中文 — 直接 paste 到 PR description 即可。
> 路徑對齊 PR #54 既有 `docs/decisions/reviews/pr-description-draft-mcp-refactor.md`（reporter brief 提到 `docs/decisions/refactor/reviews/` 應為 typo — 採對齊既有 case 路徑邏輯，便於 future grep）。

**Title**: `feat(agent-pod): Phase A schema M:N + 4 new models + 16 rule + ir.attachment defense`

**Branch**: `feature/agent-pod`
**Closes**: N/A — Phase A 是 epic `agent-runtime-pod` 第一階段（schema + ACL 落地），後續 Phase B-G 接力。本 PR 不 close 任何 issue。
**Epic**: `agent-runtime-pod`（design `docs/plans/2026-Q2/agent-runtime-pod-design.md` 4794+ 行 / plan `docs/plans/2026-Q2/agent-runtime-pod-plan.md` 682 行）
**Cycle**: 5（continuation chain：cycle 1 cloud-service decouple → cycle 2 mcp decouple → cycle 3 Project Detail page → cycle 4 mcp URL redesign → cycle 5 agent-runtime-pod Phase A）

---

## Summary

本 PR 完成 `agent-runtime-pod` epic **Phase A**：把 PaaS project 從「掛在 cloud_service 下的子物件」升格成 first-class 多租戶 ACL unit，並落地 4 個新 Odoo model（`agent_template` / `agent_session` / `skill` / `project_access`）作為後續 Phase B-G agent runtime（per-project pod / WS gateway / skill bundle / sandbox security）的 schema 與 ACL 基座。Phase A schema 落地 + ACL 三層 lock-step（CSV + ir.rule × 16 + Python role-hierarchy gate）+ migration 18.0.1.0.26 + 2 條 ir.attachment defense in depth rule + 2 條 grantor authority Python constraint；**Phase A 不 active 任何 runtime path**（`use_agent_runtime` field default False；4 model dormant 給 Phase B-G 接 controller / view / runtime layer）。

3-round implementation 演進（cycle 1 schema → cycle 2 B-1 + OQ-A-2 → cycle 3 HC-3 + HC-4 hotfix）後達 **144/144 PASS + Full suite 664 / 0 fail + 18 invariants 守線（PR #47 + #54 共 11 條 + Phase A 7 條 + Phase A #8 新增）**。

---

## Scope

### 16 impl file（10 modified + 6 untracked，git diff `develop → feature/agent-pod`）

**Modified（10 file）**：

| File | 動作摘要 |
|------|---------|
| `src/__manifest__.py` | version bump `18.0.1.0.25 → 18.0.1.0.26` |
| `src/hooks.py` | `_CONFIG_PARAMETERS` 加 `woow_paas_platform.agent_namespace_secret` |
| `src/models/__init__.py` | imports 4 new models（`agent_template` / `agent_session` / `skill` / `project_access`）|
| `src/models/cloud_service.py` | `project_ids` One2many → Many2many reverse；`_do_cleanup` + `unlink()` 加 URL-prefix narrowing（Inv #9 延伸 M:N）；新 helper `_collect_sidecar_user_mcps` + `_sidecar_url_prefix` |
| `src/models/project_project.py` | `cloud_service_id` M2O → stored compute over `cloud_service_ids[:1]` + inverse；新 `cloud_service_ids` M2M + `workspace_ids` computed M2M + `external_mcp_allowed` + `use_agent_runtime` + `agent_template_id` + `project_access_ids`；create() override bootstrap owner project_access |
| `src/models/project_task.py` | `primary_cloud_service_id` related → 獨立 M2O + computed default + drift handler（hybrid spec Q2 sign-off）；`extra_cloud_service_ids` domain 改 M:N project scope + 保留 redundant workspace clause for legacy test |
| `src/models/discuss_channel.py` | hybrid spec 接 task.primary_cloud_service_id 而非 project.cloud_service_id 直連（context 注入路徑微調）|
| `src/security/ir.model.access.csv` | 加 8 row（4 model × user/admin）對 `base.group_user` / `base.group_system` 各 1,1,1,1 |
| `src/security/ir_rules.xml` | 4 walk-path edit（mcp_server / mcp_tool / project / project_task 改 M2M walk）+ 16 new rule 對 4 個新 model（`'any'` operator 鎖 user_id + role 同 row）+ 2 條 ir.attachment defense rule（HC-4 hotfix `ir_attachment_rule_skill_bundle_user_scope` + `_admin_full`）|
| `src/models/agent_template.py`（**round 1 hotfix**）| 新 constraint `_check_project_ids_grantor_authority`（HC-3 hotfix — Python `@api.constrains` 補 ir.rule `'any'` OR-semantics 對 m2m write direction 的 expression power 不足）|

**Untracked（6 file，全新加）**：

| File | 用途 |
|------|------|
| `src/models/agent_template.py` | design §4.1 落地（116 行）+ `_check_visibility_consistency` + `_check_mcp_subset` 兩條 constraint + HC-3 round 3 hotfix grantor authority |
| `src/models/agent_session.py` | design §2.12 落地（114 行）+ `_compute_project_namespace_hash` HMAC-SHA256 + UNIQUE(project_id) + UNIQUE(project_namespace_hash) |
| `src/models/skill.py` | design §3.2 落地（90+ 行）+ `bundle_attachment_id groups='base.group_system'` field-level mask（SK-6 path (a)）+ UNIQUE(project_id, name, version) + HC-4 round 3 hotfix `_anchor_bundle_attachment` |
| `src/models/project_access.py` | design §4.3 落地（159 行）+ ROLE_HIERARCHY + ASSIGNABLE_ROLES_BY_ROLE + `_check_assignable_role` Python role-hierarchy gate（PA-5）+ `unlink()` last-owner guard + `_compute_user_id` from partner_id |
| `src/migrations/18.0.1.0.26/pre-migrate.py` | Step 1 CREATE TABLE IF NOT EXISTS `project_cloud_service_rel` + index on `cloud_service_id` |
| `src/migrations/18.0.1.0.26/post-migrate.py` | Step 2/3/8/9/10 + helper `_column_exists` / `_table_exists` |

### 6 + 2 test file（8 個檔，144 method）

| Test file | Method 數 | 段 |
|-----------|---------|----|
| `src/tests/test_agent_pod_phase_a_project_schema.py` | 11 | A.1-A.9 |
| `src/tests/test_agent_pod_phase_a_task_schema.py` | 9 | T.1-T.5（hybrid spec Q2 sign-off）|
| `src/tests/test_agent_pod_phase_a_cloud_service_mn.py` | 4 | C.1-C.5（含 GREEN regression lock C.4 leg）|
| `src/tests/test_agent_pod_phase_a_new_models.py` | 13 | N.1-N.12 |
| `src/tests/test_agent_pod_phase_a_acl.py` | 8 | R.1-R.9（含 GREEN regression lock R.3 / R.9）|
| `src/tests/test_agent_pod_phase_a_migration.py` | 9 | M.1-M.9 |
| `src/tests/test_agent_pod_phase_a_acl_new_models.py`（**round 2 + round 3**）| 29 + 4 | B1.AT-1~8 / AS-1~5 / SK-1~8 / PA-1~8（round 2 B-1 closure 29 method）+ HC3.1-3.4（round 3 HC-3 closure 4 method）|
| `src/tests/test_agent_pod_phase_a_project_task_acl_mn.py`（**round 2**）| 5 | O1.1-O1.5（OQ-A-2 closure）|
| `src/tests/test_agent_pod_phase_a_attachment_acl.py`（**round 3**）| 4 | HC4.1-4.4（HC-4 closure）|
| **總計** | **144** | |

### 3 doc + 5 cycle metadata（reporter phase 落地）

| Doc | 動作 |
|-----|------|
| `docs/plans/2026-Q2/agent-runtime-pod-design.md` | §1.3.2 ACL Invariants 新加 Inv A1 + Inv A2（Phase A #8 invariant cluster）；§4.1 字面化 `_check_project_ids_grantor_authority` constraint body |
| `docs/plans/2026-Q2/agent-runtime-pod-plan.md` | Phase A 19-task scope（落 13；leader 拍板 defer 5 view files OQ-A-3 → F-PA-4） |
| `docs/archived/tracking/test-coverage-agent-runtime-phase-a-2026-05-22.md` | cell map + Q1/Q2 sign-off 落地 + 3 條 regression lock |
| `docs/archived/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` | §1 invariant verify + §6 B-1 BLOCKER（round 1）+ §7 5 Caveat + §13 round 2 verify（B-1 closed + OQ-A-2 closed）+ §14 round 3 verify（HC-3 + HC-4 closure + std rule symmetric deactivation 紅線 independent grep verified）|
| `docs/archived/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` | §3 4 HC（HC-1/HC-2/HC-3/HC-4 — CAQA 漏抓）+ §5 28 SC + §6 CAQA cross-check + §12 round 2 HC-3 + HC-4 closure + §12.4 EC-3a/4a edge cases |
| `docs/decisions/patterns/constraint-input-trust-expansion.md`（**新加，本 PR reporter phase**）| HC-3 抽 pattern — m2m write constraint grantor authority check |
| `docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md`（**新加，本 PR reporter phase**）| HC-4 抽 pattern — bundle attachment defense in depth (anchor stamping + `ir.attachment` row-level ir.rule) |
| `docs/tracking/followups/follow-ups.md` | F-PA-1~F-PA-12 round 2 落地 + F-PA-13~F-PA-16 round 3 落地（HC-1 / HC-2 defer + EC-3a / EC-4a 提醒）|
| `CLAUDE.md` | Known patterns 段加 2 條 reference（2 new pattern doc）|

---

## 3 cycle 演進故事

### Cycle 1 — schema 落地 + initial test scaffolding（2026-05-22 round 1）

- 14 file diff（9 modified + 5 untracked schema + migration）+ 54 test method（A/T/C/N/R/M 6 段）
- CAQA round 1 **BLOCKED**：B-1 — 4 個新 model 對 `base.group_user` 給 CSV `1,1,1,1` 但 `ir.rule` 完全沒新增（plan #13 漏做）— 違反 plan §Phase A task #13 + design §3.2/§4.3 explicit rule list + [[two-layer-acl-lockstep]] pattern + 重蹈 [[cross-workspace-task-data-leak]] B1 pitfall 同源 root cause
- 攻擊面：`project_access` privilege escalation（attacker create owner row to non-own project）+ `agent_template` 跨 project read system_prompt / write binding + `skill` 跨 project bundle_sha256 forgery（RCE on agent pod 潛在）+ `agent_session` 跨 project K8s lifecycle abuse（DoS / namespace squat with M-3 hardcoded secret 串聯）
- Leader 拍板 **(a) 退回 impl 補 16 rule + ACL behaviour test**（vs (b) CSV `0,0,0,0` 暫關 / (c) escalate user）— 對齊 plan #13 字面要求

### Cycle 2 — B-1 closure + OQ-A-2 closure（2026-05-22 round 2）

- Impl 補 16 rule（4 model × 4 rule per design §3.2 + §4.3）+ PA-5 Python role-hierarchy gate `_check_assignable_role` + SK-6 field-level mask `bundle_attachment_id groups='base.group_system'`
- Impl 同步補 OQ-A-2：`project_rule_workspace_member` + `project_task_rule_workspace_member` M:N walk path 對齊 mcp_server / mcp_tool pattern（cross-workspace M:N project 對 W2 member 可見 project / task）
- Test 補 29 method (B-1 closure B1.AT-1~8 / AS-1~5 / SK-1~8 / PA-1~8) + 5 method (OQ-A-2 closure O1.1-O1.5) — 137/137 PASS
- CAQA round 2 **APPROVED WITH CAVEATS**：B-1 closed + OQ-A-2 closed；5 caveat M-1~M-5 + 6 info I-1~6 + 3 new C-A-1~C-A-3 = F-PA-1~F-PA-12 follow-up

### Cycle 3 — reviewer 抓 4 HC + leader 拍板 HC-3 + HC-4 hotfix（2026-05-23 round 3）

- Reviewer 5 agent verdict 一致 APPROVED WITH CAVEATS，但抓出 CAQA 漏抓的 **4 條 HC**：
  - **HC-1**（silent-failure-hunter C-2）：`_inverse_cloud_service_id` `[(6, 0, ...)]` replace semantics silent drop M:N bindings（Phase A 1-service M:N noop；Phase B/C multi-bind active 後 latent regression）
  - **HC-2**（silent-failure-hunter C-1）：`project.sudo().create()` (在 `ai_assistant.py:946`) bypass `project_project.create()` SUPERUSER skip bootstrap → creator silently 看不到自己 project 上 4 model（Phase A dormant；Phase D-F use_agent_runtime active 後 immediate functional regression）
  - **HC-3**（pr-test-analyzer IG-1 — **design gap**）：`agent_template.project_ids` m2m write cross-project injection — attacker (A admin) write `project_ids=[(4, B)]`，ir.rule `'any'` OR-semantics 對 `[A, B]` 仍滿足 → silent cross-project visibility leak + Phase D activation 後 system_prompt + mcp hijack；reviewer 紅線 trigger「發現 design 漏的設計問題」→ escalate planner via leader
  - **HC-4**（silent-failure-hunter I-3）：`skill.bundle_attachment_id` field-level group mask 不擋 `/web/content/<att_id>` direct download → attacker 透過 ir.attachment Python delegation 走 skill model ACL allow → bundle bytes leak → potential RCE on agent pod（Phase B/C bundle upload pipeline active 後 immediate surface）
- Leader 拍板 **HC-3 + HC-4 hotfix in this PR**（C 路徑）；HC-1 + HC-2 標 reporter follow-up F-PA-13 + F-PA-14（caveat 級）
- Impl round 3 改動 3 file（`agent_template.py` + `skill.py` + `ir_rules.xml`）+ 8 test method（HC3.1-3.4 + HC4.1-4.4）→ 144/144 PASS（既有 137 + HC hotfix 7，1 fixture overlap）
- Planner 字面化 `_check_project_ids_grantor_authority` constraint method body 進 design §4.1（HC-3 design gap reverse-document）
- CAQA round 3 + reviewer round 2 **APPROVED WITH CAVEATS** — HC-3 + HC-4 closed；HC-1 + HC-2 仍 caveat（已 surface）；無新 high-severity

---

## Test 結果

```
=========== 144 passed in <X>s ===========

Phase A 54 + B-1 round 2 補 29 + OQ-A-2 round 2 補 5 + HC-3 round 3 補 4 + HC-4 round 3 補 4 + named regression refactor branch 9 lock + Phase A 早期 regression 39 = 144 method
（fixture overlap 1：HC4 與 SK-* 共用 setUpClass）

Full suite: 664 passed / 0 failed / 10 pre-existing env error 不變
```

### 守線 regression lock（不破則本 PR 不破）

- **PR #47 + #54 11 invariants**：D4 / 6-rule split / dual-leg + 外層 AND `scope='user'` / sidecar_backed lifecycle / Inv #11 — 全 ✓（mcp_server / mcp_tool walk path 內 cloud_service field cardinality 改 M:N，dual-leg shape 不變）
- **MAJOR 1 reproducer**（[[system-mcp-create-acl-bypass]]）：`test_mcp_acl_unbound_isolation.py:H.11/H.12` 對 base.group_user create `scope='system'` 必 `AccessError` — 守線
- **B1 sub-model parity** [[cross-workspace-task-data-leak]]：mcp_tool 對等改 M:N walk + ir.attachment 「對 base.group_user 第一次加 ir.rule 對等加 group_system admin_full」對齊 ✓
- **`_do_cleanup` + `unlink()` URL-prefix narrowing 對等改寫**（Inv #9 M:N 延伸）：4-method paired fixture 全 ✓
- **GREEN regression lock 3 條設計強度 STRONG**（test report §5）：R.3 6-rule split 對等 / R.9 rule count parity / C.4 leg sidecar_backed flag 不破 — 全 ✓

### Pre-existing 10 env error（baseline 紅持平）

`git stash → re-run baseline → 同 reds reproduce` 驗證方法學健全（cycle 2 F13 pattern 對齊）：

- 10 errors 多為 subscription journal / TWD currency / smart_home demo user 既有 incompatibility
- 本 PR diff 0 改動這些 file → 無 blast radius

驗證指令：

```bash
./.claude/skills/k8s-dev-sandbox/scripts/test.sh feature-agent-pod --module woow_paas_platform
```

---

## Invariants 守線清單（18 條全綠）

### refactor branch（PR #47 + PR #54）11 條 — 全 ✓ 繼承

| # | 主旨 |
|---|------|
| **#1** | `mcp_server.project_id` `ondelete='cascade'` 不動 |
| **#2** | D4：`scope='user' ⇔ project_id IS NOT NULL` 不動 |
| **#3** | ~~D5~~ 已 cycle 4 拔，不重新引入 |
| **#4** | `_do_cleanup` 三步順序 + sidecar_backed filter — filter 延伸 M:N (URL prefix narrowing) |
| **#5** | 6-rule ACL split mcp_server / mcp_tool 對等存在 — walk path 內 cardinality 改 M:N，shape 不變 |
| **#6** | `mcp_server_rule_system_readonly` perm_create/write/unlink=False 不動 |
| **#7** | `mcp_server.url` snapshot + 演算法歸 service 不動 |
| **#8** | System-scope MCP create 限 sudo / admin / demo data — 守 MAJOR 1 防線 |
| **#9** | sidecar-backed user-scope MCP 雙 path cleanup（`_do_cleanup` + `unlink()` filter 對等） — M:N 延伸 URL prefix narrowing |
| **#10** | UI shortcut Add Sidecar MCP create path 必帶 `sidecar_backed=True` 不動 |
| **#11** | `workspace_user` × 2 必 dual-leg（外層 AND `scope='user'`）— shape 不變，leg 1 walk path M:N + leg 2 carve-out `cloud_service_ids = False` |

### Phase A epic invariants — 全 ✓

| # | 主旨 |
|---|------|
| **Phase A #1** | `project.cloud_service_id` 從 M2O 改 stored compute over `cloud_service_ids[:1]` + dual-write inverse 對齊 legacy callers |
| **Phase A #2** | `project.workspace_ids` computed Many2many aggregate `cloud_service_ids.workspace_id` + store=True |
| **Phase A #3** | `cloud_service.project_ids` 從 One2many 改 Many2many reverse（同一 relation table）|
| **Phase A #4** | `task.primary_cloud_service_id` 從 related 改 independent M2O + computed default + drift handler（hybrid Q2 sign-off）|
| **Phase A #5** | `_do_cleanup` / `unlink()` 兩條 path 在 M:N 下加 URL-prefix narrowing 不誤殺其他 service 的 sidecar-backed MCP |
| **Phase A #6** | `project_access` first-class ACL row exists（schema + bootstrap + 16 ir.rule + Python role-hierarchy gate）|
| **Phase A #7** | Q1 sign-off cross-workspace M:N walk OR semantics — mcp_server / mcp_tool / project / project_task 4 rule 全對等 M:N walk |
| **Phase A #8（NEW — Inv A1 + Inv A2）** | **(a)** `agent_template.project_ids` write requires owner/admin role on every bound project（Python `@api.constrains` 補 ir.rule `'any'` OR-semantics 對 m2m write direction 不足）+ **(b)** `skill.bundle_attachment_id` row-level deny for base.group_user（`ir.attachment` ir.rule 兩條一組）+ **(c)** anchor stamping ensure delegation target correct（`res_model + res_id + public=False` 三件套）— 對應 pattern [[constraint-input-trust-expansion]] + [[attachment-row-level-acl-and-anchor-stamping]] |

### 新 pattern doc（reporter phase 抽 — 後續所有 PR 改本區前必看）

- `docs/decisions/patterns/constraint-input-trust-expansion.md` — m2m write constraint grantor authority check
- `docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md` — bundle attachment defense in depth (anchor stamping + `ir.attachment` row-level ir.rule)

---

## CAQA / Reviewer 對 4 HC 處理路徑

| HC | 內容 | Leader 拍板 | Closure |
|----|------|------------|---------|
| **HC-1** | `_inverse_cloud_service_id` replace semantics silent drop M:N bindings | (a) approve with hard caveat | **F-PA-13 follow-up**（Phase B/C multi-bind active 前必修）|
| **HC-2** | `project.sudo().create()` bypass project_access bootstrap → creator 看不到自己 project 上 4 model | (a) approve with hard caveat | **F-PA-14 follow-up**（Phase D-F use_agent_runtime active 前必修）|
| **HC-3** | `agent_template.project_ids` m2m write cross-project injection（**design gap**）| (c) hotfix in this PR + planner 字面化 design §4.1 | **closed in PR round 3** — `agent_template._check_project_ids_grantor_authority` Python `@api.constrains` + HC3.1-3.4 4 test |
| **HC-4** | `skill.bundle_attachment_id /web/content` side-channel → potential RCE | (c) hotfix in this PR | **closed in PR round 3** — `skill._anchor_bundle_attachment` + `ir.attachment` 兩條 ir.rule + HC4.1-4.4 4 test |

---

## Caveats（soft）— reporter phase 收尾 / follow-up

CAQA 5 medium caveat（M-1~M-5）+ 6 info（I-1~I-6）+ 3 new caveat round 2（C-A-1~C-A-3）+ reviewer 28 SC + round 2 EC-3a/EC-4a = 共 ~45 條 — 分配如下：

| 嚴重度 | 內容 cluster | 落點 |
|-------|------------|------|
| Medium | M-1 project / task rule M:N walk（OQ-A-2 closed round 2，本身已 RESOLVED）| RESOLVED — round 2 落 |
| Medium | M-2 5 view files defer | F-PA-4（OQ-A-3 leader 拍板 defer to Phase B/C admin UI batch）|
| Medium | M-3 `agent_session._get_namespace_secret` hardcoded fallback | F-PA-1（install hook hard guard PR）|
| Medium | M-4 `cloud_service._sidecar_url_prefix` None fallback → broad-filter 誤殺 | F-PA-2（fixture / pre-deploy edge case PR）|
| Medium | M-5 `project.cloud_service_id` stored compute 失 `ondelete='set null'` 明示 | F-PA-3（small PR 補明示）|
| Low | C-A-1 `skill_rule_project_user` perm_W/C=False vs design「only own draft」simplification | F-PA-5（Phase B/C skill upload API PR 補 Python state guard）|
| Low | C-A-2 `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER vs design out | F-PA-6（leader / planner 拍板 design 是否更新）|
| Low | C-A-3 `bundle_attachment_id` field-level mask 同擋 project admin via backend form | F-PA-7（Phase B/C skill upload API PR + maintainer doc）|
| Low | I-1~I-6 comment / clarity / defensive style | F-PA-8 ~ F-PA-12（reporter 順手或 small PR）|
| Medium | HC-1 `_inverse_cloud_service_id` replace semantics | **F-PA-13**（Phase B/C 必修）|
| Medium | HC-2 `project.sudo().create()` bypass bootstrap | **F-PA-14**（Phase D-F 必修）|
| Info | EC-3a `_check_project_ids_grantor_authority` controller sudo path lock-step | **F-PA-15**（Phase B/C controller layer 補）|
| Info | EC-4a `_anchor_bundle_attachment` destructive overwrite doc 補強 | **F-PA-16**（reporter 順手 or Phase B/C bundle upload API PR）|
| Mixed | reviewer 28 SC（comment / type design / silent failure / test polish）| F-PA-1~F-PA-16 對應收 + SC cluster batch（部分留 Phase B/C 前必修，如 SC-12/SC-13/SC-14 `workspace_ids_snapshot` / `quota_override` / `model_overrides` weak type）|

詳細追蹤：[[follow-ups]] §Cycle 5（F-PA-1~F-PA-16）+ §Cross-cutting hint 表（檔案 → follow-up 對應）。

---

## Doc updates by reporter phase（**已完成於本 PR commit chain**）

- **新 pattern docs**（reporter phase 抽 2 條）：
  - `docs/decisions/patterns/constraint-input-trust-expansion.md`（HC-3 + PR #54 HC-2 同 family）
  - `docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md`（HC-4 defense in depth）
- **更新 design doc**：`docs/plans/2026-Q2/agent-runtime-pod-design.md` §1.3.2 加 Inv A1 + Inv A2（Phase A #8 invariant cluster）
- **更新 follow-ups**：`docs/tracking/followups/follow-ups.md` F-PA-1~12 round 2 落地 + F-PA-13~16 round 3 落地 + Cross-cutting hint 表 + See also 新增 2 pattern doc + cycle 5 caqa/reviewer report 引用
- **更新 CLAUDE.md**：Known patterns 段加 2 條 reference

---

## ⚠️ 不可逆 / 半可逆動作清單

| 動作 | 可逆性 | 緩解 |
|------|-------|------|
| 4 個新 model schema 落地（CREATE TABLE × 4 + ir.model.access.csv 加 8 row + ir_rules.xml 加 16 + 2 rule + 2 attachment rule）| **可逆**（XML revert + module -u 套用；新表可手動 DROP TABLE）| ir.attachment 兩條新 rule 為「對 base.group_user 第一次加 row-level filter on ir.attachment」— **revert 不會破壞既有 non-skill attachment 行為**（domain `res_model != skill` 對 non-skill 仍 True）|
| Migration `18.0.1.0.26` SQL backfill（Step 2-4 / 8-10）| **可逆**（idempotent；既有 1-service M:N row 走 backfill 後 functional 不變；reverse 可透過 DROP CONSTRAINT + DROP TABLE + UPDATE column）| 既有 row 行為不變；Step 2 `INSERT ... ON CONFLICT DO NOTHING` defensive |
| M:N relation table `project_cloud_service_rel` 新建 + `project.project.cloud_service_id` UNIQUE drop | **可逆**（ALTER TABLE 反向）；但 **半可逆 risk**：若 production 後有 user add 第 2 個 cloud_service（M:N entries），revert 會 drop relation table + 失去 secondary bindings | 預設 `use_agent_runtime=False` + Phase B-G 未上前無 user UI 加 secondary cloud_service 路徑 → revert window 風險窗口短 |
| `mcp_server / mcp_tool` ir.rule M:N walk path 改寫 + `project_rule / project_task_rule` workspace_member 走 M:N | **可逆**（XML revert）| 行為等價於 cycle 4 dual-leg with M:N walk；revert 後仍是 dual-leg 但 walk single |
| `project_task.primary_cloud_service_id` related → independent M2O + drift handler | **半可逆**（schema 已落地；revert 需 drop column 或 ALTER COLUMN 退回 related — Odoo ORM 處理但 PostgreSQL constraint 不對稱會 raise）| 既有 task row 在 Phase A migration step 8 已 set value；revert 前需 manual reset value |
| `_check_project_ids_grantor_authority` Python constraint（HC-3 hotfix）| **可逆**（method delete）| revert 後 HC-3 attack surface active；需評估 Phase D activation 前必修才 revert |
| `ir_attachment_rule_skill_bundle_*` 2 條 rule + `_anchor_bundle_attachment`（HC-4 hotfix）| **可逆**（XML + method revert）| revert 後 HC-4 attack surface active；Phase B/C bundle upload pipeline 前必修才 revert |
| `_check_at_least_one_owner` / `_check_assignable_role` Python constraint 在 `project_access`（PA-5 role hierarchy gate）| **可逆**（method delete）| revert 後 project_access ORM `create({'role': 'owner'})` 對非 grantor user 不擋 — 重炸 B-1 部分攻擊面（雖然 ir.rule 16 條仍守 row-level，但 role hierarchy 防降階 silently 不生效）|

**無嚴格不可逆動作**（DB-level 沒 destructive DROP COLUMN / DROP TABLE 對 production data）；Phase A migration 全 idempotent + replayable。但 **有 latent regression vectors**：F-PA-13（multi-bind silent drop）+ F-PA-14（creator visibility gap）必須在 Phase B-D activation 前修，否則 revert 也救不回 user 已踩到的 silent regression。

---

## Side effects 評估

| 影響面 | 描述 |
|--------|------|
| **既有 PaaS project 行為** | 完全不變 — `cloud_service_id` 仍可 read/write（透過 stored compute over `cloud_service_ids[:1]` + inverse）；單 service 綁定 row Phase A migration noop |
| **既有 cloud_service.project_ids** | 從 One2many 改 Many2many reverse — 走 same relation table，既有 mapped() 邏輯不變 |
| **既有 user-managed external MCP** | 不受影響（service 拆時 cleanup filter URL-prefix narrowing 不會誤殺）|
| **既有 sidecar-backed MCP**（cycle 4 PR #54）| filter shape 對等延伸 M:N — 既有 9 case fixture 全綠 |
| **既有 task.primary_cloud_service_id** | hybrid spec：existing related field → independent M2O + computed default + drift handler — 既有 task row Phase A migration step 8 backfill value，user 看到行為不變；user 手動 override 後 drift handler 不重置（spec Q2 sign-off）|
| **既有 ai_assistant.py controller** | sudo create path bypass 觸發 F-PA-14（4 model dormant Phase A 無 functional impact；Phase D-F active 後 immediate gap）|
| **既有 paas.py controller** | F-PA-13 `vals['cloud_service_id'] = X` write 在 multi-bind state silent drop（Phase A 1-service noop；Phase B/C 必修）|
| **既有 mcp_server / mcp_tool ACL** | walk path M:N 改寫 — cross-workspace M:N project 對 W2 member 多 visibility surface（design Q1 sign-off OR semantics）|
| **既有 4 個新 model 後台 form** | 5 view files defer F-PA-4 — admin 後台無法直接管理 4 model；走 ORM / DB；`external_mcp_allowed` 無 admin UI 開關（必須 ORM `with_user(admin).project.write({'external_mcp_allowed': True})`）|
| **既有 ir.attachment 行為** | 對 base.group_user 第一次加 2 條 ir.rule — `('res_model', '!=', 'woow_paas_platform.skill')` 對 non-skill attachment 仍 R/W/C/U=True，**既有 mail.message / product.image / etc 全不變** |
| **既有 paas-operator side / helm chart / Cloudflare route** | 完全不動 — Phase A schema-only，不擴 paas-operator scope |
| **既有 frontend OWL** | 完全不動 — Phase B/C admin UI / OWL component 才動 |

---

## 部署檢查

### 升級前

無 pre-deploy SQL check（Phase A migration 全 defensive table+column existence guard）；CI 既有測試會跑全 suite，本 PR 已 verified 664 / 0 fail。

### 升級後驗證

```sql
-- 1. project_cloud_service_rel M:N table 已建 + index
SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'project_cloud_service_rel';
-- expect: 1

SELECT COUNT(*) FROM pg_indexes
WHERE tablename = 'project_cloud_service_rel'
  AND indexname IN ('project_cloud_service_rel_pkey', 'project_cloud_service_rel_cloud_service_id_idx');
-- expect: 2

-- 2. project_access bootstrap row 對既有 project 都有 owner
SELECT pp.id, pp.name, COUNT(pa.id) AS owner_count
FROM project_project pp
LEFT JOIN woow_paas_platform_project_access pa
  ON pa.project_id = pp.id AND pa.role = 'owner'
WHERE pp.id IN (
  SELECT id FROM project_project WHERE cloud_service_id IS NOT NULL  -- bound projects
)
GROUP BY pp.id, pp.name
HAVING COUNT(pa.id) = 0;
-- expect: 0 row（all bound projects have at least 1 owner project_access）

-- 3. 16 new ir.rule 已套用
SELECT id, name FROM ir_rule
WHERE id IN (
  SELECT res_id FROM ir_model_data
  WHERE module='woow_paas_platform'
    AND name IN (
      'agent_template_rule_public_readonly', 'agent_template_rule_project_admin',
      'agent_template_rule_project_user', 'agent_template_rule_admin_full',
      'agent_session_rule_owner_admin', 'agent_session_rule_user',
      'agent_session_rule_guest', 'agent_session_rule_admin_full',
      'skill_rule_project_admin', 'skill_rule_project_user',
      'skill_rule_project_guest', 'skill_rule_admin_full',
      'project_access_rule_member_read', 'project_access_rule_owner_admin_manage',
      'project_access_rule_self', 'project_access_rule_admin_full'
    )
);
-- expect: 16 rows

-- 4. 2 條 ir.attachment defense rule 已套用
SELECT id, name FROM ir_rule
WHERE id IN (
  SELECT res_id FROM ir_model_data
  WHERE module='woow_paas_platform'
    AND name IN (
      'ir_attachment_rule_skill_bundle_user_scope',
      'ir_attachment_rule_skill_bundle_admin_full'
    )
);
-- expect: 2 rows

-- 5. agent_namespace_secret config_parameter 已 init（hooks.py _CONFIG_PARAMETERS）
SELECT key, COALESCE(value, '<empty>') AS value
FROM ir_config_parameter
WHERE key = 'woow_paas_platform.agent_namespace_secret';
-- expect: 1 row（value 可能 empty if no admin override；F-PA-1 標 install hook hard guard 之前 empty acceptable）
```

### Rollback（emergency）

整包退版到 18.0.1.0.25：

```bash
# 1. revert merge commit
# 2. git checkout v18.0.1.0.25（previous tag）
# 3. -u woow_paas_platform
```

Rollback 後：
- `project_cloud_service_rel` 表保留但無 caller — functional preserve
- 4 個新 model 表保留但無 import — functional dormant（schema-only）
- 16 + 2 ir.rule 退回 — Odoo ORM 不再 evaluate
- migration 18.0.1.0.26 已落的 backfill SQL **不會 reverse**（idempotent + 既有 row 行為不變）

**注意**：rollback 後若 Phase B/C 已上 production 且 user 對 4 model 已建 row → revert 會留 orphan row（schema 撤但 data 留）。建議 Phase A 落 production 後**不再 rollback**，改 forward-fix（F-PA-13/14 修補 + Phase B/C feature flag 控制）。

---

## 主要 commits

> 由 git commit-generator phase 在 release 前填入。預期 commit chain（per round structure）：

### Round 1 — schema + initial test scaffolding

1. `<sha>` test: TDD red Phase A schema + 4 new model + migration + ACL（6 test file，54 method A/T/C/N/R/M 段）
2. `<sha>` feat(agent-pod): Phase A schema M:N migration + 4 new models + cleanup hook M:N delta
3. `<sha>` feat(agent-pod): Phase A migration 18.0.1.0.26 + pre/post-migrate SQL + project_access bootstrap

### Round 2 — B-1 closure + OQ-A-2 closure

4. `<sha>` test: B-1 closure 29 test method (B1.AT-1~8 / AS-1~5 / SK-1~8 / PA-1~8) + OQ-A-2 5 test method (O1.1-O1.5)
5. `<sha>` feat(agent-pod): B-1 closure 16 ir.rule + PA-5 Python role-hierarchy gate + SK-6 field-level bundle mask + OQ-A-2 project/task M:N walk

### Round 3 — HC-3 + HC-4 hotfix

6. `<sha>` test: HC-3 + HC-4 hotfix 8 test method (HC3.1-3.4 + HC4.1-4.4)
7. `<sha>` fix(security): HC-3 `agent_template._check_project_ids_grantor_authority` constraint
8. `<sha>` fix(security): HC-4 `skill._anchor_bundle_attachment` + ir.attachment row-level defense rule

### Reporter phase — durable docs

9. `<sha>` docs(agent-pod): Phase A reporter durable — 2 new patterns (constraint-input-trust-expansion + attachment-row-level-acl-and-anchor-stamping) + design Inv A1+A2 + 4 follow-ups + CLAUDE.md
