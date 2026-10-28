# Access authorization-change revocation (the trigger scope of #1707 on the REV-01 hold)

Status: **native PASS (see "Evidence"); not released.** Whole REV-01 stays on the
released-chain oracle: this note changes *when* a revocation starts, not how the
hold, the re-confirmation or the re-publication work (all of that is #2042).

## Why

Removing a policy was the only edit that revoked the already-issued Cloudflare
Access tokens.  Editing an allow list, a require / exclude rule, a policy
decision or precedence, the login methods or the session duration left every
token valid until it expired (default 24 h, up to 7 days): a user removed from
the allow list kept reaching the service.  PR #1707 closed that gap with its own
35-second design; the owner decided to keep #2042's bounded hold (REV-01) and to
port only #1707's *trigger scope* onto it.

## What starts a revocation

A persisted edit starts the same revocation generation as a policy removal
(origin closed, scoped token revocation, bounded hold, re-confirmed
re-publication) when it **changes the app's effective authorization semantics
and is not a provable widening**:

| Edit | Site |
| --- | --- |
| a policy is created | `security_access_access_policy.create` |
| decision, precedence, inline `rule_ids`, anchor move of a policy | `security_access_access_policy.write` |
| a rule is created, edited (group / type / value / policy) or deleted | `security_access_access_policy_rule.create / write / unlink` |
| login methods, session duration, protection toggled while an app exists | `security_access_route.write`, `smart_home_route.write`, `cloud_service.write` |
| policy reorder (precedence) | the three reorder controllers: their writes are sync-suppressed, so they decide the revocation from the order before and after |
| a policy is removed | unchanged (#2042): always revokes |

The decision compares a fingerprint taken before and after the write
(`access_state.access_authorization_fingerprint`): whether the app is enforced,
the hostnames, session duration, login methods and the set of policies
(`decision`, `precedence`, the include / require / exclude sets).  The
fingerprint is computed from the sync payload the anchor already builds
(`_build_access_payload`), so the three anchor kinds (Security Access route,
Smart Home route, Cloud Service) cannot drift apart.

**Never revokes:**

- cosmetic edits: policy names, remote ids, the order / case / spacing /
  duplicates inside a selector, the order of rules inside a group, an edit that
  writes the same value;
- **provable widenings** (`access_authorization_widens_only`), because no
  already-issued token can have lost access: a new *Allow* policy, more
  addresses (or another include rule) in an *Allow* policy's include group, one
  more login method, a longer session duration.  This is one-sided and does not
  depend on how the provider orders its policies (an Allow policy only adds
  principals under both a union and a first-match evaluation).  Anything it
  cannot prove revokes: a new block / bypass policy, a removed or replaced
  address, a changed decision or precedence, a require or exclude rule added or
  removed, a shorter or unknown session duration, a login method removed, an
  empty ("every provider") login-method list that became a list.
- an anchor that is not enforced after the write, or has no Access app yet
  (nothing to revoke).

## Behaviour that follows

- **Narrowing and unprovable edits close the service for at least the hold**
  (default 120 s, `woow_paas_platform.access_revoke_hold_seconds`, 30-3600) and
  sign everyone out once.  The common edit - adding someone - does not.
- **Inline rules revoke once.**  A policy created or written with inline
  `rule_ids` lets the parent own the single final sync (`sa_rule_replace`, the
  private sentinel); a per-rule sync would revoke the same app once per rule and
  restart an already accepted hold.
- **A failed provider call cannot leave the edge closed.**  The revoke-driven
  sync parks the origin before the provider call; when the block fails the edit
  and the revocation mark roll back, and every marked anchor gets a durable
  closure marker (`access_revoke_savepoint`, the same rule as the policy-removal
  wrapper) so the resync cron re-confirms Access and re-opens it.
- **An edit during a hold** that narrows the semantics starts a new generation
  (the earlier acknowledgement is forgotten, a second revocation is requested,
  the hold restarts); a cosmetic or widening edit leaves the hold alone.  The
  previous test that expected exactly one revocation after a session-duration
  edit during a hold was written for the old trigger scope and now expects two.
- **Responses.**  The mutation result carries `saved: true`; while a revocation
  is held it says what is going on instead of "retry" (retrying cannot shorten a
  hold).  The development branch's UI does not yet read `access_configuration_ready`
  or `saved` (that is #1707's front end); until it does, the user sees the
  message as an error.
- **Break-glass switch.**  `woow_paas_platform.access_revoke_on_authorization_change`
  turns the *edit* triggers off only for an explicit `0`, `false`, `off` or `no`;
  unset, empty or any other text keeps them on.  Policy removal ignores it.

## Rollout

No Operator change beyond #2042 (`revoke_tokens` request field, `token_revocation`
receipt field, instance epoch).  No migration, no new column.  The Operator image
that carries #2042 must already be running before this Odoo build (an older
Operator never reports `token_revocation`: the platform keeps the origin closed
and logs an ERROR, as for policy removal).

## Evidence

Native Odoo 18 / PostgreSQL 15 (isolated containers on cluster1), see the batch66
report: the new `test_access_authorization_change_revoke` classes (pure
fingerprint / widening / switch / response tests and 21 scenarios for each of the
three anchor kinds), the existing REV-01 hold classes, and the Access-related
regression classes.

## Not claimed

No live Cloudflare run: the provider is the AC1011 fake, so whether an old cookie
is denied after an edit is the same open question as for REV-01 (the hold is a
bound, not proof).  The front-end change is limited to reading `saved` (a stored
change that is still being applied is shown as stored with its explanation, and
the screen is refreshed); it was checked by syntax and code reading only, not by a
UI run.  No change to the Operator.  No claim
that a widening is *never* worth revoking (it is not needed for safety); the
exemption can be switched to "revoke on any change" by a one-line change if the
owner prefers #1707's behaviour.

## Why the front-end part (production observation, 2026-10-28)

The REV-01 run on production (platform 18.0.1.0.275, batch69) removed a policy through
the product and the call came back `success: false` with "...Retry this operation."
while the policy was already gone and the revocation hold was open (the first
`revoke_tokens` also got an HTTP 500 from Cloudflare, so the hold lasted 12 min 22 s).
The pages treated that as a failed write: the removed policy stayed on screen and
"retry" was meaningless.  With `saved: true` and the reworded message, the three
Access surfaces (Security Access detail, Smart Home access tab, Cloud Service access
tab) and the policy modal now refresh and show the explanation.
