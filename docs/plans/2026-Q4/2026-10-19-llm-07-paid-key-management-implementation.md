---
title: LLM-07 paid API key management
category: plans
status: in-progress
created: 2026-10-19
related:
  - ../../../tickets/LLM-07-paid-key-management.md
  - ../../../DESIGN.md
  - ../../../DECISIONS.md
---

# LLM-07 Paid API Key Management Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. This repository has retired the `superpowers` plugin; do not invoke it.

**Goal:** Let an active AI Models account create, list, edit, extend, enable, disable, and delete paid and free-trial API-key mirrors without ever persisting or re-displaying a full `sk-` value.

**Architecture:** Keep new-api as the token authority and `woow_paas_platform.llm_api_key` as a guarded PaaS mirror. Reuse the existing one-time creation transaction, signed user client, SWR queue, and `llm_event` throttle/audit mechanisms; add a narrow key-management service layer that validates ownership locally before every external call and commits mirror changes only after definite remote results. The standalone OWL UI receives only masked-key DTOs, while a component-local reveal modal owns the one-time plaintext.

**Tech Stack:** Odoo 18 Python models/controllers, PostgreSQL independent cursors, signed new-api proxy client, standalone OWL/JavaScript/XML/SCSS, Node built-in test runner, k3s dev-sandbox Odoo tests.

---

## Preconditions and non-goals

- Base this work on the latest `origin/develop` containing #1839 (LLM-04), #1847 (LLM-02), #1848 (LLM-06), and #1849 (LLM-10). At drafting time that is `9650d6fd`.
- LLM-08's N25 password reauthentication currently exists only in the local LLM-08 integration branch, not in develop. Do **not** copy a second implementation into LLM-07. Tasks 2–3 may proceed independently; before Task 4 or any endpoint that relies on reauth, rebase this branch only after the LLM-08 reauth change is merged (or after an explicitly approved, reviewed stack base is supplied). Verify that `llm_account._llm_require_reauth()` and `/api/llm/account/reauth` exist first.
- Do not push, open a PR, alter stg/prod, import terms, or reveal/use a real new-api key while executing this plan. All backend tests use `FakeNewApi`.
- LLM-11 owns actual expiry/create notification delivery. This ticket only adds the durable expiry-notice dedupe fields and UI “expiring soon” state.
- LLM-09, LLM-12a, and LLM-15 own usage charts, hardening/invariants, and later cross-feature work. Do not pull their scope into this branch.

## Contract checklist

- D4: manual prepay plus per-key **total** limit, expiry, and model allowlist; no monthly reset budget.
- D9: default 90 days, one-click +90-day extension, optional never expiry.
- D10: terms consent is the only activation gate; at most 20 non-deleted/non-lost keys per account.
- Terms gate applies to paid-key creation and enabling only, after active-account validation and before throttle reservation or external I/O. Update, extend, disable, and delete remain usable when terms need reacceptance.
- A paid key sends `group: "default"`, `allow_user_quota_query: false`, and an allowlist that is a subset of the LLM-06 cached `default` models.
- A limit is always a **total** limit: create sends `limit_points * QUOTA_PER_POINT`; update reads actual `used_quota` then sends `limit_points * QUOTA_PER_POINT - used_quota`; a lower value is rejected with `llm_key_limit_below_used`.
- Full plaintext only traverses the successful create response into `LlmKeyRevealModal` local state. Logs, DB, events, notifications, stores, and error responses use no full `sk-` value.

## Task 1: Verify the stacked prerequisite before coding

**Files:** No production file changes. Inspect `src/models/llm_account.py`, `src/controllers/llm.py`, `src/models/llm_common.py`, `src/tests/test_llm_reauth.py` after LLM-08 lands.

1. Confirm the branch base contains `account._llm_require_reauth(session, ...)`, `ERR_LLM_REAUTH_REQUIRED`, the reauth controller route, and their registered tests.
2. Run the focused existing reauth suite in the sandbox.

   Run: `... k8s-dev-sandbox test <slug> --module woow_paas_platform --test-tags llm_reauth`

   Expected: the N25 session grant, 10-minute expiry, account/user binding, and rate-limit audit pass before a key creation path consumes them.

