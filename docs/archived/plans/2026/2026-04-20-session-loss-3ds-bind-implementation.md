---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-20-session-loss-3ds-bind-implementation.md
archived: 2026-07-19
archived-reason: "已落地；3DS 綁卡流程現況=src/controllers/billing.py"
---

# Phase 2c #1 Implementation Plan: session loss after 3DS bind

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop the ECPG bind-result bouncer from overwriting the user's authenticated `session_id` cookie after 3DS verification. One-line decorator change with TDD + staging E2E verification.

**Architecture:** Add `save_session=False` to `@route` decorator on `api_payment_method_bind_result`. This disables Odoo's `_save_session()` logic that otherwise mints and Set-Cookies an anonymous `session_id` onto the bouncer's response (see design doc for full root cause). Matches Odoo upstream pattern in `payment_mollie`/`payment_aps` for cross-site POST returns.

**Tech Stack:** Odoo 18 `@http.route(save_session=False)`; `HttpCase` with `requests.post`; ECPay 3DS sandbox for E2E.

**Design reference:** `docs/plans/2026-04-20-session-loss-3ds-bind-design.md`

---

## Pre-flight

**Step 0.1: Confirm branch**

Run: `git branch --show-current`
Expected: `develop` (or a feature branch cut from develop — confirm with user if worktree isolation is desired).

**Step 0.2: Confirm worktree status**

Run: `git status --short`
Expected: clean tree or only the design-doc commit from this session (`095a91a`).

**Step 0.3: Confirm upstream assumption**

The fix relies on `_handle_bind_callback` running with `.sudo()` regardless of incoming session. Quick sanity check:

Run: `grep -n "def _handle_bind_callback" $(find /Users/eugene/Documents/woow/AREA-odoo -name ecpg_callback.py 2>/dev/null | head -1) 2>/dev/null || echo "run inside docker"`

If local not found, verify inside docker in Task 4 (test-runner surfaces any sudo/auth issue).

---

## Task 1: Write the failing HttpCase test

**Files:**
- Modify: `src/tests/test_payment_methods_api.py` (append to `TestPaymentMethodsApi` class, after `test_delete_archives_token`)

**Step 1.1: Add test method**

Append this method to the `TestPaymentMethodsApi` class (around line 195, after `test_delete_archives_token`):

```python
    def test_bouncer_does_not_overwrite_session_cookie(self):
        """Bouncer at /api/billing/payment-methods/bind-result/<trade_no> must
        NOT set a new session_id cookie on its response.

        Why: ECPay POSTs cross-site to this URL after 3DS. SameSite=Lax strips
        the user's session_id from the request, so Odoo's default behavior is
        to mint a new anonymous session and Set-Cookie it back — overwriting
        the user's authenticated session_id in their browser. We use
        save_session=False on the route to prevent this. Upstream pattern:
        payment_mollie.mollie_return_from_checkout / payment_aps.aps_return_from_checkout.
        """
        import requests
        url = (
            f"{self.base_url()}"
            "/api/billing/payment-methods/bind-result/TEST_NO_SESSION"
        )
        with patch(
            'odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback.'
            'ECPGCallback._handle_bind_callback',
            return_value=None,
        ):
            # Fresh requests.post() (not self.url_open) to isolate from
            # self.opener's authenticated cookies — simulates ECPay's
            # cookieless cross-site POST.
            response = requests.post(
                url,
                data={'RtnCode': '1', 'RtnMsg': 'OK'},
                allow_redirects=False,
                timeout=10,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            'session_id', response.cookies,
            f"Bouncer must not Set-Cookie session_id "
            f"(got: {dict(response.cookies)})",
        )
        # Sanity: bouncer still serves the HTML+JS redirect page
        self.assertIn('window.location.replace', response.text)
```

**Step 1.2: Sanity-check imports**

The test uses `patch` (already imported at top of file, line 2) and `requests` (imported inside the test to keep the top-level imports minimal and match Odoo test conventions). No top-level import edits needed.

---

## Task 2: Run the new test; verify it FAILS (red)

**Step 2.1: Start dev Docker env (if not running)**

Run: `./scripts/start-dev.sh`
Expected: container healthy, DB available. Takes ~30s.

**Step 2.2: Run only the new test, expecting failure**

Run inside docker:

```bash
docker compose exec -T odoo /bin/bash -c "\
  odoo -c /etc/odoo/odoo.conf \
    --test-enable \
    --test-tags '/woow_paas_platform:TestPaymentMethodsApi.test_bouncer_does_not_overwrite_session_cookie' \
    --stop-after-init \
    -d \$POSTGRES_DB \
    -u woow_paas_platform 2>&1 | tail -60"
```

Expected: test FAILS with one of:
- `AssertionError: ... Bouncer must not Set-Cookie session_id (got: {'session_id': '<sid>'})`
- OR (less likely) red due to unrelated setUp issue — investigate before proceeding.

**If it passes unexpectedly**: this means either (a) the bug is already fixed, (b) the test isn't actually hitting the bouncer, or (c) the opener does send cookies. Read the output carefully and fix the test before moving on — don't skip to Task 3.

