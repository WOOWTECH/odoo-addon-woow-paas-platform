"""Local extension fields on sh.subscription.subscription.

Holds platform-specific state that the upstream model has no place for:
- low-balance email dedup (`woow_last_low_balance_warned_for`)
- T19 dunning bookkeeping (`woow_consecutive_payment_failures`,
  `woow_last_payment_failure_at`, `woow_cancellation_notified`)
"""
from odoo import fields, models


class ShSubscriptionExtensionFields(models.Model):
    _inherit = 'sh.subscription.subscription'

    woow_last_low_balance_warned_for = fields.Date(
        string='Last Low-Balance Warning Renewal Date',
        help='Date of the upcoming renewal for which a low-balance email was '
             'last sent. Prevents duplicate warnings per renewal cycle.',
        copy=False,
    )

    # --- T19 dunning state ------------------------------------------------ #

    woow_consecutive_payment_failures = fields.Integer(
        string='Consecutive Payment Failures',
        default=0,
        copy=False,
        help='Number of consecutive auto-invoice cron ticks where neither '
             'wallet nor auto-topup could settle the outstanding invoice. '
             'Reset to 0 on successful payment. Once it reaches the configured '
             'threshold (woow_paas_platform.max_consecutive_payment_failures), '
             'the subscription is auto-cancelled.',
    )
    woow_last_payment_failure_at = fields.Datetime(
        string='Last Payment Failure At',
        copy=False,
        help='Timestamp of the most recent failed billing attempt.',
    )
    woow_cancellation_notified = fields.Boolean(
        string='Cancellation Email Sent',
        default=False,
        copy=False,
        help='Set when the auto-cancellation email has been queued, so a '
             'state-transition retrigger does not double-send.',
    )