3. Record the exact merged LLM-08 commit in this plan's progress section. If it is unavailable, stop before feature edits rather than inserting a duplicate reauth protocol.
4. Commit nothing for this checkpoint.

## Task 2: Add the key-management domain contract and red backend tests

**Files:**

- Modify: `src/models/llm_common.py`
- Modify: `src/models/llm_api_key.py`
- Create: `src/tests/test_llm_api_key_management.py`
- Modify: `src/tests/__init__.py`
- Modify: `scripts/audit/baselines/test-collection.txt`

1. Write `TestLlmApiKeyManagement` using `_ConfiguredCase`, `LlmClientAdapter`, and `FakeNewApi`. Its helper should provision one active account with the fake client and create paid/free mirror fixtures only through internal service calls.
2. Add failing tests for the public mirror contract:

   ```python
   self.assertEqual(key._llm_status(), 'exhausted')
   self.assertEqual(key._llm_dto()['limit_points'], 100)
   self.assertEqual(key._llm_dto()['used_points'], 100)
   self.assertEqual(key._llm_dto()['remain_points'], 0)
   ```

   Cover expired, exhausted, `free_daily_cap`, suspend, overdraft, stale `creating`, deleted/lost exclusion, `expiry_notice_7d_at`, and `expiry_notice_1d_at` fields.
3. Add failing tests for the 20-key boundary, including disabled and expired keys counting until deletion, while deleted/lost keys do not count.
4. Add any closed error constants and safe error parameters required by DESIGN §3.4: `llm_key_not_found`, `llm_key_limit_below_used`, `llm_key_cannot_enable`, and `llm_reauth_required` if it arrives with LLM-08. Register closed result/detail values in the existing event/error registries rather than recording free text.
5. Implement the smallest model changes:

   ```python
   expiry_notice_7d_at = fields.Datetime(readonly=True, copy=False)
   expiry_notice_1d_at = fields.Datetime(readonly=True, copy=False)

   def _llm_status(self):
       if self._llm_is_stale_creating():
           return 'failed_reveal'
       if self.state in ('active', 'disabled') and self.expires_at and self.expires_at < _utcnow():
           return 'expired'
       if self.quota_limit_points is not None and self.used_quota_cached is not None \
               and self.used_quota_cached >= self.quota_limit_points * QUOTA_PER_POINT:
           return 'exhausted'
       return self.state
   ```

   Preserve the specific disabled reason in the DTO so UI can distinguish free-cap, suspend, and overdraft without trusting a new-api status value.
6. Make `_llm_dto()` derive integer point values only from integer quotas; never use float division, and retain `None` for unknown cache data.
7. Register the new test module and update the collection baseline only after the test is imported.
8. Run the focused backend class and confirm the pre-change source failed first, then passes.
9. Commit: `feat(llm): add paid key mirror status contract`.

## Task 3: Make the signed client and fake new-api express the exact token contract

**Files:**

- Modify: `src/services/newapi_client.py`
- Modify: `src/tests/llm_fake_newapi.py`
- Modify: `tests/test_llm_services.py`
- Modify: `src/tests/test_llm_api_key_management.py`

1. Add failing pure-client tests for a paid create body. Test a limited key, an unlimited key, model-limited and all-default variants, expiry timestamp, and invalid inputs.

   ```python
   self.assertEqual(body, {
       'name': 'wp_42_abcd',
       'expired_time': expires,
       'remain_quota': 100 * QUOTA_PER_POINT,
       'unlimited_quota': False,
       'model_limits_enabled': True,
       'model_limits': 'model-a,model-b',
       'allow_ips': '',
       'group': 'default',
       'cross_group_retry': False,
       'allow_user_quota_query': False,
   })
   ```

