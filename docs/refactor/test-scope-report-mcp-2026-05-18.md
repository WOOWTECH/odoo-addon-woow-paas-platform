# Test Scope Report — MCP `cloud_service_id` → `project_id` (red phase)

**Date**: 2026-05-18
**Author**: refactor-tester (TDD red phase)
**Plan**: `docs/refactor/plan-mcp-project-decouple.md`
**Decision (draft)**: `docs/refactor/decisions/mcp-project-decouple.md`
**Status**: signed-off (refactor-leader, 2026-05-18) → test-explainer
**Phase**: tester → test-explainer

## Leader rulings on Open Questions (2026-05-18 sign-off)

| Q | Ruling |
|---|--------|
| 1. AC #14 explicit controller test | **Skip** — accepted "same rebar as channel" argument; channel test covers the chain. |
| 2. AC #22 view-load smoke | **Skip** — Odoo module install/upgrade validates view XML, no duplicate test needed. |
| 3. AC #24 frontend OWL test | **Skip pytest automation** — D6 keeps frontend scope frozen; OWL change is only API param micro-edit. Manual smoke in dev sandbox during implementation. |
| 4. Migration `migrate(cr, version)` signature | Marked as implementation-phase hint. If implementer picks `pre-migrate` (no `version`), escalate back to tester for one-line patch; implementer does **not** edit test code directly. |
| 5. `active_test=False` design | Tester's call — test design domain. |
| 6. ACL workspace chain literal | Implementation decision. Tests are black-box isolation enforcement; implementer may walk a different chain as long as the regression tests pass. |

## Test header

- **5 test files**, **24 test cases** (all clean red):
  - `src/tests/test_mcp_project_decouple_migration.py` (10 cases — 6 schema-state + 4 behavior)
  - `src/tests/test_mcp_project_lifecycle.py` (4 cases)
  - `src/tests/test_mcp_acl_isolation.py` (4 cases)
  - `src/tests/test_mcp_chat_consumption.py` (3 cases)
  - `src/tests/test_mcp_integration.py` extension — `TestMcpProjectScopeConstraints` (3 cases)
- All cases fail with **`AssertionError`** — no `ImportError`, fixture break, or `ValueError("Invalid field")` noise.
- Counts: `24 post-tests in 0.34s` / `24 failed, 0 error(s)` (Odoo `--test-enable`).

Tests registered in `src/tests/__init__.py:53-56` so the Odoo test loader
discovers them at module load.

## AC → test mapping

