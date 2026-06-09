import ipaddress
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


class SecurityAccessAccessPolicy(models.Model):
    """One reusable Cloudflare Access policy on a ROUTE (P2.5 per-route).

    Mirrors the route sub-model: rows are the desired-state, the parent route
    pushes the full set declaratively to the operator (no per-row CF call).
    cf_policy_id stores the Cloudflare reusable-policy id after sync.

    P2.5: the real binding is ``route_id`` — each Application Route is its own
    Cloudflare Access application with its own policies. ``security_access_id``
    is kept as a store=True related on ``route_id.security_access_id`` so the
    existing ir.rule walk and the SA-level policy quota keep working
    (the related store materialises a real column the ir.rule domain / quota
    count can traverse), but it is no longer the field a caller binds.
    """

    _name = 'woow_paas_platform.security_access_access_policy'
    _description = 'Security Access — Access Policy'
    _order = 'sequence, id'

    route_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_route',
        string='Application Route', required=True, ondelete='cascade',
        index=True,
        help='The route this policy protects. Each protected route is its own '
             'Cloudflare Access application with its own policy set.',
    )
    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        related='route_id.security_access_id', store=True, index=True,
        help='Derived from route_id; kept stored so the ir.rule workspace walk '
             'and the SA-level policy quota can traverse it. The real binding '
             'is route_id.',
    )
    name = fields.Char(string='Name', required=True)
    decision = fields.Selection(
        selection=[('allow', 'Allow'), ('deny', 'Block')],
        string='Decision', default='allow', required=True,
        help='Allow grants access; Block denies. Block rules win over allow.',
    )
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
    rule_value = fields.Char(
        string='Rule Value', required=True,
        help='Email domain (no @) / comma-separated emails / CIDR, per type.',
    )
    sequence = fields.Integer(string='Evaluation Order', default=10)
    cf_policy_id = fields.Char(string='CF Policy ID', copy=False)

    @api.constrains('rule_type', 'rule_value')
    def _check_rule_value(self):
        for pol in self:
            val = (pol.rule_value or '').strip()
            if pol.rule_type == 'email_domain':
                if val.startswith('@') or not _DOMAIN_RE.match(val):
                    raise ValidationError(
                        "Email domain must be a bare domain (no @), e.g. "
                        "acme.com — got %r" % pol.rule_value)
            elif pol.rule_type == 'email_list':
                emails = [e.strip() for e in val.split(',') if e.strip()]
                if not emails or any('@' not in e for e in emails):
                    raise ValidationError(
                        "Email list must be comma-separated email addresses.")
            elif pol.rule_type == 'ip_range':
                try:
                    ipaddress.ip_network(val, strict=False)
                except ValueError:
                    raise ValidationError(
                        "IP range must be a valid CIDR, e.g. 203.0.113.0/24 — "
                        "got %r" % pol.rule_value)

    @api.constrains('security_access_id', 'route_id')
    def _check_policy_quota(self):
        # P2.5: the quota is an SA-LEVEL total (plan.sa_max_access_policies is
        # a per-SA allowance, not per-route), so count every policy across all
        # of the SA's routes via the stored related security_access_id. Policies
        # now bind to routes, but the budget is still pooled per Security Access.
        #
        # sa_skip_policy_quota: the P2.5 migration fan-out copies the OLD single
        # shared app's legacy policy set onto EACH protected route (behaviour-
        # equivalent re-home, not a user adding capacity). The quota was a per-SA
        # total designed for the old single-app model where the set was counted
        # once; fanning the same set across M routes multiplies the count under
        # the same budget, so an SA with a real subscription near its quota would
        # trip this constraint and abort the migration. The migration runs the
        # re-home + fan-out under this key so the constraint is bypassed for that
        # one-shot data move; normal user-driven create/write keep enforcing it.
        # sa_skip_policy_quota lives on the transaction CURSOR, not the context:
        # this constraint also fires from the deferred recompute of the stored
        # related security_access_id at flush time, and that recompute re-browses
        # the records in a context-STRIPPED env, so a context-only flag is
        # silently dropped on those passes (the fan-out copy would still abort).
        # The cursor is shared by every env in the transaction, so a flag set on
        # it survives the recompute env reconstruction. Set + cleared by the
        # migration around its one-shot data move.
        if getattr(self.env.cr, 'sa_skip_policy_quota', False):
            return
        for pol in self:
            sa = pol.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            count = self.search_count([('security_access_id', '=', sa.id)])
            if count > quota:
                raise ValidationError(
                    "Access policy quota exceeded: plan '%s' allows %d "
                    "polic(ies)." % (sub.sh_subscription_plan_id.name, quota))

    # Atomicity scope: mirror the route sub-model's savepoint so a
    # create/write/unlink whose Access re-sync raises rolls the DB row back and
    # re-raises, rather than leaving a committed policy row the operator never
    # received. The guarantee is Odoo-side ONLY — sync_access_app is a
    # declarative full-state push with no compensating action, so a
    # mid-reconcile operator failure can still leave Cloudflare partially
    # mutated; that residual CF↔DB divergence converges via the next idempotent
    # full-state re-sync, not via this savepoint (same scope the route model
    # documents). A stale policy row alone is not served publicly, so the
    # window is benign relative to routes, but keeping the savepoint matters
    # for parity: a BFF that catches the sync error and reports success would
    # otherwise leave the row committed while CF was never updated.

    # P2.5: a policy now belongs to ONE route, so a mutation re-syncs that
    # route's own Access app (not the whole SA). The liveness gate lives inside
    # route._sync_access_to_operator (it no-ops without a live tunnel / when the
    # route is not protected), so create/write/unlink just call the route sync
    # and let the route decide. This keeps the gate single-sourced on the route.

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            policies = super().create(vals_list)
            # sa_skip_route_sync: migration / provision-time bulk row moves set
            # this so the row re-home does NOT call the operator (CF converges
            # via the explicit re-sync those flows issue, not per-row here).
            if not self.env.context.get('sa_skip_route_sync'):
                for route in policies.mapped('route_id'):
                    route._sync_access_to_operator()
        return policies

    def write(self, vals):
        with self.env.cr.savepoint():
            result = super().write(vals)
            if (not self.env.context.get('sa_skip_route_sync')
                    and any(k in vals for k in (
                        'name', 'decision', 'rule_type', 'rule_value',
                        'sequence', 'route_id'))):
                for route in self.mapped('route_id'):
                    route._sync_access_to_operator()
        return result

    def unlink(self):
        affected = self.mapped('route_id')
        # Capture each doomed row's cf_policy_id BEFORE super().unlink() wipes
        # it. The operator deletes reusable policies only via existing_policy_ids
        # (delete-old-then-create), which _build_access_payload derives from the
        # SURVIVING rows. Once the row is gone its cf id never enters that list,
        # so the operator never deletes the policy → permanent CF orphan, one
        # leaked per unlink. Inject the captured ids per-ROUTE on the resync so
        # the operator reaps them.
        doomed_by_route = {}
        for pol in self:
            if pol.cf_policy_id and pol.route_id:
                doomed_by_route.setdefault(
                    pol.route_id.id, []).append(pol.cf_policy_id)
        with self.env.cr.savepoint():
            result = super().unlink()
            for route in affected:
                if route.exists():
                    doomed = doomed_by_route.get(route.id) or []
                    route.with_context(
                        sa_unlinked_cf_policy_ids=doomed
                    )._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'name': self.name, 'decision': self.decision,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence, 'route_id': self.route_id.id,
        }