2. Add a narrowly validated `paid_token_body(...)` helper beside `free_trial_token_body(...)`. It must reject invalid token name, expiry, quota, bool, model string, or group rather than passing arbitrary payloads through the proxy.
3. Add a pure helper for a full token PUT body, populated from the actual safe `GET /api/token/:id` result plus only the allowed changed fields. Require and preserve `id`, `name`, `allow_ips`, group, quota flags, model settings, expiry, and `cross_group_retry`; reject missing/foreign/untrusted response shapes.
4. Extend `FakeNewApi` with `user_update_token`, `user_set_token_status`, and `user_delete_token`. Each must authenticate the UP, reject a token from another user, preserve the fake token’s fields on update, and record the call name. Keep fake reveal behavior and its 120-second boundary unchanged.
5. Add failure injection coverage for definite rejection, `NotExecuted`, and `Unknown` management writes. The service must not optimistically mutate a mirror after an unknown result.
6. Run the focused pure service and fake-backed suites.
7. Commit: `feat(llm): model paid token payload and management transport`.

## Task 4: Implement secure paid-key creation with one-time reveal

**Files:**

- Modify: `src/models/llm_api_key.py`
- Modify: `src/models/llm_account.py` only if an existing account helper needs a narrow reusable read; do not reimplement reauth
- Modify: `src/tests/test_llm_api_key_management.py`

1. Write failing tests for:

   - valid 90-day limited and never/unlimited paid creation;
   - `client_request_id` replay returning `llm_key_already_shown` with zero additional fake calls;
   - key count 21 returning `llm_key_limit` before any external call;
   - default-group allowlist success and free/unknown model rejection;
   - stale `creating` becoming `failed_reveal`;
   - terms reacceptance blocking paid create before throttle/event/external I/O;
   - terms with `requires_reaccept=False` not blocking;
   - no reauth for ordinary create, and `llm_reauth_required` before throttle/external I/O for `expires='never'` plus `limit_points=None`.

2. Extract the shared creation transaction into a private helper that takes a fully validated mirror specification and a payload factory. Do not let a UI-provided display name enter `newapi_name`, logs, events, or error details.
3. Validate request fields before reserving throttle:

   ```python
   spec = self._llm_validate_paid_create(
       account, name=name, limit_points=limit_points, expires=expires, models=models,
   )
   account._llm_require_current_terms()
   if spec['expires_at'] is None and spec['limit_points'] is None:
       account._llm_require_reauth(session)
   event_id = Event._llm_throttle_reserve(account, 'key_create', uid=user.id, real_ip=real_ip)
   ```

4. Insert the `creating` mirror in an independent cursor before POST. Include `key_label`, limit, expiry, and comma-joined allowlist in that insert, and keep the SQL column allowlist closed.
5. Reuse the existing 20-second create deadline, bounded POST → list-by-name → reveal sequence, retry-only-after-successful-empty-list rule, and `failed_reveal` state. The response DTO includes `key` only in this success path.
6. Ensure every exception is swallowed by the controller’s create boundary exactly as the existing free-trial route does; do not permit `retrying()` to replay a potentially completed create.
7. Expand tests to grep event payloads, fake call logs, and mirror fields for a generated `sk-` pattern; assert no plaintext is stored.
8. Run the focused class, then commit: `feat(llm): create paid API keys with one-time reveal`.

## Task 5: Implement ownership-safe update, extend, status, and delete operations

**Files:**

- Modify: `src/models/llm_api_key.py`
- Modify: `src/models/llm_event.py` only if existing safe event helpers need a closed action/result entry
- Modify: `src/tests/test_llm_api_key_management.py`

1. Write failing tests first for A/B account isolation: B invoking every key operation with A’s PaaS key ID returns `llm_key_not_found` and makes zero fake external calls.
2. Add a private local lookup that anchors `key_id` to the caller’s active account and rejects deleted/lost/creating records before obtaining UP or a client.
3. Write the update test sequence:

   ```python
   result = service._llm_update_key(partner, key.id, limit_points=100)
   self.assertEqual(fake.tokens[key.newapi_token_id]['remain_quota'], 70 * QUOTA_PER_POINT)
   self.assertEqual(result['remain_after_points'], 70)
   ```

   Set fake `used_quota` to 30 points first. Also assert a requested 20-point total returns `llm_key_limit_below_used` with `used=30`, changes neither remote nor mirror, and preserves all unrelated token fields such as `allow_ips`.
