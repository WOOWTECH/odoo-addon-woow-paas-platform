# REV-01 revocation hold, FAULT-02 recovery and fence hardening

Status: **candidate component PASS (native); whole REV-01, FAULT-02, RACE-06
stay unproven until their oracles run on the released chain.**

This note records why the Access safety candidate (PR #2042) was extended before
it could be released, what the extension does, and what it deliberately does not
claim.

## Findings that blocked a release

1. **REV-01 closed the origin and never re-opened it.** Removing a policy set
   `access_token_revoke_pending` and `access_publish_pending`; nothing cleared
   the first one. The route stayed parked (`http_status:503`) after deleting even
   one of several policies, and the delete API kept answering "retry".
2. **FAULT-02 treated an epoch change as an Operator restart.** Production runs
   two Operator replicas behind one Service and each process generates its own
   epoch, so consecutive health checks alternate. The probe would have parked
   every protected route within a few cron cycles, and only the
   `access_protection_lost` rows had an automatic recovery path. A health payload
   without the epoch (an Operator deployed after Odoo) also raised and closed
   every tenant.
3. **The probe cron never ran on upgraded installs.** Its call had been appended
   to an existing `ir.cron` record inside a `noupdate="1"` file; upgrades do not
   rewrite `code` of existing records.
4. **The RACE-06 dispatch fence could fence a route for good.**
   - A `resolved` identity was reused as `adopt_only` with the dead app id when a
     route re-created its app (protection turned off, then on): the PUT of a
     deleted app failed every time.
   - The effect-start marker was never cleared, so any failed create (even a
     provider 4xx such as an unavailable login method) left the route fenced with
     no administrator exit: review adopts only exactly one match, the single-retry
     authorization refused anything with a marker, and abandon needs both the
     name and the hostname to change.
5. **Pre-existing candidate test debt** that turns the post-merge `odoo-tests.yml`
   red (PR CI does not run Odoo tests): the pinned sha256 of the Operator's
   `cloudflare.py`, the payload key-set tests (missing `revoke_tokens`), and four
   fixtures that predate the strict publication receipts / live-connector
   recovery.

## REV-01: bounded hold, then re-confirm and re-publish

1. Policy removal marks the anchor (`access_token_revoke_pending`,
   `access_publish_pending`, `access_token_revoke_accepted_at` cleared) and parks
   the origin before any provider call.
2. The Access sync carries `revoke_tokens=true` until the provider acknowledges
   it (`token_revocation == "accepted"`). The acknowledgement time starts the
   hold; later syncs do not revoke again, so a hold never re-logs users out.
3. The hold is `woow_paas_platform.access_revoke_hold_seconds`
   (default 120, clamped to 30-3600; an unset or unparsable value is the default
   and never shortens it). The resync cron is woken just after the hold would end;
   the 15 minute run remains the safety net.
4. When the hold elapsed, the next sync re-confirms the Access app/policies, the
   normal release path re-publishes the origin (still requiring a live connector
   for Security Access), and **only then** the revocation markers are cleared. A
   failed re-publication keeps them, so the cron retries.
5. A tenant edit during the hold cannot release the origin: the batch release
   paths in the route/service `write()` skip anchors with an open revocation.
6. A second policy removal starts a new revocation generation and restarts the
   hold.
7. If the revoke-driven sync itself fails (provider error, invalid receipt), the
   markers are kept and the sync returns without raising. Raising would roll the
   markers back while the edge stays parked, leaving a closed origin with no
   record for the cron.
8. If the Access app is gone (protection turned off or lost) there is nothing to
   revoke: the origin is re-published and the markers cleared.
9. A refused revocation (`token_revocation == "failed"`) and an Operator that does
   not report the field at all are logged at ERROR with the reason; the origin
   stays closed (fail-closed) and the revocation is retried.

The provider acknowledging a revocation is **not** proof that an old cookie is
denied, and the platform has no cookie to probe with. The hold is a bound. The
live oracle (same browser, same cookie, timed observations through removal,
hold, re-publication and final denial) must still observe the denial; if the old
cookie reaches the origin after re-publication, raise the hold.

Scope: only removing a whole policy triggers a revocation. Edits of rules,
login methods and session duration do not (PR #1707 covers them with an
authorization fingerprint; porting it is the planned follow-up).

Cost: the affected route answers 503 for roughly the hold plus one cron wake-up
(about 2-3 minutes with defaults) after every policy removal, and users sign in
again once.

## FAULT-02: instance-aware epoch, mark-only probe, automatic recovery

- `/health` also returns `instance_id` (the pod name). Odoo remembers
  `{instance_id: epoch}` per Security Access (bounded to 8, stored in the
  system-only field; the refresh endpoint reads and writes it with `sudo`) and
  calls it a restart only when the **same** instance reports a **new** epoch. A
  replica seen for the first time is learned. An Operator without the epoch
  fields gives no restart evidence and is not an error; the connector rules still
  apply.
- The Operator Deployment runs one uvicorn process per pod, so one pod never
  alternates epochs.
- The connector-down / malformed-inventory rules close the routes (pending).
- The probe is its own cron record (`ir_cron_access_fault_probe`, 15 minutes). It
  **only marks**: parking and recovery belong to the resync leg, which parks
  first, re-confirms Access and re-publishes in a transaction that keeps the
  marker. A probe that parked in the same transaction as its flag would strand a
  parked edge if that transaction were rolled back. An unreachable Operator is
  not treated as a fault by the probe (nothing is exposed; Access is enforced at
  Cloudflare); a user-initiated refresh still fails closed.
- The probe takes one batch per run and continues after the last id it handled,
  wrapping at the end, so every Security Access is probed.
- A *new* closure wakes the resync cron about two minutes later; a connector
  that stays down does not create a loop.
- Recovery is the existing exact Access sync: the Security Access resync leg also
  scans `access_publish_pending` rows (not those inside a revocation hold).
  Release still requires a fresh status read with a live connector, so recovery
  never skips the Access confirmation. Smart Home and Cloud Service keep their
  manual retry semantics for plain pending.
- Resync budget: a sync that spent operator calls but did not release (a hold, no
  live connector, a fence awaiting review) now counts against the leg's budget
  (`pending=` in the run log); only the liveness gate means "no call was made".

## RACE-06: the fence must not turn one failure into an outage

- A `resolved` fence starts a new cycle when the route needs a new app (callers
  only create when no app id is stored). If the old app still existed the provider
  refuses the duplicate domain (409), which stays uncertain and goes to review.
- A provider refusal that proves no app exists (HTTP 400, 401, 403, 404, 405, 415,
  422, 429 from the Operator) clears the effect-start marker; timeouts, transport
  failures, 5xx and 409 keep it (adopt-only, reviewed).
- An administrator may authorize exactly one more create for an uncertain send
  with the exact confirmation text, but only after a settle window of 15 minutes
  from the send; the authorization clears the marker.

## Deployment order

Operator first, then Odoo. An old Operator ignores `revoke_tokens` and never
reports `token_revocation`: Odoo then keeps the origin closed and logs
`did not report token_revocation` at ERROR on every attempt. It does not
invalidate anything on the missing epoch fields.

## Operations

- Shorten or extend the hold: set the system parameter above; it is read on every
  sync.
- Stop the fault probe without a deploy: deactivate the *Probe Access faults* cron.
- Rows stuck in a revocation hold can be inspected with
  `access_token_revoke_pending = true` (and `access_token_revoke_accepted_at`).

## Tests

`test_access_revoke_hold_rev01` (decision functions; the three anchor kinds
through the AC1011 fake operator: close, hold, no re-revoke, re-confirm before
re-publication, failed release keeps markers, tenant edit cannot release,
restart of the hold, unacknowledged and refused revocations, teardown during the
hold, provider failure at removal, cron scan shape, SA recovery through the cron,
SH/CS plain pending left alone), `test_fault02_durable_invalidation` (per-instance
epoch rules including two alternating replicas, old Operator, workspace admin
without the system group, mark-only probe, rotation, cron wiring),
`test_race06_fence_lifecycle` (refused create is retryable, uncertain create stays
fenced, re-create after teardown) and the Operator health contract tests.

## Still to prove on the released chain

REV-01: normal-product policy removal with the same original cookie, timed
observations, durable pending, blocked origin, terminal denial, re-publication.
FAULT-02: connector outage, Operator container restart, Pod rescheduling, each
with fail-closed behaviour and recovery that re-confirms Access. RACE-06: the
all-crash oracle against the real provider.
