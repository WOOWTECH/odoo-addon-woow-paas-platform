---
title: Configure direct-route workspace role guard
category: plans
status: active
created: 2026-10-11
related:
  - docs/guides/testing/TESTING.md
  - docs/decisions/patterns/capability-gate-single-source-field.md
---

# Configure Workspace Role Guard Implementation Plan

**Goal:** Show the service creation form only after the current workspace endpoint confirms the caller is owner or admin, including direct hash URLs and workspace changes.

**Architecture:** Keep the backend authorization unchanged. AppConfigurationPage uses the existing workspaceService.getWorkspace JSON-RPC adapter. It clears previous authorization and form state before each route load, reads the server role, and ignores superseded or unmounted requests. Its template and launch handler share a fail-closed getter; the workspace list cache is never an authority.

**Tech Stack:** Odoo 18 standalone OWL, existing JSON-RPC services, Node's built-in test runner.

The installed writing-plans skill guides this work. The repository retires superpowers, so its retired execution plugin is not invoked.

## Scope and starting evidence

- 2026-10-11 first drafted on base `0574330a` (audit of production `18.0.1.0.224`); 2026-10-16 rebased onto develop `1acf80f0` (release 18.0.1.0.243), branch `fix/role-ux-gate-direct-routes`. Reproduced unchanged on production .243 by the commercial acceptance run (X-W-009).
- Production audit found user, guest, and nonmember direct routes displayed an enabled creation form. No unauthorized service was created: existing backend role checks rejected those roles.
- Change only configure-page UI logic and its regression tests. Do not change backend ACL, manifest version, branding, or another service flow.

## Task 1: Establish meaningful red coverage

**Files:** `tests/ui/test_configure_workspace_role_guard.mjs`; existing router, Root, JSON-RPC adapters, configure page, and XML are loaded as real source.

1. Follow the existing `tests/ui/*.mjs` source-import rewrite convention.
2. Feed direct hash URLs through the actual router and Root props.
3. Keep workspaceService, cloudService, and rpc real; mock the HTTP boundary with explicit role fixtures and valid template/plan IDs.
4. Evaluate the outer branches from the actual page XML. Cover owner/admin visible form and valid create payload; member/user, guest, nonmember, missing/unknown roles hidden form and zero create requests.
5. Cover pending and failed workspace reads, stale local cache, workspace/template route changes, late responses, unmount, and duplicate launch.
6. Run the new suite against unchanged production UI source and retain its failures as the red baseline.

## Task 2: Implement the narrow guard

**Files:** `src/static/src/paas/pages/configure/AppConfigurationPage.js`, `AppConfigurationPage.xml`.

1. Read `workspaceService.getWorkspace(workspaceId)` before template/plans.
2. Clear all previous workspace authorization and form inputs immediately when IDs change.
3. Register prop-change and unmount hooks; capture IDs and use a request generation to ignore stale responses.
4. Fail closed for denied, malformed, missing-role, or failed workspace responses.
5. Gate the complete form and launch handler on the same current-route role result; retain owner/admin creation behavior and backend errors.
6. Use current workspace response data for breadcrumb naming, without reading the workspace cache for authorization.

## Task 3: Verify and prepare review artifacts

1. Run the new suite and existing `node --test tests/ui/*.mjs tests/frontend/*.mjs`.
2. Run XML well-formed checks and repository lint; preserve unrelated existing baseline failures separately.
3. Stage the plan for the documentation index generator, regenerate index, and run its `--check`. Check changed Markdown's local links when lychee is available.
4. Inspect `git diff --check`, exact diff, manifest equality with base, and original checkout status.
5. Save the review diff and local test evidence outside the checkout in the audit evidence directory. No commit, push, PR, sandbox/prod deployment.

## Verification boundary

The local suite integrates real routing, component lifecycle methods, XML branch conditions, service adapters, and JSON-RPC transport with OWL lifecycle/UI component stubs and mock HTTP. It does not mount the Odoo assets or run a headed browser in the k3s Odoo sandbox. That repository UX gate remains pending; passing local checks does not establish a production fix or commercial acceptance.
