import ipaddress
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)


class SecurityAccessAccessPolicy(models.Model):
    """One reusable Cloudflare Access policy on an SA (service-layer shared).

    Mirrors the route sub-model: rows are the desired-state, the parent SA
    pushes the full set declaratively to the operator (no per-row CF call).
    cf_policy_id stores the Cloudflare reusable-policy id after sync.
    """

    _name = 'woow_paas_platform.security_access_access_policy'
    _description = 'Security Access — Access Policy'
    _order = 'sequence, id'

    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access', required=True, ondelete='cascade', index=True,
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

    @api.constrains('security_access_id')
    def _check_policy_quota(self):
        for pol in self:
            sa = pol.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_access_policies
            count = len(sa.access_policy_ids)
            if count > quota:
                raise ValidationError(
                    "Access policy quota exceeded: plan '%s' allows %d "
                    "polic(ies)." % (sub.sh_subscription_plan_id.name, quota))

    @api.model_create_multi
    def create(self, vals_list):
        policies = super().create(vals_list)
        for sa in policies.mapped('security_access_id'):
            if sa.access_enabled and sa.state == 'active' and sa.tunnel_id:
                sa._sync_access_to_operator()
        return policies

    def write(self, vals):
        result = super().write(vals)
        if any(k in vals for k in ('name', 'decision', 'rule_type',
                                   'rule_value', 'sequence')):
            for sa in self.mapped('security_access_id'):
                if sa.access_enabled and sa.state == 'active' and sa.tunnel_id:
                    sa._sync_access_to_operator()
        return result

    def unlink(self):
        affected = self.mapped('security_access_id')
        result = super().unlink()
        for sa in affected:
            if sa.exists() and sa.access_enabled and sa.state == 'active' \
                    and sa.tunnel_id:
                sa._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'name': self.name, 'decision': self.decision,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence,
        }
