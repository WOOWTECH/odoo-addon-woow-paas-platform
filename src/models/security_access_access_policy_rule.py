import ipaddress
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError

# Shared with the policy格式驗證：bare domain（不可帶 @），如 acme.com。
_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)
# ISO 3166-1 Alpha-2 國碼，如 TW / US（大小寫不拘，存前 upper）。
_COUNTRY_RE = re.compile(r'^[A-Za-z]{2}$')


class SecurityAccessAccessPolicyRule(models.Model):
    """One selector rule inside an Access policy（P-rule 子模型）.

    P-rule（2026-06）把「1 policy = 1 flat rule」拆成「1 policy → N rules」，
    對齊 Cloudflare Access 原生 policy 結構：每條 rule 屬於 include / require /
    exclude 三組之一（OR / AND / NOT 語意），policy 是 rule 的容器並承載
    decision + precedence（sequence）。

    rule 不直接呼叫 operator——父 policy 的 route 以宣告式 full-state 推送
    （鏡像 policy 子模型）。route_id 經 policy_id 兩跳 stored related 物化
    （policy_id.security_access_id），讓 ir.rule workspace walk 與 SA-level
    email 配額 count 可 traverse。
    """

    _name = 'woow_paas_platform.security_access_access_policy_rule'
    _description = 'Security Access — Access Policy Rule'
    _order = 'sequence, id'

    policy_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_access_policy',
        string='Access Policy', required=True, ondelete='cascade', index=True,
        help='The policy this rule belongs to. The policy carries the '
             'allow/deny/bypass decision; this rule is one selector within '
             'its include/require/exclude group.',
    )
    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        related='policy_id.security_access_id', store=True, index=True,
        help='Derived from policy_id (two hops); kept stored so the ir.rule '
             'workspace walk and the SA-level email quota can traverse it.',
    )
    group = fields.Selection(
        selection=[
            ('include', 'Include'),
            ('require', 'Require'),
            ('exclude', 'Exclude'),
        ],
        string='Group', default='include', required=True,
        help='Include = OR (match any), Require = AND (match all), '
             'Exclude = NOT (match none). A policy must have at least one '
             'Include rule.',
    )
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
            ('everyone', 'Everyone'),
            ('country', 'Country'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
    rule_value = fields.Char(
        string='Rule Value',
        help='Email domain (no @) / comma-separated emails / CIDR / '
             'comma-separated ISO-2 country codes, per rule_type. '
             'Empty for "everyone".',
    )
    sequence = fields.Integer(string='Display Order', default=10)

    @api.constrains('rule_type', 'rule_value', 'group')
    def _check_rule_value(self):
        for rule in self:
            val = (rule.rule_value or '').strip()
            if rule.rule_type == 'everyone':
                # everyone 是「所有人」selector，不帶值；有值多半是誤填。
                if val:
                    raise ValidationError(
                        "The 'Everyone' rule takes no value — got %r."
                        % rule.rule_value)
                continue
            if rule.rule_type == 'email_domain':
                if val.startswith('@') or not _DOMAIN_RE.match(val):
                    raise ValidationError(
                        "Email domain must be a bare domain (no @), e.g. "
                        "acme.com — got %r" % rule.rule_value)
            elif rule.rule_type == 'email_list':
                emails = [e.strip() for e in val.split(',') if e.strip()]
                if not emails or any('@' not in e for e in emails):
                    raise ValidationError(
                        "Email list must be comma-separated email addresses.")
            elif rule.rule_type == 'ip_range':
                try:
                    ipaddress.ip_network(val, strict=False)
                except ValueError:
                    raise ValidationError(
                        "IP range must be a valid CIDR, e.g. 203.0.113.0/24 — "
                        "got %r" % rule.rule_value)
            elif rule.rule_type == 'country':
                codes = [c.strip() for c in val.split(',') if c.strip()]
                if not codes or any(not _COUNTRY_RE.match(c) for c in codes):
                    raise ValidationError(
                        "Country must be comma-separated ISO 3166-1 alpha-2 "
                        "codes, e.g. TW,US — got %r" % rule.rule_value)

    def _suppress_route_sync(self):
        """Whether a per-rule operator sync should be skipped for this op.

        sa_skip_route_sync: migration / provision bulk row moves converge CF via
        their own explicit re-sync, not per-row here.

        sa_rule_replace: a policy-driven whole-rule-set replace (controller
        _update_policy writes (5,0,0)+creates). Odoo runs the (5,0,0) unlink
        FULLY — including this per-rule sync — BEFORE the (0,0,) creates, so a
        per-rule sync in that window pushes the policy in its transient
        empty-include state and the operator rejects "≥1 include" (422), aborting
        the whole edit. The policy's own write() issues the final full-state sync
        after the replace settles, so per-rule syncs here are wrong AND redundant.
        """
        ctx = self.env.context
        return bool(ctx.get('sa_skip_route_sync') or ctx.get('sa_rule_replace'))

    # Atomicity + sync: mirror the policy sub-model. A rule create/write/unlink
    # re-syncs its policy's ROUTE (the route owns the declarative full-state
    # push to the operator). Wrapped in a savepoint so a sync raise rolls the
    # rule row back. The liveness gate lives inside route._sync_access_to_operator
    # (no-ops without a live tunnel / unprotected route), so we always call it
    # and let the route decide. _suppress_route_sync() skips the per-rule call
    # for migration/provision bulk moves (sa_skip_route_sync) and for a
    # policy-driven rule-set replace (sa_rule_replace), where the policy's own
    # write() owns the final full-state sync.
    #
    # Note: creating a policy WITH rule_ids fires BOTH the nested rule sync and
    # the policy's own post-create sync; the duplicate is benign — the route
    # push is an idempotent full-state declaration and the liveness gate inside
    # route._sync keeps a pending SA from calling the operator at all.

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            rules = super().create(vals_list)
            policies = rules.mapped('policy_id')
            # Adding rules can only push email count OVER the cap or attach an
            # email selector to a bypass policy — re-check those (partial-state
            # safe: both only raise on a real violation, never on an undercount).
            # The "≥1 include" check is NOT re-run here: a fresh policy create
            # may add a non-include rule first, and the policy-level constraint
            # already covers the final state.
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            if not self._suppress_route_sync():
                for route in rules.mapped('policy_id.route_id'):
                    route._sync_access_to_operator()
        return rules

    def write(self, vals):
        policies = self.mapped('policy_id')
        with self.env.cr.savepoint():
            result = super().write(vals)
            policies = (policies | self.mapped('policy_id'))
            policies._check_at_least_one_include()
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            if (not self._suppress_route_sync()
                    and any(k in vals for k in (
                        'group', 'rule_type', 'rule_value', 'sequence',
                        'policy_id'))):
                for route in policies.mapped('route_id'):
                    route._sync_access_to_operator()
        return result

    def unlink(self):
        policies = self.mapped('policy_id')
        routes = policies.mapped('route_id')
        with self.env.cr.savepoint():
            result = super().unlink()
            # Removing a rule can drop the last Include — re-validate surviving
            # policies (a policy deleted by cascade no longer exists → skipped).
            # sa_rule_replace: a whole-rule-set replace (controller _update_policy
            # does (5,0,0)+creates) transiently empties the policy before the new
            # rules land; suppress the per-rule check during that window — the
            # policy-level @api.constrains validates the final state.
            if not self.env.context.get('sa_rule_replace'):
                policies.exists()._check_at_least_one_include()
            if not self._suppress_route_sync():
                for route in routes.exists():
                    route._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'group': self.group,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence,
        }