Source AC numbering matches `docs/refactor/plan-mcp-project-decouple.md
§Acceptance criteria` (AC #1-#25).

### Schema & 資料 (AC #1, #2, #3, #4, #5)

| AC # | Spec line | Test | File:Line |
|------|-----------|------|-----------|
| 1 | `mcp_server.project_id` M2O cascade, indexed | `test_ac1_mcp_server_has_project_id_field` | `test_mcp_project_decouple_migration.py:79` |
| 1 | `mcp_server.cloud_service_id` removed | `test_ac1_mcp_server_no_cloud_service_id_field` | `test_mcp_project_decouple_migration.py:101` |
| 1 | `project_id` column physically present | `test_ac1_project_id_column_exists` | `test_mcp_project_decouple_migration.py:154` |
| 2 | `project.project.mcp_server_ids` inverse | `test_ac2_project_project_has_mcp_server_ids_inverse` | `test_mcp_project_decouple_migration.py:110` |
| 2 | `cloud_service.user_mcp_server_ids` removed | `test_ac2_cloud_service_no_user_mcp_server_ids` | `test_mcp_project_decouple_migration.py:129` |
| 3 | Migration backfills `project_id` | `test_ac3_migrate_backfill_user_scope_mcp` | `test_mcp_project_decouple_migration.py:271` |
| 3 / Resolved Q4 | Migration archives orphan rows (no unlink) | `test_ac3_migrate_orphan_user_scope_archive` | `test_mcp_project_decouple_migration.py:374` |
| 3 / D4 invariant | System-scope rows untouched | `test_ac3_migrate_system_scope_untouched` | `test_mcp_project_decouple_migration.py:412` |
| 4 | Migration idempotent | `test_ac4_migrate_idempotent` | `test_mcp_project_decouple_migration.py:312` |
| 5 | `cloud_service_id` column dropped | `test_ac5_cloud_service_id_column_dropped` | `test_mcp_project_decouple_migration.py:139` |

### Constraint (AC #6, #7)

D4 / D5 enforcement; covered via extension of existing `test_mcp_integration.py`
so the Constraint suite lives alongside other MCP-model tests.

| AC # | Spec line | Test | File:Line |
|------|-----------|------|-----------|
| 6 | D4: `scope='user' ⇔ project_id IS NOT NULL` (user direction) | `test_ac6_constraint_user_scope_requires_project` | `test_mcp_integration.py:867` |
| 6 | D4 (system direction): `scope='system' ⇔ project_id IS NULL` | `test_ac6_constraint_system_scope_excludes_project` | `test_mcp_integration.py:896` |
| 7 | D5: user MCP requires bound project (positive + negative) | `test_ac7_constraint_user_scope_requires_bound_project` | `test_mcp_integration.py:919` |

> AC #8 (`mcp_server.url` algorithm path changed to walk
> `self.project_id.cloud_service_id`) is **deliberately not testable** at
> the ORM/AC layer — it is a code-walk lint (grep `self.cloud_service_id`
> in `mcp_server.py` post-impl must return 0 hits, per
> `decisions/mcp-project-decouple.md` §Invariant 7). Reviewer-phase check.

### Lifecycle (AC #9, #10, #11, #12)

