import logging
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _subscription_total_with_tax(sub):
    """Compute (untaxed, tax_rate, tax_amount, total) for a subscription.

    Shared between `_skip_trial_estimate` (skip-trial confirmation modal)
    and the `/api/dashboard/summary` endpoint's `next_bill_estimate` so a
    single tax-computation rule serves both surfaces. Drift here would
    surface as "the modal said 573.85 but the dashboard said 499" — the
    same class of bug as the original Issue #9 root cause.

    Tax source matches what `_generate_invoice_vals` will write onto the
    invoice line: prefer `sh_taxes_ids`, fall back to the subscription's
    product taxes for subs whose onchange chain never populated
    `sh_taxes_ids` (legacy data path).

    Args:
        sub: a `sh.subscription.subscription` recordset (single record).

    Returns:
        tuple of floats: (untaxed, tax_rate, tax_amount, total).
        All zero when sub is empty / sh_plan_price is 0.
    """
    if not sub:
        return 0.0, 0.0, 0.0, 0.0
    untaxed = sub.sh_plan_price or 0.0
    if not untaxed:
        return 0.0, 0.0, 0.0, 0.0

    taxes = sub.sh_taxes_ids
    if not taxes and sub.product_id:
        taxes = sub.product_id.taxes_id

    currency = sub.currency_id or sub.env.company.currency_id

    if taxes:
        compute = taxes.compute_all(
            untaxed,
            partner=sub.sh_partner_id,
            currency=currency,
        )
        tax_amount = compute['total_included'] - compute['total_excluded']
        tax_rate = (tax_amount / untaxed) if untaxed else 0.0
    else:
        tax_amount = 0.0
        tax_rate = 0.0

    return float(untaxed), float(tax_rate), float(tax_amount), float(untaxed + tax_amount)


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
        # #367 (per-entity): capture whether THIS entity already holds a
        # subscription BEFORE we create/link the new one. The 7-day trial is a
        # per-service (per-entity) one-time grant — NOT per-partner (Eugene's
        # 2026-07-19 #367 clarification): every new service gets its own trial;
        # only a re-subscription on a service that already had one (change-plan;
        # or a hypothetical suspend-resume that recreates a sub) must not
        # restart it.
        #
        # ``subscription_id`` is the entity's canonical "is subscribed" pointer
        # and is the reliable, entity-scoped signal here:
        #   • brand-new entity (incl. a partner's 2nd/3rd service) → empty → trial
        #   • change-plan → the wizard / HTTP endpoints close the old sub
        #     (state='close') but do NOT unlink subscription_id (no code path
        #     sets it False; ondelete='set null' only fires on delete, and
        #     change-plan closes — never deletes) → truthy → no trial
        #   • suspend/resume → _do_resume flips state without recreating a
        #     subscription, so it never reaches this method → trial untouched
        # Read here, before the create + the trailing self.write() that replaces
        # it at the end of this method, so it reflects the entity's PRIOR state.
        entity_has_prior_sub = bool(self.sudo().subscription_id)
        _logger.info(
            "[SUB_MIXIN] _create_and_confirm_subscription entry entity=%s:%s partner=%s plan=%s prior_sub=%s",
            self._name, self.id, partner.id, plan.id, entity_has_prior_sub,
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

        # Always override the onchange-chain-computed dates with plan-driven
        # values; the third-party onchange compute is unreliable on both
        # branches (issue #33 for trial, #39 for non-trial). See helper
        # docstrings for the specific arithmetic each branch corrects.
        #
        # #367 (per-entity): grant the trial only when the plan offers one AND
        # this entity had no prior subscription (``entity_has_prior_sub``,
        # captured at entry above). A brand-new service — including a partner's
        # 2nd/3rd — gets its own 7-day trial; a change-plan / resume on a
        # service that already had one enters the active (non-trial) state.
        if plan.sh_trial and not entity_has_prior_sub:
            self._force_trial_dates_from_plan(subscription, plan)
        else:
            self._force_active_dates_from_plan(subscription, plan)

        # Confirm → in_progress (starts trial if configured)
        subscription.sh_subscription_confirm()

        self.write({'subscription_id': subscription.id})
        _logger.info(
            "[SUB_MIXIN] created subscription=%s name=%s plan=%s entity=%s:%s trial=%s",
            subscription.id, subscription.name, plan.name,
            self._name, self.id, subscription.sh_trial,
        )
        return subscription

    def _force_trial_dates_from_plan(self, subscription, plan):
        """Force trial fields and dates on ``subscription`` from ``plan``.

        Plan-driven explicit set; never trusts the chained onchange compute
        in ``_create_and_confirm_subscription`` to populate trial dates.
        Writes the full date triple — sh_trial_end_date, sh_start_date,
        sh_end_date — so downstream readers (UI trial countdown, refund
        compute) never see a partial state.
        """
        trial_delta = {
            'day': relativedelta(days=plan.sh_trial_duration),
            'week': relativedelta(weeks=plan.sh_trial_duration),
            'month': relativedelta(months=plan.sh_trial_duration),
            'year': relativedelta(years=plan.sh_trial_duration),
        }.get(plan.sh_trial_unit, relativedelta())
        cycle_delta = {
            'day': relativedelta(days=plan.sh_duration),
            'week': relativedelta(weeks=plan.sh_duration),
            'month': relativedelta(months=plan.sh_duration),
            'year': relativedelta(years=plan.sh_duration),
        }.get(plan.sh_unit, relativedelta())
        trial_end = date.today() + trial_delta - relativedelta(days=1)
        start_date = trial_end + relativedelta(days=1)
        end_date = start_date + cycle_delta - relativedelta(days=1)
        subscription.write({
            'sh_trial': True,
            'sh_trial_duration': plan.sh_trial_duration,
            'sh_trial_unit': plan.sh_trial_unit,
            'sh_trial_end_date': trial_end,
            'sh_start_date': start_date,
            'sh_end_date': end_date,
            'sh_date_of_next_payment': start_date,
        })
        _logger.info(
            "Forced trial for subscription %s: %s %s, billing %s → %s",
            subscription.name, plan.sh_trial_duration, plan.sh_trial_unit,
            start_date, end_date,
        )

    def _force_active_dates_from_plan(self, subscription, plan):
        """Force ``subscription`` into a coherent non-trial (active) state.

        Plan-driven (start_date=today, end_date=start+cycle_delta-1d) so
        downstream readers never see end < start; cycle_delta does NOT
        multiply by sh_no_of_billing_cycle (= -1 on every platform plan,
        which was the issue #39 root cause).

        ``sh_date_of_next_payment`` is seeded to ``start_date``;
        ``sh_subscription_confirm()`` advances it by one cycle to
        ``start + cycle_delta`` after creating the first invoice.

        #367: also clears ``sh_trial`` / ``sh_trial_end_date``. This helper is
        the active branch, reachable for a trial plan when the entity already
        holds a subscription (per-entity guard: a change-plan / resume on an
        existing service) — the onchange chain may have flipped
        ``sh_trial=True``, so the flag must be cleared to stay consistent with
        the active date triple.
        """
        cycle_delta = {
            'day': relativedelta(days=plan.sh_duration),
            'week': relativedelta(weeks=plan.sh_duration),
            'month': relativedelta(months=plan.sh_duration),
            'year': relativedelta(years=plan.sh_duration),
        }.get(plan.sh_unit, relativedelta())
        start_date = date.today()
        end_date = start_date + cycle_delta - relativedelta(days=1)
        subscription.write({
            # #367: the onchange chain may have left sh_trial=True; the active
            # path is non-trial by definition, so clear the flag and any
            # trial-end residue to keep sh_trial and the date triple consistent.
            'sh_trial': False,
            'sh_trial_end_date': False,
            'sh_start_date': start_date,
            'sh_end_date': end_date,
            'sh_date_of_next_payment': start_date,
        })
        _logger.info(
            "Forced active dates for subscription %s: billing %s → %s",
            subscription.name, start_date, end_date,
        )

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

    def _skip_trial_estimate(self):
        """Compute the upcoming-invoice breakdown for skip-trial.

        Side-effect-free counterpart to ``_skip_trial``: returns the same
        amount that the actual skip-trial would invoice, broken down into
        untaxed / tax / total, plus wallet sufficiency info. Used by the
        confirm modal to render the breakdown before the user commits.

        Mirrors the same three preconditions as ``_skip_trial`` so the
        modal can surface the same UserError messages on load.

        Returns:
            dict with keys:
                untaxed, tax_rate, tax_amount, total, currency,
                wallet_balance, sufficient, shortage,
                billing_cycle_start, billing_cycle_end
        Raises:
            UserError when no subscription / not active / not in trial.
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        if not sub:
            raise UserError("No subscription linked to this service.")
        if sub.state != 'in_progress':
            raise UserError("Subscription is not active.")
        if not sub.sh_trial:
            raise UserError("Subscription is not in trial period.")

        # Tax breakdown is computed by the shared helper so the dashboard's
        # next_bill_estimate uses the same rule as the skip-trial modal.
        untaxed, tax_rate, tax_amount, total = _subscription_total_with_tax(sub)

        today = fields.Date.today()
        delta = {
            'day': relativedelta(days=sub.sh_recurrency or 1),
            'week': relativedelta(weeks=sub.sh_recurrency or 1),
            'month': relativedelta(months=sub.sh_recurrency or 1),
            'year': relativedelta(years=sub.sh_recurrency or 1),
        }.get(sub.sh_unit, relativedelta(months=1))

        wallet_balance = sub.sh_partner_id.wallet_balance or 0.0
        return {
            'untaxed': float(untaxed),
            'tax_rate': float(tax_rate),
            'tax_amount': float(tax_amount),
            'total': float(total),
            'currency': 'pts',
            'wallet_balance': float(wallet_balance),
            'sufficient': wallet_balance >= total,
            'shortage': float(max(0.0, total - wallet_balance)),
            'billing_cycle_start': today.isoformat(),
            'billing_cycle_end': (today + delta).isoformat(),
        }

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

    def _is_halted(self):
        """這筆記錄是否處於「停權中」——lifecycle cron 決定要不要復權的判準。

        預設只認 ``state == 'suspended'``，與 SmartHome / SecurityAccess 自己
        ``_do_resume`` 的 guard 一致（對它們是零行為變化）。

        CloudService override 成 ``state == 'suspended' or
        bool(pre_suspend_state)``：它的 state 會被 upgrade 管線改寫，需要一個不受
        呼叫順序影響的事實源（#482）。cron 讀這個方法而不是直接比 state，是為了讓
        「停權中但 state 被改寫過」的服務仍走得到復權——否則租戶付了款也回不來
        （#497）。
        """
        self.ensure_one()
        return self.state == 'suspended'

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
            # #368 PR-3：ChangePlanModal 用 plan_id 定位目前方案（cycle×group×seats
            # 由前端 PlanSelector 反推）並擋「churn 成同方案」。三 surface（CS/SA/SH）
            # 皆以此 dict 為 subscription 資料源，故補在 mixin 一處即全數受惠。
            'plan_id': sub.sh_subscription_plan_id.id if sub.sh_subscription_plan_id else None,
            'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else None,
            'plan_price': sub.sh_plan_price,
            'is_trial': sub.sh_trial,
            'trial_end_date': sub.sh_trial_end_date.isoformat() if sub.sh_trial_end_date else None,
            'start_date': sub.sh_start_date.isoformat() if sub.sh_start_date else None,
            'end_date': sub.sh_end_date.isoformat() if sub.sh_end_date else None,
            'next_payment': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
            'last_payment_status': sub.sh_last_payment_status if hasattr(sub, 'sh_last_payment_status') else None,
        }
