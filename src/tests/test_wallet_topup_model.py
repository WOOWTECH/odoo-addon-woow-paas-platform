from odoo.tests.common import TransactionCase


class TestWalletTopupModel(TransactionCase):
    def test_model_exists_and_has_required_fields(self):
        model = self.env['woow.wallet.topup']
        fields = model._fields
        for name in (
            'partner_id', 'plan_id', 'amount', 'credits', 'bonus',
            'currency_id', 'state', 'payment_transaction_id',
            'wallet_transaction_id', 'failure_code', 'failure_message',
            'invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
            'invoice_title', 'invoice_lovecode',
            'invoice_account_move_id', 'invoice_issued_at',
        ):
            self.assertIn(name, fields, f"missing field {name}")

    def test_state_selection_values(self):
        model = self.env['woow.wallet.topup']
        selection = dict(model._fields['state'].selection)
        self.assertEqual(
            set(selection.keys()),
            {'draft', 'pending', 'done', 'failed', 'cancelled'},
        )