4. Implement update as GET token → validate token id/name/owner shape → construct complete PUT → wait for definite success → write the local mirror. Treat an unknown write as unavailable with no local optimistic change. Do not use the words or fields “remaining limit” as public input semantics.
5. Add extension tests. Paid keys get `max(now, remote_expiry) + 90 days`; free-trial keys return `llm_key_invalid` without I/O. An expired paid key may extend, and an expiry notice timestamp is cleared when the date moves.
6. Add status tests. Disabling does not need terms; enabling checks current local status for expired/exhausted/free-capped/suspended/overdraft and calls `_llm_require_current_terms()` before the first external call. A successful remote status PUT updates `state` and `disabled_by` atomically through the guarded internal path.
7. Add delete tests. Verify typed `confirm_name` matches `key_label` exactly, remove the remote token, then mark the mirror deleted. A failed-reveal key is deletable and freeing it permits a replacement; an expired free-trial key no longer reserves its one-key slot after deletion.
8. Emit only closed-code `key_update`, `key_status`, and `key_delete` events with target model/id, uid, and real IP. Never emit `key_label`, upstream error text, token bodies, or plaintext.
9. Run the focused backend test module and commit: `feat(llm): manage paid and free trial API keys`.

## Task 6: Reconcile key mirrors through SWR without synchronous list I/O

**Files:**

- Modify: `src/models/llm_swr.py`
- Modify: `src/models/llm_api_key.py`
- Modify: `src/tests/test_llm_api_key_management.py`

1. Write failing SWR tests using a fake token list that includes active/disabled tokens, used quotas, last access timestamps, and a mirror token missing from the remote list.
2. Extend `_llm_store_token_usage()` so it only accepts an item when both remote `id` and `name` match one of the account’s mirrors. Update cached use fields, mirror active/disabled status where it is safe, and mark a definite absent remote token deleted only after a successful full list.
3. Do not let a list failure or malformed item change a mirror state. Keep pagination bounded for the 20-key account limit and keep the 30-second `KIND_TOKENS` TTL.
4. Ensure `/api/llm/keys/list` continues to return DB/SWR data only and merely queues a refresh when stale. Add an assertion that list calls make zero synchronous fake new-api calls.
5. Add DTO tests for total-limit progress numbers and `data_as_of` propagation.
6. Run focused SWR/key tests and commit: `feat(llm): reconcile API key mirrors through SWR`.

## Task 7: Wire controller endpoints and error-safe HTTP responses

**Files:**

- Modify: `src/controllers/llm.py`
- Modify: `src/models/llm_common.py`
- Modify: `src/tests/test_llm_api_key_management.py`
- Modify: `src/tests/test_llm_platform_foundation.py` only for shared error-registry assertions

1. Write `HttpCase` tests for `/api/llm/keys/create` paid payload, update, extend, set_status, delete, and list. Authenticate two portal users for the cross-account cases.
2. Extend `_SAFE_PARAMS` only with closed, low-risk values needed by the UI: `reason` and `used`. Never pass confirmation names, model lists, upstream body/errors, or exception strings.
3. Add routes that anchor all partner/user/session data server-side:

   ```python
   @route('/api/llm/keys/set_status', auth='user', methods=['POST'], type='json')
   def llm_keys_set_status(self, key_id=None, enabled=None, **kw):
       return self._run('keys/set_status', lambda partner: request.env[KEY_MODEL]
                        ._llm_set_key_status(partner, key_id, enabled, request.env.user,
                                             _peer_ip(), request.session))
   ```

4. Keep `keys/create` on `swallow_operational=True`; use the normal non-replay behavior for update/extend/status/delete. All handlers must continue to convert accidental exceptions to `llm_internal_error` without serializing tracebacks.
5. Verify terms gate timing with call counters: outdated terms for paid create/enabling return `llm_tos_outdated`, create no throttle rows, and make zero fake calls; update/disable/delete/extend remain available.
6. Run the HTTP tests and commit: `feat(llm): expose paid key management endpoints`.

