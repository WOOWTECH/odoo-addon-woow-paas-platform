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
        _logger.info(
            "[SUB_MIXIN] _create_and_confirm_subscription entry entity=%s:%s partner=%s plan=%s",
            self._name, self.id, partner.id, plan.id,
        )
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
            "[SUB_MIXIN] created subscription=%s name=%s plan=%s entity=%s:%s trial=%s",
            subscription.id, subscription.name, plan.name,
            self._name, self.id, subscription.sh_trial,
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
        if not sub:
            return False
        if sub.state == 'cancel':
            return True
        if not sub.sh_end_date:
            return False
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
    # Skip trial
    # ----------------------------------------------------------------

    def _skip_trial(self):
        """Skip the trial period and start paid billing immediately.

        Creates the first invoice and advances the next payment date
        by one billing cycle.

        Returns:
            dict with invoice info
        Raises:
            UserError if not in trial
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        _logger.info(
            "[SUB_MIXIN] _skip_trial entry entity=%s:%s sub=%s",
            self._name, self.id, sub.id if sub else None,
        )
        if not sub:
            raise UserError("No subscription linked to this service.")
        if sub.state != 'in_progress':
            raise UserError("Subscription is not active.")
        if not sub.sh_trial:
            raise UserError("Subscription is not in trial period.")

        today = fields.Date.today()

        # Check wallet balance before proceeding
        partner = sub.sh_partner_id
        plan_price = sub.sh_plan_price
        _logger.info(
            "[SUB_MIXIN] _skip_trial balance check sub=%s partner=%s price=%s balance=%s",
            sub.id, partner.id, plan_price, partner.wallet_balance,
        )
        if partner.wallet_balance < plan_price:
            raise UserError(
                "Insufficient wallet balance to skip trial. "
                "Current balance: %.2f, required: %.2f. "
                "Please top up your wallet first."
                % (partner.wallet_balance, plan_price)
            )

        # End trial and set billing to start today
        sub.write({
            'sh_trial': False,
            'sh_trial_end_date': today - relativedelta(days=1),
            'sh_start_date': today,
            'sh_date_of_next_payment': today,
        })

        # Generate and create invoice
        invoice_vals = sub._generate_invoice_vals()
        invoice = self.env['account.move'].sudo().create(invoice_vals)
        sub.compute_view_invoice()
        _logger.info(
            "[SUB_MIXIN] _skip_trial invoice created sub=%s invoice=%s amount=%s",
            sub.id, invoice.id, invoice.amount_total,
        )

        # Advance next payment by one billing cycle
        unit_deltas = {
            'day': relativedelta(days=sub.sh_recurrency),
            'week': relativedelta(weeks=sub.sh_recurrency),
            'month': relativedelta(months=sub.sh_recurrency),
            'year': relativedelta(years=sub.sh_recurrency),
        }
        delta = unit_deltas.get(sub.sh_unit)
        if delta:
            sub.sh_date_of_next_payment = today + delta

        # Post/pay invoice based on company settings
        sub._handle_invoice_creation(invoice)

        _logger.info(
            "[SUB_MIXIN] _skip_trial done entity=%s:%s sub=%s invoice=%s amount=%s next_payment=%s",
            self._name, self.id, sub.id, invoice.name,
            invoice.amount_total, sub.sh_date_of_next_payment,
        )

        return {
            'invoice_id': invoice.id,
            'amount': invoice.amount_total,
        }

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
