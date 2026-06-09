"""18.0.1.0.37 — Security Access P2.5: migrate Service-wide Access → Per-route.

P2.5 downshifts the Access three-piece (policies / login methods / session
duration) + the CF Access app id from the SA layer to the route sub-model: each
Application Route is now its own Cloudflare Access application
(``route.is_protected`` = that route's Access on/off). The SA-level fields
(``access_enabled`` / ``login_google`` / ``login_email_otp`` /
``session_duration`` / ``access_app_id`` / ``access_unresolved_idp_types`` and
the ``access_policy_ids`` inverse keyed on ``security_access_id``) are gone, and
``access_policy.route_id`` is the new (required) binding.

What this migration does, per existing Security Access DB row:

1. Copy the old SA-level login_google / login_email_otp / session_duration onto
   EACH of that SA's is_protected routes (those routes were the destinations of
   the single SA-level Access app, so they keep the same login/session).
2. Re-home every legacy access_policy (security_access_id = this SA, route_id
   still NULL) onto that SA's protected route(s). One protected route → set
   route_id directly. Multiple protected routes → COPY the policy to each
   (behaviour-equivalent: the old single shared app's policy set applied to all
   its destinations, so each per-route app gets the same set). Zero protected
   routes → attach to the primary route as an inert (unprotected) carrier so no
   policy is silently dropped; it stays inert until that route is protected.
3. Clear the orphan SA-level access_app_id column and LOG the old id, so a stg
   operator can manually tear down the single legacy CF Access app (the
   per-route re-sync rebuilds one app per protected route — see Phase 5).

Idempotent: a policy whose route_id is already set is skipped; re-running finds
no NULL-route_id legacy policy and no orphan SA access_app_id to clear.

prod is a NO-OP: prod has no P2 Access data (prod operator is still the pre-P2
image, no live SA Access settings), so there are no SA-level access values to
move. stg holds the only runtime-created test SA rows this migration touches.

Implementation note: the old SA-level columns are read via RAW SQL because the
P2.5 ORM no longer declares those fields — Odoo leaves the columns in place as
orphans (it does not auto-drop removed-field columns), so they are still present
here in post-migrate. Each read is guarded by a column-existence check so a
genuine fresh install (no legacy columns) is a clean no-op.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_SA_TABLE = 'woow_paas_platform_security_access'
_LEGACY_COLS = (
    'access_enabled', 'login_google', 'login_email_otp',
    'session_duration', 'access_app_id',
)


def _existing_legacy_cols(cr):
    cr.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND column_name = ANY(%s)
        """,
        (_SA_TABLE, list(_LEGACY_COLS)),
    )
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    cols = _existing_legacy_cols(cr)
    if 'access_app_id' not in cols and 'session_duration' not in cols:
        # No legacy SA-level Access columns at all → fresh install / nothing to
        # migrate (prod path). Clean no-op.
        _logger.info(
            "18.0.1.0.37 P2.5 migration: no legacy SA-level Access columns "
            "found — nothing to migrate (prod no-op / fresh install).")
        return

    # Read every SA's legacy SA-level Access values via raw SQL (the P2.5 ORM
    # no longer maps these columns; they survive as orphans until cleaned below).
    select_cols = ['id'] + [c for c in _LEGACY_COLS if c in cols]
    cr.execute(
        "SELECT %s FROM %s" % (', '.join(select_cols), _SA_TABLE)
    )
    rows = cr.fetchall()
    sa_legacy = {}
    for row in rows:
        rec = dict(zip(select_cols, row))
        sa_legacy[rec['id']] = rec

    env = api.Environment(cr, SUPERUSER_ID, {})
    Route = env['woow_paas_platform.security_access_route']
    Policy = env['woow_paas_platform.security_access_access_policy']

    moved_policies = 0
    copied_policies = 0
    touched_routes = 0
    cleared_app_ids = 0

    for sa_id, legacy in sa_legacy.items():
        routes = Route.search([('security_access_id', '=', sa_id)])
        if not routes:
            continue
        protected = routes.filtered('is_protected')
        primary = routes.filtered('is_primary')[:1] or routes[:1]

        # ---- 1. copy SA-level login/session onto each protected route ----
        login_g = bool(legacy.get('login_google'))
        login_otp = bool(legacy.get('login_email_otp'))
        session = legacy.get('session_duration') or '24h'
        for route in protected:
            vals = {}
            if route.login_google != login_g:
                vals['login_google'] = login_g
            if route.login_email_otp != login_otp:
                vals['login_email_otp'] = login_otp
            if route.session_duration != session:
                vals['session_duration'] = session
            if vals:
                # sa_skip_route_sync: migration must not call the operator; it
                # only re-homes DB state. Phase 5 does the live CF re-sync.
                route.with_context(sa_skip_route_sync=True).write(vals)
                touched_routes += 1

        # ---- 2. re-home legacy policies (route_id still NULL) ----
        legacy_pols = Policy.search([
            ('security_access_id', '=', sa_id),
            ('route_id', '=', False),
        ])
        if legacy_pols:
            # Target routes: the protected ones (each becomes its own app). If
            # none are protected, park on the primary route as an inert carrier
            # so nothing is dropped. Idempotent: NULL route_id only.
            targets = protected or primary
            for pol in legacy_pols:
                first = targets[:1]
                pol.with_context(sa_skip_route_sync=True).write(
                    {'route_id': first.id})
                moved_policies += 1
                # multiple protected routes → copy the same policy to each
                # additional one (behaviour-equivalent fan-out). sa_skip_route_
                # sync so the copy does not call the operator during migration.
                for extra in targets[1:]:
                    pol.with_context(sa_skip_route_sync=True).copy(default={
                        'route_id': extra.id,
                        'cf_policy_id': False,
                    })
                    copied_policies += 1

        # ---- 3. clear orphan SA-level access_app_id + log it ----
        if 'access_app_id' in cols:
            old_app = legacy.get('access_app_id')
            if old_app:
                cr.execute(
                    "UPDATE %s SET access_app_id = NULL WHERE id = %%s"
                    % _SA_TABLE,
                    (sa_id,),
                )
                cleared_app_ids += 1
                _logger.warning(
                    "18.0.1.0.37 P2.5 migration: SA id=%s had legacy CF Access "
                    "app id %r (single Service-wide app). Cleared from DB; this "
                    "app must be MANUALLY torn down in Cloudflare/stg — the "
                    "per-route re-sync rebuilds one app per protected route. "
                    "See Phase 5.", sa_id, old_app)

    _logger.info(
        "18.0.1.0.37 P2.5 migration done: re-homed %s legacy polic(ies) "
        "(+%s copied to extra protected routes), updated login/session on %s "
        "route(s), cleared %s legacy SA access_app_id(s).",
        moved_policies, copied_policies, touched_routes, cleared_app_ids)
