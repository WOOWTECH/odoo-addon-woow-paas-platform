"""Local extension fields on sh.subscription.subscription.

Holds platform-specific state that the upstream model has no place for:
- low-balance email dedup (`woow_last_low_balance_warned_for` +
  `woow_last_low_balance_mail_id`)
- T19 dunning bookkeeping (`woow_consecutive_payment_failures`,
  `woow_last_payment_failure_at`, `woow_cancellation_notified`)
"""
from odoo import fields, models


class ShSubscriptionExtensionFields(models.Model):
    _inherit = 'sh.subscription.subscription'

    woow_last_low_balance_warned_for = fields.Date(
        string='Last Low-Balance Warning Renewal Date',
        help='Renewal date (sh_date_of_next_payment) this cycle\'s low-balance '
             'warning belongs to. Written by BOTH the D-3 pre-renewal cron '
             '(low_balance_warning_cron) and the dunning failure path '
             '(_on_payment_failure) — they share the same key, so the '
             'send/skip decision lives in one place '
             '(_low_balance_warning_needs_send). Only stamped when the mail '
             'actually reached the queue, and never read on its own: the '
             'carrier below decides whether the cycle still needs a retry.',
        copy=False,
    )
    woow_last_low_balance_mail_id = fields.Many2one(
        'mail.mail',
        string='Last Low-Balance Mail',
        ondelete='set null',
        copy=False,
        help='The queued low-balance notification for this cycle. The date '
             'marker alone cannot tell "queued" from "delivered": '
             'force_send=False only enqueues, and a mail that fails delivery '
             'parks at state=exception which Odoo\'s scheduler never retries '
             '(process_email_queue only picks up outgoing). Keeping the '
             'carrier lets the next cron tick re-send instead of going silent '
             'for the whole renewal cycle (#389). state=cancel is an explicit '
             '"do not send", not a transient failure, so it is not retried. '
             'Retries are bounded by _LOW_BALANCE_RETRY_WINDOW_DAYS rather '
             'than by the dunning threshold (which may legitimately be 0). '
             'The mail is created with auto_delete=False so the carrier '
             'survives — Odoo unlinks auto_delete mails not only on success '
             'but also on mail_email_invalid / mail_email_missing. Empty '
             'means legacy data (pre-.89) or a manually purged mail; both are '
             'conservatively treated as delivered so upgrading does not '
             're-notify every overdue tenant at once.',
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

    # --- #1424 billing hold ------------------------------------------------ #

    woow_billing_hold_notified_at = fields.Datetime(
        string='Billing Hold Notice Sent At',
        copy=False,
        readonly=True,
        help='#1424: when the tenant was told (once) that this subscription is not '
             'charged because its cloud service never worked (it has stayed in error '
             'since it was created). The auto-invoice cron skips such subscriptions; '
             'billing resumes once the service reaches running. Stamped only after the '
             'notice reached the mail queue.',
    )