**Step 2.3: Do NOT commit yet**

Keep the failing test unstaged. Move to Task 3.

---

## Task 3: Apply the one-line fix + docstring

**Files:**
- Modify: `src/controllers/billing.py:253-299`

**Step 3.1: Edit the route decorator**

Locate the decorator on `api_payment_method_bind_result` (currently lines 253-259). Add `save_session=False` as the last kwarg. After edit:

```python
    @route(
        "/api/billing/payment-methods/bind-result/<string:trade_no>",
        auth="public",
        type="http",
        methods=["GET", "POST"],
        csrf=False,
        save_session=False,
    )
    def api_payment_method_bind_result(self, trade_no, **kw):
```

**Step 3.2: Extend the docstring**

Edit the existing docstring (lines 261-278). Keep points (1) and (2) as-is, add point (3) explaining `save_session=False`. After edit the docstring should end with:

```python
        2) Don't 303 to `/woow`. The browser's POST to this endpoint is cross-site,
           so SameSite=Lax cookies are stripped. A server 303 chains the cross-site
           context into the GET, so cookies still aren't sent and `/woow` (auth=user)
           bounces to /web/login. Instead, return a tiny same-origin HTML page that
           uses a JS top-level navigation — that GET is treated as same-origin so
           Lax cookies are sent and the SPA loads with the user's session intact.

        3) `save_session=False`. Even with the JS redirect above, Odoo still mints
           an anonymous session for this `auth='public'` endpoint (because the
           incoming POST carries no session_id cookie) and its `_save_session`
           post-dispatch hook sets `Set-Cookie: session_id=<anon_sid>` on the
           response — which overwrites the user's authenticated cookie in the
           browser. `save_session=False` short-circuits `_save_session`, keeping
           the user's session_id intact. Upstream Odoo uses the same flag on
           `payment_mollie.mollie_return_from_checkout` and
           `payment_aps.aps_return_from_checkout` for the identical reason.
        """
```

**Step 3.3: Do NOT commit yet** — need green test first.

---

## Task 4: Verify test passes (green) + run full suite

**Step 4.1: Re-run the new test**

Same command as Step 2.2:

```bash
docker compose exec -T odoo /bin/bash -c "\
  odoo -c /etc/odoo/odoo.conf \
    --test-enable \
    --test-tags '/woow_paas_platform:TestPaymentMethodsApi.test_bouncer_does_not_overwrite_session_cookie' \
    --stop-after-init \
    -d \$POSTGRES_DB \
    -u woow_paas_platform 2>&1 | tail -60"
```

Expected: the specific test PASSES. Look for `1 passed` or `OK` in the summary.

**Step 4.2: Run the full `woow_paas_platform` test suite**

```bash
docker compose exec -T odoo /bin/bash -c "\
  odoo -c /etc/odoo/odoo.conf \
    --test-enable \
    --test-tags woow_paas_platform \
    --stop-after-init \
    -d \$POSTGRES_DB \
    -u woow_paas_platform 2>&1 | tail -100"
```

Expected:
- `TestSanitizePhone*`, `TestAttemptAutoTopup`, `TestLowBalance`, the new `test_bouncer_does_not_overwrite_session_cookie`, and any other non-pre-existing tests: **PASS**
- The 3 pre-existing failures from Phase 2c #2 (`test_create_bind_returns_three_d_url`, `test_delete_archives_token`, `test_list_cards_returns_payment_tokens`): **still fail** with `body['success']=False`. This is expected per the E2E test results doc and is tracked separately as Phase 2c #2.
- No OTHER new failures.

**If any other test regresses**: stop, investigate. Do not proceed to commit.

---

## Task 5: Commit

**Step 5.1: Stage changes**

```bash
git add src/controllers/billing.py src/tests/test_payment_methods_api.py
git status --short
```

Expected output:
```
M  src/controllers/billing.py
M  src/tests/test_payment_methods_api.py
```

**Step 5.2: Commit**

```bash
git commit -m "$(cat <<'EOF'
fix(ecpg): preserve user session on 3DS bind bouncer

Add save_session=False to the OrderResultURL bouncer so Odoo's
_save_session post-dispatch hook does not Set-Cookie an anonymous
session_id onto the response. ECPay's cross-site POST arrives without
the user's SameSite=Lax cookie, so Odoo mints an anon session; without
this flag the response overwrites the authenticated session_id in the
user's browser and the subsequent /woow navigation redirects to login.

Mirrors payment_mollie.mollie_return_from_checkout and
payment_aps.aps_return_from_checkout from Odoo upstream.

Covered by TestPaymentMethodsApi.test_bouncer_does_not_overwrite_session_cookie
(HttpCase asserts no Set-Cookie: session_id on bouncer response).
EOF
)"
```

**Step 5.3: Confirm clean status**

Run: `git log --oneline -3`
Expected: the fix commit at HEAD.

---

## Task 6: Deploy to staging

**Step 6.1: Update `__manifest__.py` version**

