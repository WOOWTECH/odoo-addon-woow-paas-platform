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
   deletion is rolled back as before (the policy stays, nothing is revoked) - but
   the origin was parked *before* the failing call and a parked edge cannot roll
   back. The policy `unlink` therefore keeps a durable closure marker
   (`access_publish_pending` only, after the rollback) and wakes the resync cron,
   which re-confirms Access and re-opens the origin. Callers that catch the error
   (the controllers) commit the marker.
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
- A *new* closure, and a pending route whose connector inventory is valid again,
  wake the resync cron *now* (the trigger becomes visible only after the probe
  commits, so it cannot start before the marker is durable); a connector that
  stays down does not create a loop. A short connector blip therefore costs the
  time until the next probe tick (15 minutes at most) plus about a minute.
- A resync run stops starting new rows after 80 s (the scan is one transaction; a
  run killed by the worker time limit would roll back parks it had already done)
  and continues 10 s later only when it made progress.
- Recovery is the existing exact Access sync: every resync leg (Security Access,
  Smart Home, Cloud Service) also scans `access_publish_pending` rows, but not
  those inside a revocation hold. A sync that parks before its provider call can
  leave a parked edge behind a rolled-back transaction on any of the three, so
  each needs the automatic exit. Release still requires a fresh status read with
  a live connector (Security Access) or the halted-state checks (Smart Home,
  Cloud Service), so recovery never skips the Access confirmation. Rows left
  pending by earlier failed publications are retried too, which is intended.
- Resync budget: a sync that spent operator calls but did not release (a hold, no
  live connector, a fence awaiting review) now counts against the leg's budget
  (`pending=` in the run log); only the liveness gate means "no call was made".

## RACE-06: unknown create outcomes converge on their own

Cloudflare has no idempotency key for the Access app POST, which is why an unknown
outcome (worker killed around the provider call) used to leave a route fenced until an
administrator reviewed it.  It does keep **exactly one Access app per domain**
(measured on the live account: a repeated POST is refused with HTTP 409 / error 11010
`access.api.error.application_already_exists`; 8 simultaneous POSTs gave one 201 and
seven 409; a second app with a different name on the same domain is refused too, the
same name on another domain is allowed).  That uniqueness is the missing primitive:

- **Create-or-adopt** (Security Access, Smart Home and Cloud Service alike): a create
  that the provider refuses with 409 triggers a read-only lookup of the app that
  matches our exact name **and** domain; exactly one match is adopted and updated in
  place, anything else (a foreign app holding the domain, several matches) raises the
  original error. Updates and teardowns are never adopted.
- **Smart Home fence**: an unknown outcome (effect-start marker set, result lost) is
  left alone for a settle window (`woow_paas_platform.access_create_settle_seconds`,
  default 600 s, clamped 30-86400, so the Operator's tail of an in-flight call can
  finish), then repeated automatically; the repeat creates the app or is told it
  exists and adopts it.  Before the marker nothing was sent, which is recoverable at
  once. The administrator actions remain available.
- A provider refusal that proves no app of ours exists (HTTP 400, 401, 403, 404, 405,
  409 after a failed adoption, 415, 422, 429) clears the marker; timeouts, transport
  failures and 5xx keep it until the window passes.
- A `resolved` identity starts a new cycle when the route needs a new app.

Not claimed: the reusable policies of an attempt whose app landed but whose id was lost
stay behind as unattached reusable policies (no authorization effect; the reaper's
naming makes them findable). Only the effective authorization is single.

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
hold, provider failure at removal keeps the closure and re-opens through the cron,
cron scan shape, recovery of plain pending through the cron on every leg), `test_fault02_durable_invalidation` (per-instance
epoch rules including two alternating replicas, old Operator, workspace admin
without the system group, mark-only probe, rotation, cron wiring),
`test_race06_fence_lifecycle` (refused create is retryable, uncertain create waits for
the settle window then converges, foreign domain refused, re-create after teardown),
`test_race06_create_or_adopt` (the adopt helper; crash after the provider applied the
create and crash before reaching it, retried, for Security Access, Cloud Service and
Smart Home: always one app) and the Operator health contract tests.

## Still to prove on the released chain

REV-01: normal-product policy removal with the same original cookie, timed
observations, durable pending, blocked origin, terminal denial, re-publication.
FAULT-02: connector outage, Operator container restart, Pod rescheduling, each
with fail-closed behaviour and recovery that re-confirms Access. RACE-06: the
all-crash oracle on the released chain.
