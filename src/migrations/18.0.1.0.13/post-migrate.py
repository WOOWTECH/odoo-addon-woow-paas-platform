"""T19: back-fill dunning state on subscriptions that pre-date the feature.

The model gains three columns on this version. ORM defaults are set
when columns are created, but pre-existing rows on Postgres may end up
with NULL for the integer/boolean fields depending on how Odoo's column
add-or-alter ran. Force a deterministic state for old rows so the cron's
threshold logic doesn't misread NULL.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE sh_subscription_subscription
        SET woow_consecutive_payment_failures = COALESCE(
                woow_consecutive_payment_failures, 0
            ),
            woow_cancellation_notified = COALESCE(
                woow_cancellation_notified, false
            )
        WHERE woow_consecutive_payment_failures IS NULL
           OR woow_cancellation_notified IS NULL
        """
    )
    _logger.info(
        "T19 back-fill: %s subscription rows defaulted (failures=0, "
        "cancellation_notified=false)",
        cr.rowcount,
    )