| AC # | Spec line | Test | File:Line |
|------|-----------|------|-----------|
| 9 / Invariant #4 | Cleanup hook order: unlink → uninstall → state='deleting' | `test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall` | `test_mcp_project_lifecycle.py:108` |
| 9 sanity | Cleanup hook does not touch system MCP | `test_ac9_cleanup_hook_does_not_touch_system_mcp` | `test_mcp_project_lifecycle.py:159` |
| 10 (folded into #9 test) | service.unlink keeps project | `test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall` (final assertion) | `test_mcp_project_lifecycle.py:147` |
| 11 / Invariant #1 | project.unlink cascades MCP | `test_ac11_delete_project_cascades_user_mcp` | `test_mcp_project_lifecycle.py:199` |
| 12 | Project A unlink leaves project B's MCP | `test_ac12_delete_project_does_not_cross_contaminate` | `test_mcp_project_lifecycle.py:224` |

### AI chat consumption (AC #13, #14, #15)

| AC # | Spec line | Test | File:Line |
|------|-----------|------|-----------|
| 13 | `discuss_channel._get_user_mcp_tools` walks via `task.project_id.mcp_server_ids` | `test_ac13_channel_gets_user_mcp_via_project` | `test_mcp_chat_consumption.py:101` |
| 13 (negative) | Unbound project → empty `mcp_tool` recordset (graceful) | `test_ac13_channel_no_mcp_for_unbound_project` | `test_mcp_chat_consumption.py:140` |
| 14 | `ai_assistant._get_user_mcp_tools_for_channel` covered indirectly via channel chain | (shared rebar with `_get_user_mcp_tools`; verified via AC #13) | — |
| 15 | `ai.assistant.mcp_server_ids` M2M domain unchanged (system MCP path) | `test_ac15_assistant_system_mcp_unchanged` | `test_mcp_chat_consumption.py:173` |

> **AC #14 caveat**: The controller method is structurally identical to
> `discuss_channel._get_user_mcp_tools` (same chain rebar). Plan §AC #14
> is implementer's contract — reviewer phase must grep
> `cloud_service_id.user_mcp_server_ids` in `controllers/ai_assistant.py`
> and ensure 0 hits remain.

### ACL — cross-workspace isolation (AC #17, #18, #19, #20)

The B1-style sub-model lock — every test maps to a regression guard.

| AC # | Spec line | Test | File:Line |
|------|-----------|------|-----------|
| 17 / Invariant #5 | `mcp_server_rule_workspace_member` denies cross-workspace user MCP | `test_alice_cannot_read_bob_user_mcp_via_orm` | `test_mcp_acl_isolation.py:153` |
| 18 / B1 sub-model | `mcp_tool_rule_workspace_member` denies cross-workspace tool | `test_alice_cannot_read_bob_user_mcp_tool` | `test_mcp_acl_isolation.py:191` |
| 19 | Both `search` + `browse().read()` paths denied | (`search` empty + `AccessError` asserted) within AC #17/#18 cases | — |
| 20 | Own-workspace user retains read | `test_bob_can_read_own_workspace_mcp` | `test_mcp_acl_isolation.py:229` |
| 17 (system leg) / Invariant #6 | scope='system' universally readable | `test_system_mcp_readable_by_all_users` | `test_mcp_acl_isolation.py:252` |

> AC #21 (`ir.model.access.csv` user row RWCU `1,0,0,0` → `1,1,1,1`) is
> **structural** — implementer-side change, not a behavior assertion at
> the ACL layer. Covered indirectly by AC #20 (own-workspace user must
> write/create/unlink within own ws once ACL row is opened up).

### Backend / Frontend view (AC #22, #23, #24, #25)

**Intentionally not in this red set** — see `刻意不測` below.

## Red proof (failing pytest output)

Test runner invocation:

```bash
docker compose run --rm --no-deps web odoo \
  -d woow_feature_project_mcp_skill \
  --test-enable \
  --test-tags "/woow_paas_platform:TestMcpProjectDecoupleMigrationSchema,\
/woow_paas_platform:TestMcpProjectDecoupleMigrationBehavior,\
/woow_paas_platform:TestMcpProjectLifecycle,\
/woow_paas_platform:TestMcpAclCrossWorkspaceIsolation,\
/woow_paas_platform:TestMcpChatConsumption,\
/woow_paas_platform:TestMcpProjectScopeConstraints" \
  --stop-after-init --log-level=test
```

Summary line (verbatim from Odoo log):

```
2026-05-18 01:22:37,305 1 INFO  ... odoo.service.server: 24 post-tests in 0.34s, 249 queries
2026-05-18 01:22:37,305 1 ERROR ... odoo.tests.result : 24 failed, 0 error(s) of 24 tests
```

### Sample failures — one per category (verbatim trace)

**Schema-state (AC #1) — direct AssertionError on field check**

```
FAIL: TestMcpProjectDecoupleMigrationSchema.test_ac1_mcp_server_has_project_id_field
  File "/mnt/extra-addons/woow_paas_platform/tests/test_mcp_project_decouple_migration.py", line 87
AssertionError: 'project_id' not found in {...mcp_server fields...} :
  AC #1: mcp_server.project_id field missing — refactor not yet implemented.
  Plan: drop cloud_service_id and add project_id M2O to project.project with ondelete='cascade'.
```

**Schema-state (AC #2 inverse) — direct AssertionError**

```
FAIL: TestMcpProjectDecoupleMigrationSchema.test_ac2_project_project_has_mcp_server_ids_inverse
  File "/mnt/extra-addons/woow_paas_platform/tests/test_mcp_project_decouple_migration.py", line 117
AssertionError: 'mcp_server_ids' not found in {...project fields...} :
  AC #2: project.project.mcp_server_ids inverse missing — AI chat consumption chain
  (channel→task→project→mcp_servers) depends on this inverse.
```

**Schema-state (AC #5) — direct AssertionError on raw SQL `information_schema`**

```
FAIL: TestMcpProjectDecoupleMigrationSchema.test_ac5_cloud_service_id_column_dropped
  File "/mnt/extra-addons/woow_paas_platform/tests/test_mcp_project_decouple_migration.py", line 150
AssertionError: (1,) is not None :
  AC #5: woow_paas_platform_mcp_server.cloud_service_id column must be dropped by migration 18.0.1.0.24.
  The column still exists — backfill SQL or DROP COLUMN SQL not yet implemented.
```

**Migration behavior (AC #3 backfill) — file-existence guard**

```
FAIL: TestMcpProjectDecoupleMigrationBehavior.test_ac3_migrate_backfill_user_scope_mcp
  File "...test_mcp_project_decouple_migration.py", line 218, in _assert_migration_implemented
    self.assertIsNotNone(self.migrate, ...)
AssertionError: Migration file src/migrations/18.0.1.0.24/post-migrate.py is missing.
  Implementation phase must create it with backfill SQL + orphan archive +
  DROP COLUMN + ir_model_data noupdate flip. See plan §Migration path.
```

**Lifecycle (AC #9 cleanup hook order) — field guard**

```
FAIL: TestMcpProjectLifecycle.test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall
  File "...test_mcp_project_lifecycle.py", line 83, in _assert_schema_ready
    self.fail(...)
AssertionError: mcp_server.project_id field missing — lifecycle tests cannot run pre-implementation. Plan §D1 / AC #1.
```

**ACL (AC #19 cross-workspace deny) — setUp field guard**

```
FAIL: TestMcpAclCrossWorkspaceIsolation.test_alice_cannot_read_bob_user_mcp_via_orm
  File "...test_mcp_acl_isolation.py", line 53, in setUp
AssertionError: mcp_server.project_id field missing — ACL tests rely on the new domain
  (project_id.cloud_service_id.workspace_id.access_ids...). Plan §D1 / AC #1.
```

**Constraint (AC #6) — setUp field guard**

```
FAIL: TestMcpProjectScopeConstraints.test_ac6_constraint_user_scope_requires_project
  File "...test_mcp_integration.py", line 823, in setUp
AssertionError: mcp_server.project_id field missing — constraint tests rely on the new schema
  (Plan AC #1). Implement schema before constraints.
```

**Chat consumption (AC #13) — setUp field guard**

```
FAIL: TestMcpChatConsumption.test_ac13_channel_gets_user_mcp_via_project
  File "...test_mcp_chat_consumption.py", line 68, in setUp
AssertionError: mcp_server.project_id field missing — chat-consumption tests rely on the new schema (AC #1).
```

### Full failing-test roster (24 / 24)

```
TestMcpAclCrossWorkspaceIsolation
  ├─ test_alice_cannot_read_bob_user_mcp_tool          AssertionError
  ├─ test_alice_cannot_read_bob_user_mcp_via_orm       AssertionError
  ├─ test_bob_can_read_own_workspace_mcp               AssertionError
  └─ test_system_mcp_readable_by_all_users             AssertionError

TestMcpChatConsumption
  ├─ test_ac13_channel_gets_user_mcp_via_project       AssertionError
  ├─ test_ac13_channel_no_mcp_for_unbound_project      AssertionError
  └─ test_ac15_assistant_system_mcp_unchanged          AssertionError

TestMcpProjectScopeConstraints (in test_mcp_integration.py)
  ├─ test_ac6_constraint_system_scope_excludes_project        AssertionError
  ├─ test_ac6_constraint_user_scope_requires_project          AssertionError
  └─ test_ac7_constraint_user_scope_requires_bound_project    AssertionError

TestMcpProjectDecoupleMigrationSchema
  ├─ test_ac1_mcp_server_has_project_id_field          AssertionError
  ├─ test_ac1_mcp_server_no_cloud_service_id_field     AssertionError
  ├─ test_ac1_project_id_column_exists                 AssertionError
  ├─ test_ac2_cloud_service_no_user_mcp_server_ids     AssertionError
  ├─ test_ac2_project_project_has_mcp_server_ids_inverse AssertionError
  └─ test_ac5_cloud_service_id_column_dropped          AssertionError

TestMcpProjectDecoupleMigrationBehavior
  ├─ test_ac3_migrate_backfill_user_scope_mcp          AssertionError
  ├─ test_ac3_migrate_orphan_user_scope_archive        AssertionError
  ├─ test_ac3_migrate_system_scope_untouched           AssertionError
  └─ test_ac4_migrate_idempotent                       AssertionError

TestMcpProjectLifecycle
  ├─ test_ac11_delete_project_cascades_user_mcp        AssertionError
  ├─ test_ac12_delete_project_does_not_cross_contaminate AssertionError
  ├─ test_ac9_cleanup_hook_does_not_touch_system_mcp   AssertionError
  └─ test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall AssertionError
```

All red. No `ImportError`. No `ValueError("Invalid field")`. No fixture
break (setUp ordering, missing DB rows). Every failure points implementer
at the exact AC / Invariant / decision-doc line to fix.

## 刻意不測 (intentional out-of-scope)

| 未測項目 | 理由 |
|---------|------|
| **AC #8** `_model_build_mcp_endpoint_url` path change (`self.project_id.cloud_service_id`) | URL算法 not changed — only the walk path. Code-walk lint (grep `self.cloud_service_id` in `mcp_server.py` returns 0 hits) is reviewer's check; can be tested but adds no behaviour assertion beyond AC #1/#7. |
| **AC #14** `controllers/ai_assistant.py._get_user_mcp_tools_for_channel` | Identical rebar to `discuss_channel._get_user_mcp_tools` (AC #13). Adding a controller-level test would need `request.env` mocking; verified instead by reviewer grep `cloud_service_id.user_mcp_server_ids` in controller — 0 hits. |
| **AC #16** SSE streaming end-to-end MCP tool exec | Out of unit-test scope — needs real n8n sidecar + langgraph runtime. Plan calls it "regression" but it's an integration-test class; would require helm-up sandbox. Left to manual smoke in staging. |
| **AC #21** `ir.model.access.csv` RWCU bit change `1,0,0,0` → `1,1,1,1` | Structural-config change with no behaviour assertion above what AC #20 (own-ws CRUD) covers. Reviewer-phase grep on CSV row. |
| **AC #22** `mcp_server_views.xml` form view field rename (`cloud_service_id` → `project_id`) | View XML asset — Odoo loads it at module install; failure surfaces as a deploy-time error, not a test failure. View regression covered by general `test_ux_routing_views.py` smoke when view loads. Listing as separate case would be over-test. |
| **AC #23** `mcp_server` action domain unchanged `[('scope', '=', 'system')]` | No behaviour change — regression covered by AC #15 (assistant system MCP path). |
| **AC #24** Frontend `McpServersTab.xml` query path change | OWL frontend — no Python test infra in the addon for frontend rendering yet. Manual smoke via dev sandbox once implementation lands. Scope mismatch with this refactor's tester role (Python / ORM / migration). |
| **AC #25** `/api/cloud-services/...` MCP-related endpoints | If endpoint exists, behaviour covered by AC #13/#14 path (controller walks the same chain). Listing here would be implementation-specific. |
| **Helm CLI mocking layer** (mock vs real `kubectl`/`helm`) | Plan AC list says no helm CLI mocking in this refactor's red set. Lifecycle test uses Python-level `unittest.mock.patch` on `get_paas_operator_client` — sufficient for ORM-state assertions and order regression. Real helm validation is staging-only (k8s-dev-sandbox). |
| **`scope='system'` + cross-user visibility regression beyond `test_system_mcp_readable_by_all_users`** | Pre-existing behaviour, not in this refactor's scope. Risk #3 covers the assumption that system MCP is platform-level and intentionally always-readable. |
| **`mcp_server.url` algorithm** | Not changed (AC #8 only changes the *walk path* to backing service, not the URL string assembly). No behaviour regression to lock. |

## Test fixture / dependency assumptions

- All tests `@tagged('post_install', '-at_install')` — run after the addon
  is loaded (consistent with `test_mcp_integration.py` and
  `test_cross_workspace_task_isolation.py` precedent).
- Two-workspace Alice/Bob fixture mirrors
  `test_cross_workspace_task_isolation.py` to keep B1 regression patterns
  visually consistent for reviewers.
- Migration tests use `importlib.util.spec_from_file_location` to load
  `src/migrations/18.0.1.0.24/post-migrate.py` dynamically (migration dirs
  use dot-separated names which are not valid Python identifiers).
- Lifecycle order test patches
  `odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client`
  (function-scoped import in `_do_cleanup`; patching the source-module
  attribute is sufficient).
- Raw-SQL fixture (`_seed_user_scope_row_via_sql`) introspects
  `information_schema.columns` so the same helper works on pre- or
  post-migration schema — avoiding fixture-broken failures during the
  green transition.

## Open questions (handed to planner / leader)

1. **AC #14 explicit controller test?** Currently relying on
   "identical-rebar-to-channel" argument. If reviewer phase wants
   defence-in-depth, an additional controller test using
   `MagicMock(env=...)` (pattern from `test_cloud_service_acl.py
   ::TestCloudServiceControllerRegression`) can be added in implementer
   phase. Flag if this is wanted.

2. **AC #22 view-load smoke test?** Plan calls it `test #19` in §Test
   scope 建議 (`mcp_server_form_view_loads`). I omitted it — XML view
   load failure surfaces at `-u woow_paas_platform` time, not as a unit
   test red signal. If desired, the implementer should add a simple
   `view = self.env.ref('woow_paas_platform.view_mcp_server_form'); self.assertTrue(view)`
   to `test_ux_routing_views.py`. Currently not blocking.

3. **AC #24 frontend OWL test?** Plan calls it `test #20`
   (`service_page_mcp_tab_renders`). Out of scope for this Python tester
   role. If implementer wants front-end smoke, that is a separate
   frontend-tester role / hurl-test layer.

4. **Migration `migrate(cr, version)` signature**: My tests call
   `self.migrate(self.cr, '18.0.1.0.23')` mirroring the `18.0.1.0.23`
   migration's signature (`def migrate(cr, version): ...`). If the
   implementer chooses a different invocation surface (e.g.
   `pre-migrate` instead of `post-migrate`, or a class-based hook),
   tests need a one-line patch to the loader. Flag if signature deviates.

5. **`McpServer.with_context(active_test=False)`** in lifecycle / ACL
   tests — used because `active=False` rows are filtered by default
   `active_test` context. Tests rely on this for orphan-archive
   verification and ondelete cascade existence checks. If implementer
   chooses to keep cascade with `active=True` (no archive logic), the
   `active_test=False` is harmless but flag the design choice for
   consistency.

6. **`workspace_id` chain accuracy**: ACL domain in plan AC #17:
   `('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)`.
   Verified `project.project.workspace_id` is `compute='_compute_workspace_id'`
   with `store=True` (`models/project_project.py:14-25`) so this stored
   field is searchable. The walk works.

## Hand-off

Hand back to **refactor-leader**. Suggested next phase: **test-explainer**
(consume this report + the 5 test files → produce human-readable
walk-through for the implementer). After test-explainer, implementer
phase can target zero-by-zero green transitions.

**No code changes to `src/models/`, `src/security/`, `src/controllers/`,
`src/migrations/` made.** All edits limited to `src/tests/` +
`src/tests/__init__.py` registration.
