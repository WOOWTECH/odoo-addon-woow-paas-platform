"""Local renewal requirements shared by billing and LLM conversion reserves.

``sh_plan_price`` is the existing tax-inclusive renewal amount. Keep the billing
API's inclusive date window and per-subscription sufficient flags unchanged.
No Odoo imports or remote calls: callers supply their transaction and date.
"""
from datetime import timedelta


def upcoming_renewal_requirements(env, partner, *, today, days=3,
                                  hidden_subscription_ids=None):
    """Return ``(upcoming, total_needed)`` for this partner's visible renewals.

    ``hidden_subscription_ids`` lets the existing billing presentation mixin
    supply its exclusion list. Model callers derive the same state-independent
    concealed-instance chain here; merely unlisted templates remain visible.
    """
    if type(days) is not int or days < 0:
        raise ValueError('Renewal window must be a non-negative integer.')
    if hidden_subscription_ids is None:
        # Preserve HiddenChainMixin's two searches, including each model's
        # active_test context; a joined template predicate can differ for
        # archived templates.
        template_ids = env['woow_paas_platform.cloud_app_template'].sudo().search([
            ('conceal_instances', '=', True),
        ]).ids
        services = env['woow_paas_platform.cloud_service'].sudo().search([
            ('template_id', 'in', template_ids),
        ])
        hidden_subscription_ids = services.mapped('subscription_id').ids
    subs = env['sh.subscription.subscription'].sudo().search([
        ('sh_partner_id', '=', partner.id),
        ('state', '=', 'in_progress'),
        ('sh_date_of_next_payment', '>=', today),
        ('sh_date_of_next_payment', '<=', today + timedelta(days=days)),
        ('id', 'not in', hidden_subscription_ids),
    ])
    upcoming = []
    total_needed = 0.0
    for sub in subs:
        amount = sub.sh_plan_price or 0.0
        total_needed += amount
        upcoming.append({
            'subscription_id': sub.id,
            'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else '',
            'amount': amount,
            'renewal_date': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
            'sufficient': partner.wallet_balance >= amount,
        })
    return upcoming, total_needed