## Task 8: Add the Key Management tab and a safe paid-key form

**Files:**

- Create: `src/static/src/paas/pages/llm/tabs/LlmKeysTab.js`
- Create: `src/static/src/paas/pages/llm/tabs/LlmKeysTab.xml`
- Create: `src/static/src/paas/pages/llm/tabs/LlmKeysTab.scss`
- Create: `src/static/src/paas/components/llm/LlmKeyFormModal.js`
- Create: `src/static/src/paas/components/llm/LlmKeyFormModal.xml`
- Create: `src/static/src/paas/components/llm/LlmKeyFormModal.scss`
- Modify: `src/static/src/paas/pages/llm/LlmPage.js`
- Modify: `src/static/src/paas/pages/llm/LlmPage.xml`
- Modify: `src/static/src/paas/core/llm_routes.js`
- Modify: `src/static/src/paas/services/llm_service.js`
- Modify: `src/static/src/paas/services/llm_errors.js`

1. Add failing Node tests in a new `tests/ui/test_llm_keys.mjs` that load the real source through the repository’s existing import-stub convention.
2. Make `keys` a live tab only after its component is wired in both `LlmPage.components` and `LlmPage.xml`. Keep the hash URL as the sole tab source of truth.
3. Add service methods that use the existing `call()` sanitizer and preserve a create `client_request_id` for the lifetime of an open form:

   ```javascript
   async createPaidKey(clientRequestId, values) {
     return call('/api/llm/keys/create', {
       client_request_id: clientRequestId,
       kind: 'paid',
       name: values.name,
       limit_points: values.limitPoints,
       expires: values.expires,
       models: values.models,
     }, { timeoutMs: KEY_CREATE_TIMEOUT_MS, transportCode: LLM_KEY_CREATE_INTERRUPTED });
   }
   ```

4. Build `LlmKeyFormModal` as a local form state component. Defaults are 90 days, unlimited total limit, and all paid models. Its selector is populated only from `llmService.models.items` with `group === 'default'`; a free model can never be posted from client UI.
5. Name the control “Total limit” and, while editing a limited key, show a calculated sentence such as “Used 30 points; 70 points remain after this change.” Reject values below used locally for usability, but leave server validation authoritative.
6. Choosing never expiry plus unlimited must show the existing reauth flow from LLM-08; clear password input immediately after submission and keep the reauth grant only in the server session.
7. Add desktop table and mobile card layouts. Show masked key, type, status, used/total progress, last use, expiry, creation time, expiring-soon marker, and a per-row action menu. Never include a full key in props, store, console, aria labels, DOM data attributes, or URL parameters.
8. Read `?extend=<id>` defensively, accept only an integer key that is in the current list, open the 90-day confirmation once, and remove/ignore malformed query state without an external call.
9. Add modal focus, Escape/backdrop confirmation, and full-screen mobile sheet behavior by following existing modal utilities rather than inventing a new focus trap.
10. Run the new UI source tests and commit: `feat(llm): add API key management tab and form`.

## Task 9: Generalize the reveal modal without widening plaintext scope

**Files:**

- Modify: `src/static/src/paas/components/llm/LlmKeyRevealModal.js`
- Modify: `src/static/src/paas/components/llm/LlmKeyRevealModal.xml`
- Modify: `src/static/src/paas/components/llm/LlmKeyRevealModal.scss`
- Modify: `src/static/src/paas/pages/llm/tabs/LlmOverviewTab.js`
- Modify: `src/static/src/paas/pages/llm/tabs/LlmOverviewTab.xml`
- Modify: `tests/ui/test_llm_overview.mjs`
- Modify: `tests/ui/test_llm_keys.mjs`

