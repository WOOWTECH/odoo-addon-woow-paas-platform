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

Known sequence boundary (not fixed by this PR): the upstream static definition
of `seq_wallet_transaction` omits `implementation`, so its default is Odoo's
standard `ir.sequence`. Read-only prod/stg observations supplied for #977 match
that default: both use the global sequence id=19, `company_id=False`, prefix
`WT`, and `implementation='standard'` (prod `number_next_actual=1`, with about
zero wallet transactions; stg `number_next_actual=1589`). Standard
`next_by_code` is backed by PostgreSQL `nextval`, whose allocations do not roll
back with a transaction, savepoint rollback, or framework replay; known,
audit-visible numbering gaps can therefore remain. This differs from
`account.move`, whose transactional numbering comes from `sequence.mixin`.
Do not infer or test a gapless wallet transaction sequence.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_WALLET_CREDIT_METADATA_CTX = '_woow_wallet_credit_metadata_write'
_WALLET_CREDIT_METADATA_CAPABILITY = object()


def _wallet_credit_metadata(records):
    return records.with_context(**{
        _WALLET_CREDIT_METADATA_CTX: _WALLET_CREDIT_METADATA_CAPABILITY,
    })


def _is_wallet_credit_metadata(env):
    return (
        env.context.get(_WALLET_CREDIT_METADATA_CTX)
        is _WALLET_CREDIT_METADATA_CAPABILITY
    )


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
    woow_credit_origin = fields.Selection(
        [('normal', 'Normal credit'), ('auto_recredit', 'Auto recredit')],
        default='normal',
        readonly=True,
        index=True,
        groups='base.group_system',
    )
    woow_repair_incident_ref = fields.Integer(
        readonly=True,
        index=True,
        groups='base.group_system',
    )

    @api.model_create_multi
    def create(self, values_list):
        protected = {'woow_credit_origin', 'woow_repair_incident_ref'}
        if (not _is_wallet_credit_metadata(self.env)
                and any(protected & set(values) for values in values_list)):
            raise AccessError(_(
                'Wallet credit audit metadata is managed only by the '
                'internal credit process.'
            ))
        return super().create(values_list)

    def write(self, values):
        if ({'woow_credit_origin', 'woow_repair_incident_ref'} & set(values)
                and not _is_wallet_credit_metadata(self.env)):
            raise AccessError(_(
                'Wallet credit audit metadata is managed only by the '
                'internal credit process.'
            ))
        return super().write(values)

    def init(self):
        """先做 non-PII duplicate 診斷，再建立 PR-F DB backstop。"""
        parent_init = getattr(super(), 'init', None)
        if parent_init:
            parent_init()
        self.env.cr.execute(
            "SELECT COUNT(*) FROM (SELECT payment_transaction_id FROM wallet_transaction "
            "WHERE transaction_type = 'topup' AND payment_transaction_id IS NOT NULL "
            "GROUP BY payment_transaction_id HAVING COUNT(*) > 1) groups")
        count = self.env.cr.fetchone()[0]
        if count:
            self.env.cr.execute(
                "SELECT ARRAY_AGG(id ORDER BY id) FROM wallet_transaction WHERE "
                "transaction_type = 'topup' AND "
                "payment_transaction_id = (SELECT payment_transaction_id FROM wallet_transaction "
                "WHERE transaction_type = 'topup' AND payment_transaction_id IS NOT NULL "
                "GROUP BY payment_transaction_id HAVING COUNT(*) > 1 ORDER BY payment_transaction_id LIMIT 1)")
            raise UserError(_(
                'PR-F cannot create wallet_transaction_topup_payment_tx_uniq: '
                '%(count)s duplicate key group(s); first transaction ids=%(ids)s. '
                'Resolve data manually; automatic deduplication is forbidden.'
            ) % {'count': count, 'ids': self.env.cr.fetchone()[0]})
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS wallet_transaction_topup_payment_tx_uniq
            ON wallet_transaction (payment_transaction_id)
            WHERE transaction_type = 'topup' AND payment_transaction_id IS NOT NULL
        """)
