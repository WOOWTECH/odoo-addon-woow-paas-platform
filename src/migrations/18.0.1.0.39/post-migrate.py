"""18.0.1.0.39 — Security Access P-rule: flat policy rule → include sub-rule.

P-rule downshifts the policy's flat ``rule_type`` / ``rule_value`` into a
``security_access_access_policy_rule`` sub-model so a policy can hold many
selector rules across include / require / exclude groups (Cloudflare's native
shape). Existing policies each carried exactly one selector, which becomes one
``group='include'`` rule (the legacy semantics: a single allow/deny condition is
an OR-of-one include).

What this migration does, per existing access_policy row:

1. Read the orphan flat columns (rule_type / rule_value) via RAW SQL — the
   P-rule ORM no longer declares those fields, so Odoo leaves the columns in
   place as orphans (it does not auto-drop removed-field columns). Each read is
   guarded by a column-existence check so a genuine fresh install (no legacy
   columns) is a clean no-op.
2. For each policy with no rules yet, create one ``group='include'`` rule
   carrying the old (rule_type, rule_value). The policy's decision is unchanged
   (it stays on the policy row).
3. Heal the per-policy email quota seed: write sa_max_emails_per_policy=20 onto
   the four seeded Security Access plans if it is still 0 (the seed `-u` applies
   the value on noupdate="0" data, but this defends the sticky-noupdate upgrade
   path — see seed-noupdate-sticky-template-links).

Idempotent: a policy that already has rules is skipped; re-running finds none.

prod is a NO-OP: prod has no P2/P2.5 Access policy data (prod operator predates
the Access feature), so there are no flat policies to migrate. stg holds the only
runtime-created test policies this migration touches.

Cursor flag: creating the include rules fires the policy email-quota constraint
(_check_emails_per_policy) — including from the deferred recompute of the stored
related security_access_id in a context-STRIPPED env. The bypass flag is set on
the transaction CURSOR (shared across all envs) rather than the context, and
env.flush_all() forces the deferred recompute to run NOW while the flag is still
set, mirroring 18.0.1.0.37. sa_skip_route_sync (context) keeps the row moves from
calling the operator — CF converges via the per-route re-sync, not per-row here.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_POLICY_TABLE = 'woow_paas_platform_security_access_access_policy'
_LEGACY_COLS = ('rule_type', 'rule_value')
_SEEDED_PLAN_XMLIDS = (
    'woow_paas_platform.plan_security_access_entry_monthly',
    'woow_paas_platform.plan_security_access_entry_yearly',
    'woow_paas_platform.plan_security_access_pro_monthly',
    'woow_paas_platform.plan_security_access_pro_yearly',
)


def _existing_legacy_cols(cr):
    cr.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND column_name = ANY(%s)
        """,
        (_POLICY_TABLE, list(_LEGACY_COLS)),
    )
    return {row[0] for row in cr.fetchall()}


def _heal_email_quota_seed(env):
    # Defend the sticky-noupdate upgrade path: ensure the four seeded SA plans
    # carry sa_max_emails_per_policy=20 even if the seed write was skipped. A
    # no-op when the seed already applied (value already 20).
    healed = 0
    for xmlid in _SEEDED_PLAN_XMLIDS:
        plan = env.ref(xmlid, raise_if_not_found=False)
        if plan and not plan.sa_max_emails_per_policy:
            plan.sa_max_emails_per_policy = 20
            healed += 1
    if healed:
        _logger.info(
            "18.0.1.0.39 P-rule migration: healed sa_max_emails_per_policy=20 "
            "on %s seeded plan(s) (sticky-noupdate upgrade path).", healed)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _heal_email_quota_seed(env)

    cols = _existing_legacy_cols(cr)
    if 'rule_type' not in cols and 'rule_value' not in cols:
        # No legacy flat columns → fresh install / nothing to migrate.
        _logger.info(
            "18.0.1.0.39 P-rule migration: no legacy flat rule columns found "
            "— nothing to migrate (prod no-op / fresh install).")
        return

    select_cols = ['id'] + [c for c in _LEGACY_COLS if c in cols]
    cr.execute("SELECT %s FROM %s" % (', '.join(select_cols), _POLICY_TABLE))
    rows = [dict(zip(select_cols, r)) for r in cr.fetchall()]
    if not rows:
        _logger.info(
            "18.0.1.0.39 P-rule migration: no access policies present — "
            "nothing to migrate.")
        return

    Policy = env['woow_paas_platform.security_access_access_policy']
    Rule = env['woow_paas_platform.security_access_access_policy_rule']

    created = 0
    skipped = 0
    # sa_skip_policy_quota (CURSOR flag): the include-rule create fires
    # _check_emails_per_policy, also via the deferred stored-related recompute in
    # a context-stripped env; the cursor flag survives that env reconstruction.
    cr.sa_skip_policy_quota = True
    try:
        for rec in rows:
            pol = Policy.browse(rec['id'])
            if not pol.exists():
                continue
            if pol.rule_ids:
                skipped += 1
                continue
            rule_type = rec.get('rule_type') or 'email_domain'
            rule_value = rec.get('rule_value') or ''
            Rule.with_context(sa_skip_route_sync=True).create({
                'policy_id': pol.id,
                'group': 'include',
                'rule_type': rule_type,
                'rule_value': rule_value,
            })
            created += 1
        # Force pending recomputes (+ their constraint re-fire) to run NOW,
        # while the bypass flag is still set.
        env.flush_all()
    finally:
        cr.sa_skip_policy_quota = False

    _logger.info(
        "18.0.1.0.39 P-rule migration done: created %s include rule(s) from "
        "flat policies, skipped %s already-migrated policy(ies).",
        created, skipped)
