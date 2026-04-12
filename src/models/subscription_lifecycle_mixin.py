import logging
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SubscriptionLifecycleMixin(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_mixin'
    _description = 'Subscription Lifecycle Mixin'

    subscription_id = fields.Many2one(
        'sh.subscription.subscription',
        string='Subscription',
        ondelete='set null',
        copy=False,
    )

    # Computed convenience fields for UI display
    subscription_state = fields.Selection(
        related='subscription_id.state',
        string='Subscription State',
        store=True,
    )
    subscription_plan_name = fields.Char(
        related='subscription_id.sh_subscription_plan_id.name',
        string='Plan',
    )
    subscription_end_date = fields.Date(
        related='subscription_id.sh_end_date',
        string='Subscription End Date',
    )
    subscription_next_payment = fields.Date(
        related='subscription_id.sh_date_of_next_payment',
        string='Next Payment Date',
    )
    subscription_is_trial = fields.Boolean(
        related='subscription_id.sh_trial',
        string='Is Trial',
    )
    subscription_trial_end = fields.Date(
        related='subscription_id.sh_trial_end_date',
        string='Trial End Date',
    )

    # Suspend/resume state tracking
    pre_suspend_state = fields.Char(
        string='Pre-Suspend State',
        help='State before suspension, used to restore on resume',
    )

    # ----------------------------------------------------------------
    # Subscription creation
    # ----------------------------------------------------------------

    def _create_and_confirm_subscription(self, partner, plan):
        """Create a subscription linked to this entity and confirm it.

        Args:
            partner: res.partner record (workspace owner's partner)
            plan: sh.subscription.plan record
        Returns:
            sh.subscription.subscription record
        """
        self.ensure_one()
        Subscription = self.env['sh.subscription.subscription']

        # sh_subscription_plan_id is on product.template; find product via template
        product_tmpl = self.env['product.template'].sudo().search([
            ('sh_product_subscribe', '=', True),
            ('sh_subscription_plan_id', '=', plan.id),
        ], limit=1)

        if not product_tmpl:
            raise UserError(
                "No subscription product found for plan: %s" % plan.name
            )

        # Get the first variant of this template
        product = product_tmpl.product_variant_id

        # Validate subscription journal is configured
        # (sh_subscription uses company.sh_journal_id for invoice creation;
        #  post_init_hook sets a default, admin can override in Settings → Woow PaaS)
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            raise UserError(
                "Subscription journal is not configured. "
                "Please go to Settings → Woow PaaS → Subscription Lifecycle "
                "and select a journal."
            )

        sub_vals = {
            'sh_partner_id': partner.id,
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
            'sh_source': 'manual',
        }
        subscription = Subscription.sudo().create(sub_vals)

        # Trigger onchanges to populate recurrence, prices, and dates.
        # Order matters: plan → product → trial dates → end date.
        subscription._onchange_sh_subscription_plan_id()
        subscription._onchange_sh_product_id()
        subscription._onchange_sh_trial_subcription_start_date()
        subscription._onchange_sh_trial_subcription_end_date()

        # sh_subscription deduplicates trials per partner+plan+product:
        # if the same user already has a subscription with the same plan,
        # onchanges disable trial and set start_date=today (immediate billing).
        # PaaS treats each service independently — force trial from plan.
        if plan.sh_trial and not subscription.sh_trial:
            subscription.sh_trial = True
            subscription.sh_trial_duration = plan.sh_trial_duration
            subscription.sh_trial_unit = plan.sh_trial_unit
            trial_delta = {
                'day': relativedelta(days=plan.sh_trial_duration),
                'week': relativedelta(weeks=plan.sh_trial_duration),
                'month': relativedelta(months=plan.sh_trial_duration),
                'year': relativedelta(years=plan.sh_trial_duration),
            }.get(plan.sh_trial_unit, relativedelta())
            trial_end = date.today() + trial_delta - relativedelta(days=1)
            subscription.sh_trial_end_date = trial_end
            subscription.sh_start_date = trial_end + relativedelta(days=1)
            subscription.sh_date_of_next_payment = subscription.sh_start_date
            _logger.info(
                "Forced trial for subscription %s: %s days, billing starts %s",
                subscription.name, plan.sh_trial_duration,
                subscription.sh_start_date,
            )

        # Confirm → in_progress (starts trial if configured)
        subscription.sh_subscription_confirm()

        self.write({'subscription_id': subscription.id})
        _logger.info(
            "Created subscription %s (plan=%s) for %s id=%s",
            subscription.name, plan.name, self._name, self.id,
        )
        return subscription

    # ----------------------------------------------------------------
    # Subscription state checks
    # ----------------------------------------------------------------

    def _get_grace_period_days(self):
        return int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))

    def _get_cleanup_after_months(self):
        return int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'
        ))

    def _is_subscription_active(self):
        """Check if subscription allows normal operations."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub:
            return True  # No subscription = unrestricted (admin/legacy)
        return sub.state == 'in_progress'

    def _is_in_grace_period(self):
        """Check if subscription is expired but within grace period."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub or not sub.sh_end_date:
            return False
        if sub.state not in ('expire', 'close'):
            return False
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return 0 < days_expired <= self._get_grace_period_days()

    def _is_past_grace_period(self):
        """Check if subscription expired beyond grace period."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub or not sub.sh_end_date:
            return False
        if sub.state == 'cancel':
            return True
        if sub.state not in ('expire', 'close'):
            return False
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return days_expired > self._get_grace_period_days()

    def _check_operation_allowed(self, operation='write'):
        """Check if the current user can perform an operation.

        Args:
            operation: 'read' or 'write'
        Raises:
            UserError if not allowed
        """
        self.ensure_one()
        # Admin bypasses all restrictions
        if self.env.user.has_group('base.group_system'):
            return

        if self._is_subscription_active():
            return

        if self._is_in_grace_period():
            if operation == 'read':
                return
            raise UserError(
                "Your subscription has expired. "
                "You have a grace period to renew. "
                "During this time, modifications are not allowed."
            )

        if self._is_past_grace_period():
            raise UserError(
                "Your subscription has expired and the service is suspended. "
                "Please renew your subscription to restore access."
            )

    # ----------------------------------------------------------------
    # Suspend / Resume / Cleanup (abstract — implemented by subclass)
    # ----------------------------------------------------------------

    def _do_suspend(self):
        """Suspend the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    def _do_resume(self):
        """Resume the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    def _do_cleanup(self):
        """Permanently remove the service. Implemented by CloudService / SmartHome."""
        raise NotImplementedError

    # ----------------------------------------------------------------
    # Subscription info for API responses
    # ----------------------------------------------------------------

    def _get_subscription_info(self):
        """Return subscription data dict for API responses."""
        self.ensure_one()
        sub = self.sudo().subscription_id
        if not sub:
            return None
        return {
            'id': sub.id,
            'state': sub.state,
            'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else None,
            'plan_price': sub.sh_plan_price,
            'is_trial': sub.sh_trial,
            'trial_end_date': sub.sh_trial_end_date.isoformat() if sub.sh_trial_end_date else None,
            'start_date': sub.sh_start_date.isoformat() if sub.sh_start_date else None,
            'end_date': sub.sh_end_date.isoformat() if sub.sh_end_date else None,
            'next_payment': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
            'last_payment_status': sub.sh_last_payment_status if hasattr(sub, 'sh_last_payment_status') else None,
        }