1. Write failing tests that assert paid and free flows both leave `llmService.keys` free of `key`, close/unmount clears `state.key`, and errors never expose the transport text.
2. Change the modal API so the parent supplies a creation function and safe display metadata (`kind`, allowed models, base URL); the modal alone creates the request ID and receives the one-time plaintext result.
3. Retain the 40-second creation timeout, browser/history leave guards, explicit “I have saved this key somewhere safe” acknowledgment, copy failure feedback, and no automatic clipboard write.
4. Keep `buildEnvVarsText()`’s Base URL requirement. The paid variant should not advertise the free Qwen model; display either the selected allowlist or “All paid models” without putting user-controlled display names into an unsafe context.
5. Replace the temporary free-trial “contact support to reset it” wording with the DESIGN §6.3.2 recovery route: create a new key and disable or delete the old key. Update the overview’s expired/free trial hints to navigate to Key Management.
6. Run modal, overview, and keys UI tests. Commit: `feat(llm): reuse one-time key reveal for paid keys`.

## Task 10: Finish client state, translations, and regression coverage

**Files:**

- Modify: `src/static/src/paas/services/llm_service.js`
- Modify: `src/static/src/paas/services/llm_errors.js`
- Modify: `src/i18n/zh_TW.po`
- Modify: `tests/ui/test_llm_errors.mjs`
- Modify: `tests/ui/test_llm_nav.mjs`
- Modify: `tests/ui/test_llm_i18n.mjs`
- Modify: `scripts/audit/baselines/test-collection.txt` if the final registered test class set changed

1. Add service methods for update, extend, status, and delete. Each refreshes the masked key list/state after success; none caches a creation response or full key.
2. Add error-map tests for every new server code and enable reason. Ensure the UI says “total limit” rather than “remaining quota,” and “永不到期” rather than “永不過期” in zh-TW.
3. Update translations using the repo’s existing extraction/update workflow. Preserve code/data values and do not translate model IDs, error codes, or JSON field names.
4. Add regression tests for stale responses, double-clicked action buttons, session expiry, terms reaccept redirect/overview prompt, and no request when a disabled UI action is invoked.
5. Run all `tests/ui/test_llm_*.mjs` and the focused backend modules. Commit: `test(llm): cover paid key UI and error contracts`.

## Task 11: Verify the whole branch before review

**Files:** No intended feature changes except generated collection/docs index artifacts required by the checks.

1. Run `git diff --check` and inspect every changed file for unrelated changes, full key patterns, accidental logging, direct `sudo` in controller code, and manifest version drift.
2. Run the project’s pure tests that cover this work:

   ```bash
   node --test tests/ui/test_llm_errors.mjs tests/ui/test_llm_nav.mjs \
     tests/ui/test_llm_overview.mjs tests/ui/test_llm_keys.mjs
   python3 -m py_compile src/models/llm_api_key.py src/models/llm_swr.py \
     src/controllers/llm.py src/services/newapi_client.py
   ```

3. Run the Odoo module tests in a dedicated dev sandbox. Use the project plugin command rather than a local Odoo invocation, and point every cluster command at `--context woow-k3s`.
4. Update `scripts/audit/baselines/test-collection.txt` only after all new registered Odoo classes exist; run the C2 collection audit and the repository lint/timeout ladder checks.
5. Regenerate `docs/index.md` after this plan document is added, then run `python3 scripts/audit/gen-docs-index.py --check` and local Markdown link checks where available.
6. Do not deploy the shared `aimodels` station. After a future merge and explicit demo authorization, provide the owner a separate test matrix for AC1–AC10, including real key deletion/recreation, limits, expiry, reauth, terms gate, and log/DB redaction checks.
7. Commit any solely mechanical baseline/index changes separately. Do not push or open a PR without explicit user authorization.

## Progress

- [x] 2026-10-19: audited latest develop (#1839/#1847/#1848/#1849), existing free-key foundation, UI skeleton, fake client, and LLM-08 reauth dependency.
- [x] 2026-10-19: created isolated local worktree `feat/llm-07-paid-key-management-1019` from `9650d6fd`.
- [ ] LLM-08 reauth is merged/rebased into this branch base.
- [ ] Tasks 2–11 complete with recorded verification evidence.
