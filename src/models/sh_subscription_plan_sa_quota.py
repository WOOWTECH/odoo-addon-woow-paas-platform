from odoo import fields, models


class ShSubscriptionPlanSaQuota(models.Model):
    """Security Access quota dimensions on subscription plans.

    Only adds new fields — never overrides std field types
    (odoo-std-field-no-override pattern). The four quotas mirror the four
    Security Access sub-resources (routes / access policies / custom
    domains / email rules); P1 enforces sa_max_routes, the others are
    enforced by their own phases (P2-P4) but modelled now so plan seeds
    don't churn.
    """

    _inherit = 'sh.subscription.plan'

    sa_max_routes = fields.Integer(
        string='SA Max Application Routes',
        default=0,
        help='Security Access: max user-defined application routes '
             '(excluding the auto-created primary route). 0 = none allowed.',
    )
    sa_max_access_policies = fields.Integer(
        string='SA Max Access Policies',
        default=0,
        help='Security Access: max access policies (enforced in P2). '
             '0 = unlimited (NOT "none allowed" — sa_max_routes above uses the '
             'opposite convention for 0; see #743). No seeded plan sets 0 '
             'today; the CS access variant is specced to.',
    )
    sa_max_custom_domains = fields.Integer(
        string='SA Max Custom Domains',
        default=0,
        help='Security Access: max custom domains (enforced in P3).',
    )
    sa_max_email_rules = fields.Integer(
        string='SA Max Email Routing Rules',
        default=0,
        help='Security Access: max email routing rules (enforced in P4).',
    )
    sa_max_emails_per_policy = fields.Integer(
        string='SA Max Emails per Access Policy',
        default=20,
        help='Security Access: max email addresses across the email_list '
             'rules of a single access policy. 0 = unlimited.',
    )