Read current version in `src/__manifest__.py`:

```bash
grep '"version"' src/__manifest__.py
```

Bump the **patch** number (format `{odoo_version}.{major}.{minor}.{patch}`, e.g. `18.0.1.0.5` → `18.0.1.0.6`). Edit the file:

```python
"version": "18.0.1.0.6",  # adjust to actual next patch
```

**Step 6.2: Commit + tag**

```bash
git add src/__manifest__.py
git commit -m "release: 18.0.1.0.6"  # use actual version
git push
git tag v18.0.1.0.6                    # use actual version
git push origin v18.0.1.0.6            # triggers paas-odoo-ci
```

**Step 6.3: Deploy staging**

Invoke: `/woow:deploy-stg`

The skill will:
- Create a `stg-*` git tag to trigger paas-odoo-ci image build
- Poll CI status until complete (15min timeout)
- Deploy to `dev-sandbox-stg` namespace

Expected: skill returns with staging URL `https://stg.woowtech.io/woow` ready.

---

## Task 7: Staging E2E verification

**Step 7.1: Fresh login**

1. Open `https://stg.woowtech.io/woow` in a private/incognito browser window.
2. Log in as admin.
3. Open DevTools → Application → Cookies → `stg.woowtech.io`.
4. **Note the current `session_id` value** (copy it somewhere).

**Step 7.2: Bind a card**

1. Navigate to `#/settings/payment-methods`.
2. Click "Add payment method".
3. Fill the ECPay form with sandbox test card:
   - Card: `4311-9522-2222-2222`
   - Expiry: `12/28`
   - CVV: `222`
4. Submit → 3DS page appears.
5. Enter OTP `1234` → submit.

**Step 7.3: Verify success criteria**

Tick each one. Fail = Task 7 fails.

- [ ] Browser lands on `#/settings/payment-methods?bind_status=ok&trade_no=BIND...`
- [ ] **No** redirect to `/web/login`
- [ ] **No** need to re-login
- [ ] DevTools → Application → Cookies → `session_id` **has the same value** as Step 7.1 (the authenticated session was preserved)
- [ ] DevTools → Network → the bouncer request (POST to `/api/billing/payment-methods/bind-result/...`): response headers contain **no** `Set-Cookie: session_id=...`
- [ ] The new card appears in the payment methods list
- [ ] Delete flow still works (click delete → card gone)
- [ ] (Regression) Auto-topup TC-2b-1 still works: trigger the cron, transaction completes, wallet credited

**If any box fails**: collect the relevant network trace / screenshot and report back; do NOT proceed to Task 8.

---

## Task 8: Update test results doc

**Files:**
- Modify: `docs/plans/2026-04-19-e2e-test-results.md`

**Step 8.1: Edit row #1 in "Phase 2c Follow-ups" table**

Locate the table row for #1 (around line 37) and update it from:

```
| 1 | Session loss after 3DS bind redirect | ECPay's cross-site POST to bouncer is anonymous → Odoo issues new anon `session_id` cookie that overwrites the user's authenticated one. User has to re-login after bind. Possible fix: bypass session middleware on bouncer, or different binding flow (popup window). |
```

To:

```
| 1 | ~~Session loss after 3DS bind redirect~~ ✅ **FIXED** | Added `save_session=False` to bouncer route decorator (commit `<hash>`), mirroring `payment_mollie`/`payment_aps` upstream pattern. Verified on staging `<image tag>`: admin binds sandbox card 4311-9522-2222-2222, completes 3DS OTP 1234, lands back on `#/settings/payment-methods?bind_status=ok` with session_id cookie unchanged — no login bounce. Covered by `TestPaymentMethodsApi.test_bouncer_does_not_overwrite_session_cookie`. |
```

Replace `<hash>` with the Task 5 commit short-SHA and `<image tag>` with the staging image tag (e.g. `paas-odoo-stg:20260420.HHMM`).

**Step 8.2: Commit the doc update**

```bash
git add docs/plans/2026-04-19-e2e-test-results.md
git commit -m "docs: mark Phase 2c #1 (session loss) verified on staging"
git push
```

---

## Definition of Done

- [x] Design doc committed (`095a91a`)
- [ ] Failing test added and verified red
- [ ] One-line fix + docstring extended
- [ ] New test passes green
- [ ] Full `woow_paas_platform` suite shows no new regressions
- [ ] Fix committed with descriptive message
- [ ] Staged version tagged, image built, deployed to `dev-sandbox-stg`
- [ ] Manual E2E checklist (Task 7) all boxes ticked
- [ ] `docs/plans/2026-04-19-e2e-test-results.md` row #1 marked fixed with commit + image tag

## Out of scope (do not drift)

- 3 pre-existing `TestPaymentMethodsApi` HttpCase fails → Phase 2c #2
- Popup UX refactor → future PR if desired
- `/payment/ecpg/bind_callback` (upstream addon, S2S only) → no change needed
- Any other Phase 2c items (#3, #5, #6) → separate sessions per e2e-test-results.md pickup prompts
