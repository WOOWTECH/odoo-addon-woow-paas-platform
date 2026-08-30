"""G2-B: extend `wallet.transaction` with structured top-up bonus metadata.

The upstream `sh_subscription_wallet.wallet.transaction` model exposes a single
`amount` column for credit additions, which collapses base credits + bonus into
one number. The frontend transaction list / credit history modal therefore
can't show users the bonus they earned.

Adding `base_amount` + `bonus_amount` as Float fields lets the API surface the
breakdown without forcing a refactor of the upstream model. These fields are
written by `woow.wallet.topup._on_payment_done` for new top-ups and backfilled
by the `18.0.1.0.17` migration for existing rows.

Both fields are 0 on rows that don't represent a top-up (e.g. subscription
debits, refunds), and on legacy top-up rows whose `wallet_transaction_id`
was never linked back to the `woow_wallet_topup` record. The frontend handles
the fallback by treating `bonus_amount <= 0` as "render the description as-is".

Known sequence boundary: on a fresh install, the upstream
`seq_wallet_transaction` omits `implementation`, so it uses Odoo's standard
`ir.sequence` implementation (`next_by_code` backed by PostgreSQL `nextval`).
That counter is non-transactional: rollback, savepoint rollback, or framework
replay may leave gaps in wallet transaction numbers. This differs from
`account.move`, whose transactional numbering comes from `sequence.mixin`.
Do not infer or test a gapless wallet transaction sequence from rollback.
"""
from odoo import fields, models


class WalletTransaction(models.Model):
    _inherit = 'wallet.transaction'

    base_amount = fields.Float(
        string='Base Amount',
        help=(
            "For top-up rows: the base credits before bonus. "
            "Always 0 for non-topup transaction types and for legacy "
            "rows that predate this field."
        ),
    )
    bonus_amount = fields.Float(
        string='Bonus Amount',
        help=(
            "For top-up rows: the bonus credits added on top of base. "
            "Always 0 when no bonus applies or for non-topup types."
        ),
    )
