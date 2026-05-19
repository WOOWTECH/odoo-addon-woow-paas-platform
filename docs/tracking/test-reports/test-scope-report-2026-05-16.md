---
name: test-scope-report-2026-05-16
phase: red
team: refactor
tester: refactor-tester
plan_ref: docs/refactor/plan-cloud-service-project-decouple.md
sandbox: dev-sandbox-feat-decouple-cs-projects (docker-desktop, local mode)
created: 2026-05-16T00:00:00Z
updated: 2026-05-17T01:10:00Z
status: red-captured
---

# Test Scope Report — Decouple CloudService ↔ Project (Phase 2, TDD red)

## TL;DR

5 new test files cover all 25 acceptance criteria, plus 7 explicit regression locks. Red captured on `dev-sandbox-feat-decouple-cs-projects` (docker-desktop, local hostPath mode): **25 refactor-scope failures** with correct failure modes (`AssertionError` / `KeyError` on missing fields). 7 regression tests pass as expected. 32 additional failures in the full module run are pre-existing infra issues (`Subscription journal is not configured`) and are out of scope for this phase. Frontend-contract tests (AC #20-25) use file-content assertions on the static assets; full E2E (playwright-cli) is intentionally deferred to a later layer as per plan.

## Acceptance criteria → test mapping

| AC # | Test file | Class.test_method |
|---|---|---|
| 1 | `src/tests/test_project_decouple.py` | `TestProjectDecouple.test_ac1_create_unbound_project_is_allowed` |
| 2 | `src/tests/test_project_decouple.py` | `TestProjectDecouple.test_ac2_unique_cloud_service_constraint_preserved` |
| 3 | `src/tests/test_project_decouple.py` | `TestProjectDecouple.test_ac3_bound_project_workspace_id_unchanged` |
| 4 | `src/tests/test_project_decouple.py` | `TestProjectUnboundACL.test_ac4_unbound_project_invisible_to_non_member` |
| 5 | `src/tests/test_project_decouple.py` | `TestProjectUnboundACL.test_ac5_unbound_project_visible_after_member_add` |
| 6 | `src/tests/test_project_decouple.py` | `TestProjectUnboundACL.test_ac6_bound_project_visible_to_workspace_member` |
| 7 | `src/tests/test_cloud_service_acl.py` | `TestCloudServiceWorkspaceRule.test_ac7_bob_cannot_read_w1_service_via_orm` (+ `_browse_w1_read_raises_access_error` / `_write_w1_service_via_orm` / `_unlink_w1_service_via_orm` / `_bob_can_read_own_w2_service`) |
| 8 | `src/tests/test_cloud_service_acl.py` | `TestCloudServiceControllerRegression.test_ac8_list_services_endpoint_unchanged_for_member` + `_denied_for_outsider` |
| 9 | `src/tests/test_task_service_extras.py` | `TestTaskExtrasFieldsExist.test_ac9_extra_cloud_service_ids_field_exists` + `_extras_can_be_empty` |
| 10 | `src/tests/test_task_service_extras.py` | `TestTaskExtrasFieldsExist.test_ac10_primary_cloud_service_related_to_project` + `_updates_when_project_rebound` |
| 11 | `src/tests/test_task_service_extras.py` | `TestTaskExtrasDomain.test_ac11_extras_field_has_workspace_scoped_domain` |
| 12 | `src/tests/test_task_service_extras.py` | `TestTaskExtrasRecordRuleEnforcement.test_ac12_cross_workspace_extra_invisible_or_rejected` + `_same_workspace_extra_accepted` |
| 13 | `src/tests/test_task_service_extras.py` | `TestTaskCrossProjectMove.test_ac13_extras_preserved_on_cross_project_move` + `_incompatible_extras_flag_exposed` |
| 14 | `src/tests/test_chatter_service_injection.py` | `TestPrimaryAndExtrasMerge.test_ac14_primary_then_extras_merged_with_dedup` |
| 15 | `src/tests/test_chatter_service_injection.py` | `TestHelmValuesSanitizer.test_ac15_helm_values_password_redacted` + `_token_and_api_key_patterns_redacted` |
| 16 | `src/tests/test_chatter_service_injection.py` | `TestHelmValuesSanitizer.test_ac16_blacklisted_fields_never_injected` |
| 17 | `src/tests/test_chatter_service_injection.py` | `TestPrimaryAndExtrasMerge.test_ac17_unbound_with_extras_injects_extras_only` |
| 18 | `src/tests/test_chatter_service_injection.py` | `TestBoundNoExtrasRegression.test_ac18_bound_no_extras_only_primary` |
| 19 | `src/tests/test_chatter_service_injection.py` | `TestPrimaryAndExtrasMerge.test_ac19_unbound_no_extras_no_service_section` |
| 20 | `src/tests/test_ux_routing_views.py` | `TestRouterAlias.test_ac20_router_declares_projects_path` + `_keeps_ai_assistant_for_bookmarks` |
| 21 | `src/tests/test_ux_routing_views.py` | `TestSidebarEntry.test_ac21_sidebar_navitem_renamed_to_projects` |
| 22 | `src/tests/test_ux_routing_views.py` | `TestCreateProjectModal.test_ac22_modal_offers_no_service_option` |
| 23 | `src/tests/test_ux_routing_views.py` | `TestCreateProjectModal.test_ac23_modal_removes_has_project_filter` + `_no_longer_requires_service_selection` |
| 24 | `src/tests/test_ux_routing_views.py` | `TestProjectsListBadge.test_ac24_projects_page_shows_binding_state` |
| 25 | `src/tests/test_ux_routing_views.py` | `TestTaskFormLinkedServicesPanel.test_ac25_task_form_xml_declares_extras_panel` |

## Regression 守線 (explicit pre-green tests)

These tests target behavior that exists today and must remain after implementation. They may pass at red phase — their value is preventing regression once implementation refactors.

- **Project unique constraint** (`test_ac2_unique_cloud_service_constraint_preserved`) — locks `_sql_constraints['unique_cloud_service']`; if implementation drops the constraint per a misread of D1, this fails.
- **Bound project workspace_id compute chain** (`test_ac3_bound_project_workspace_id_unchanged`) — locks `cloud_service_id.workspace_id` related path; guards against replacement with a stored field that could break existing rows.
- **Bound project visible to workspace member** (`test_ac6_bound_project_visible_to_workspace_member`) — locks existing `project_rule_workspace_member` "bound" leg after the unbound short-circuit is removed.
- **Controller list endpoint contract** (`test_ac8_list_services_endpoint_unchanged_for_member`) — locks `/api/workspaces/<id>/services` shape for members after D8 ir.rule is added.
- **Controller outsider denial** (`test_ac8_list_services_denied_for_outsider`) — locks `workspace.check_user_access` rejection of non-members.
- **Bound + no extras AI prompt regression** (`test_ac18_bound_no_extras_only_primary`) — locks current `_get_cloud_service_context` happy path post-refactor.
- **Unbound + no extras empty** (`test_ac19_unbound_no_extras_no_service_section`) — locks current "empty for unbound" behavior; ensures the AC #17 fix doesn't accidentally inject empty section markers.

## Red 證明 (Odoo test runner output, captured 2026-05-17T00:59Z)

Sandbox: `dev-sandbox-feat-decouple-cs-projects` (docker-desktop, local hostPath; built by `woow-paas-sandbox #2`).

```
$ ./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-decouple-cs-projects \
    --module woow_paas_platform --keep-on-fail
...
red captured: 58 / 435
  - 24 FAIL (assertion)
  - 34 ERROR (exception)
  - 377 pass
```

Refactor-scope breakdown (the only failures that matter for this phase):

| File | Count | ACs |
|---|---|---|
| `test_task_service_extras` | 8 | AC #9-13 |
| `test_ux_routing_views` | 7 | AC #20-25 (1 regression — `_ac20_keeps_ai_assistant_for_bookmarks` — passes as expected post path-fix) |
| `test_chatter_service_injection` | 5 | AC #14-17 |
| `test_cloud_service_acl` | 4 | AC #7 |
| `test_project_decouple` | 1 | AC #4 (AC #5 surfaces as a downstream effect once AC #4 fix is in place) |
| **Refactor total** | **25** | |

Pre-existing infra failures (out of scope, *not* introduced by this PR): 32, mostly `UserError: Subscription journal is not configured` from the env's missing demo data. Confirmed by `woow-paas-sandbox #2` to be env-setup, not refactor-induced.

Note on path fix during red phase: initial run of `test_ux_routing_views.py` failed all 8 tests because the `_AssetReader` used `src/...` relative paths but `odoo.modules.get_module_path('woow_paas_platform')` already roots at the addon directory. Paths corrected to drop the `src/` prefix (commit-pending), re-run yields 7 FAIL + 1 PASS (the AC #20 regression now correctly passes). The 7 failures' `AssertionError` messages each surface the *real* contract that is missing, e.g.:

```
test_ac20_router_declares_projects_path
  AssertionError: Regex didn't match: 'path\s*:\s*[\'"]projects[\'"]'
  not found in '/** @odoo-module **/ ... { path: "ai-assistant", name: "AI Assistant" ... }'
```

```
test_ac25_task_form_xml_declares_extras_panel
  AssertionError: 'extra_cloud_service_ids' not found in
  '<?xml version="1.0" encoding="utf-8"?> ... <field name="chat_enabled"/> ...'
  : AC #25: task form must surface extra_cloud_service_ids (Linked Services panel)
```

Sample tracebacks captured by `woow-paas-sandbox #2` (full file at `/tmp/red-phase-fails-detail.log` inside the host):

**AC #15 helm-values sanitizer**

```
File: tests/test_chatter_service_injection.py:199
AssertionError: 'super-secret-pw-123' unexpectedly found in
  '...Helm Values):\n```json\n{"db": {"password": "super-secret-pw-123"}...'
  AC #15: raw password value leaked into AI prompt
```

**AC #7 cloud_service ACL**

```
test_ac7_bob_browse_w1_read_raises_access_error — AssertionError: AccessError
not raised. Current ir_rules.xml has no record rule on
woow_paas_platform.cloud_service; ACL CSV grants RWCU to base.group_user.
```

**AC #9-13 task extras**

```
test_ac9_extra_cloud_service_ids_field_exists — KeyError: 'extra_cloud_service_ids'
in Task._fields — field not declared yet.
test_ac10_primary_cloud_service_related_to_project — same family.
test_ac11_extras_field_has_workspace_scoped_domain — same family.
test_ac12_same_workspace_extra_accepted — same family.
test_ac13_extras_preserved_on_cross_project_move — AttributeError on
  task.extra_cloud_service_ids.
test_ac13_incompatible_extras_flag_exposed — AssertionError: none of
  (incompatible_extra_cloud_service_ids, has_incompatible_extras,
  extra_cloud_service_warning) exists on project.task.
```

**AC #4 unbound project ACL leak**

```
test_ac4_unbound_project_invisible_to_non_member — AssertionError: Bob can
see Alice's unbound project. ir_rules.xml line 55 short-circuits via
'|', ('cloud_service_id', '=', False), ... — the documented multi-tenant
leak.
```

Failure-mode map (predicted vs. observed):

| AC # | Predicted failure | Observed status |
|---|---|---|
| 1 | passes (baseline lock) | ✅ pass |
| 2 | passes (regression lock) | ✅ pass |
| 3 | passes (regression lock) | ✅ pass |
| 4 | `AssertionError: Bob can see Alice's unbound project` | ✅ red — exact match |
| 5 | passes post-AC #4 fix (regression of member-add path) | ⏳ green now (no rule = visible), needs re-check after AC #4 ir.rule tightening |
| 6 | passes (regression) | ✅ pass |
| 7 read/write/unlink/browse | `AccessError not raised` (no ir.rule) | ✅ red — 4 fails as predicted |
| 8 | passes (regression) | ✅ pass |
| 9-13 | `KeyError` / `AttributeError` on missing fields | ✅ red — 8 fails as predicted |
| 14 | `'Extra Svc' not in context` | ✅ red |
| 15 | `'***REDACTED***' not in context` + secret leaked | ✅ red |
| 16 | blacklisted value leaked | ✅ red |
| 17 | `context must not be empty for unbound+extras` | ✅ red |
| 18 | passes (regression) | ✅ pass |
| 19 | passes (regression) | ✅ pass |
| 20 (new path) | `projects` path missing | ✅ red |
| 20 (regression) | `ai-assistant` path present | ✅ pass (after path fix) |
| 21 | sidebar lacks `Projects` literal | ✅ red |
| 22 | modal lacks `No service` literal | ✅ red |
| 23 | `has_project` filter / required-service gate present | ✅ red (2 fails) |
| 24 | `Unbound` badge text missing | ✅ red |
| 25 | `extra_cloud_service_ids` not in `project_task_views.xml` | ✅ red |

Every predicted red has a concrete `AssertionError` / `KeyError` evidence line in `/tmp/red-phase-fails-detail.log`; no `ImportError` / `SyntaxError` / `fixture broken` in scope.

| AC # | Expected failure | Reason in current source |
|---|---|---|
| 1 | (likely passes; baseline lock) | `cloud_service_id` already nullable per `project_project.py:7-13` |
| 2 | (likely passes; regression lock) | `_sql_constraints` block already present `:27-33` |
| 3 | (likely passes; regression lock) | `_compute_workspace_id` related chain present |
| 4 | `AssertionError: Bob can see Alice's unbound project` | `ir_rules.xml:55` short-circuit `'|', ('cloud_service_id', '=', False), ...` |
| 5 | `AssertionError: Bob added as project member must be able to read P1` (after AC #4 fix breaks current short-circuit) | _passes today only because rule lets everyone see unbound_; post-rule-tighten will need member-aware rule |
| 6 | (passes today; regression) | bound leg of existing rule |
| 7 read | `AssertionError: Bob saw a W1 cloud_service` | no `ir.rule` on `woow_paas_platform.cloud_service` (`ir_rules.xml` lacks it) |
| 7 write/unlink/browse | `AssertionError: AccessError not raised` | same — no rule, ACL CSV grants RWCU to `base.group_user` |
| 8 | (passes today; regression) | controller gates via `check_user_access` |
| 9 | `KeyError: 'extra_cloud_service_ids'` on `Task._fields[...]` | `project_task.py` has no such field |
| 10 | `KeyError: 'primary_cloud_service_id'` | same |
| 11 | `KeyError: 'extra_cloud_service_ids'` | same |
| 12 | `KeyError: 'extra_cloud_service_ids'` on write | same |
| 13 | `AttributeError: 'project.task' object has no attribute 'extra_cloud_service_ids'` | same |
| 14 | `AssertionError: 'Extra Svc' not in context` | `_get_cloud_service_context` only injects single primary service |
| 15 | `AssertionError: '***REDACTED***' not in context` (helm dumped raw) | `discuss_channel.py:248` dumps `service.helm_values` JSON verbatim |
| 16 | `AssertionError: 'n8n-leak-aaaa' leaked` | sanitizer absent |
| 17 | `AssertionError: context must not be empty for unbound+extras` | early return `:225-226` on unbound returns `''` |
| 18 | (likely passes; regression) | current path matches |
| 19 | (passes today; regression) | current empty return |
| 20 | `AssertionError: router.js must declare 'projects' path` | `router.js` has only `ai-assistant` family |
| 21 | `AssertionError: Sidebar.js must declare 'Projects' nav item` | `Sidebar.js:11` declares `"AI Assistant"` |
| 22 | `AssertionError: CreateProjectModal must expose 'No service' option` | absent in current modal |
| 23 | `AssertionError: 'has_project' filter must be removed` | `CreateProjectModal.js:71-72` `!svc.has_project` filter present |
| 23b | `AssertionError: 'Please select a cloud service' gate must be removed` | `CreateProjectModal.js:131-132` |
| 24 | `AssertionError: 'unbound' missing from SupportProjectsPage` | grep confirms 0 hits in JS, 0 hits for "Unbound" |
| 25 | `AssertionError: 'extra_cloud_service_ids' missing from project_task_views.xml` | view doesn't reference the field |

Once the dev-sandbox env is ready, this section will be replaced with actual pytest output snippets (failure traces, last 5 lines per failed test) confirming the failure-mode column above.

## 刻意不測 (with reason)

| Out-of-scope | Reason |
|---|---|
| 1:N Service ↔ Project | D1=B keeps unique constraint; explicitly rejected in plan §Rejected options#1 |
| Task direct FK to Service | D2=A uses M2M extras instead; plan §Rejected options#2-3 |
| Cross-workspace extras candidates | D6=C limits to same workspace; testing the relaxed case belongs to the future PR that flips D6 |
| Project audit log | Out of scope per plan §Out of scope |
| Sidebar i18n / zh-tw entry | Open Q4 = keep English literal; no translation to verify |
| Sanitizer behavior on >5-level-nested helm_values | Reasonable depth limit per plan §刻意不測; deep recursion test is unit-test scope for implementation, not acceptance |
| AI reply *content quality* with sanitized prompt | Sanitizer in / sanitizer out is the contract; LLM response shape is out of scope |
| `/woow#/ai-assistant` ↔ `/woow#/projects` browser hash round-trip via real browser | playwright-cli E2E recommended (plan §E2E); deferred from red phase for runtime cost |
| Full Linked Services panel render in headed browser | Same — file-contract test verifies field-binding only |
| paas-operator behavior | Plan §Out of scope: pure Odoo module change |
| Frontend i18n strings (translation entries) | Open Q4 |
| `has_project` controller field removal | Plan keeps `has_project` for UI display flag; not testing removal |

## Fixture / Env 假設

- **Odoo test runner**: `--test-tags woow_paas_platform` (module-tagged); test classes will be picked up via existing `src/tests/__init__.py` registration.
- **Database state**: every test uses `TransactionCase` → automatic rollback; no persistent fixtures.
- **No external network**: no helm / k8s / paas-operator calls in any new test.
- **AI client**: not invoked — tests call `_get_cloud_service_context()` directly (the string-producing helper), not `_post_ai_reply` (which goes to AI provider).
- **Permissions**: tests create their own `res.users` with `base.group_user`; no reliance on demo users (no `--demo` flag required, and tests should work in `noupdate` mode).
- **Module path lookup** (test_ux_routing_views): uses `odoo.modules.get_module_path('woow_paas_platform')` so it works under both worktree and CI-image layouts.
- **Frontend file-content tests**: deliberately do not require a JS toolchain; they read raw source files.

## Open questions (for planner / leader)

- **AC #5 enforcement choice**: plan D3=A leaves `user_ids` vs. `message_partner_ids` open. `test_ac5` adds the user to *both* to stay portable; if implementation chooses only one, the test still passes. Confirm this dual-add is acceptable; otherwise tester will tighten.
- **AC #11 domain form**: plan does not specify whether the domain is static (`[('workspace_id', '=', project_id.workspace_id.id)]`) or dynamic (computed via onchange / `_compute_domain`). Test asserts only that `workspace_id` token appears in the domain repr; if implementation needs a stricter contract, file an addendum.
- **AC #13 warning-field naming**: test asserts one of three plausible names exists (`incompatible_extra_cloud_service_ids` / `has_incompatible_extras` / `extra_cloud_service_warning`). If implementation picks a different name, raise it back to tester before merging.
- **AC #15 redaction sentinel**: `***REDACTED***` is what plan §D9 specifies. Test asserts that exact string; if i18n-ized later, the test must adapt.

## Files added

- `src/tests/test_project_decouple.py` (AC #1-6)
- `src/tests/test_cloud_service_acl.py` (AC #7-8)
- `src/tests/test_task_service_extras.py` (AC #9-13)
- `src/tests/test_chatter_service_injection.py` (AC #14-19)
- `src/tests/test_ux_routing_views.py` (AC #20-25)
- `src/tests/__init__.py` (registered the 5 modules above)

No production code touched.
