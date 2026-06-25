"""G2-B: backfill base_amount and bonus_amount on existing top-up rows.

The new fields default to 0. For top-ups recorded before this migration,
join wallet.transaction.id <- woow_wallet_topup.wallet_transaction_id to
recover the original credits + bonus values from the topup record.

The WHERE clause requires BOTH `base_amount` and `bonus_amount` to be NULL
or 0 for the row to qualify, making the migration idempotent: re-running
`-u` after the first execution updates 0 rows because every previously
backfilled row now has at least `base_amount > 0`. A row whose `base_amount`
was manually set to non-zero in a dev DB is also skipped to avoid clobbering
hand-edited data.

Top-ups that never linked back to a wallet.transaction (e.g. a payment
whose callback skipped `_on_payment_done`) keep `base_amount=0`,
`bonus_amount=0`, and the frontend falls back to the unchanged description.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE wallet_transaction wt
        SET base_amount  = topup.credits,
            bonus_amount = topup.bonus
        FROM woow_wallet_topup topup
        WHERE topup.wallet_transaction_id = wt.id
          AND wt.transaction_type = 'topup'
          AND (wt.base_amount IS NULL OR wt.base_amount = 0)
          AND (wt.bonus_amount IS NULL OR wt.bonus_amount = 0)
        """
    )
    _logger.info(
        "G2-B migration: backfilled base_amount/bonus_amount on %s top-up rows",
        cr.rowcount,
    )
