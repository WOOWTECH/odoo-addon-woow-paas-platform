from odoo.tests.common import TransactionCase


class TestPaymentTransactionFields(TransactionCase):
    def test_woow_topup_id_field_registered(self):
        fields = self.env['payment.transaction']._fields
        self.assertIn('woow_topup_id', fields)
        self.assertEqual(fields['woow_topup_id'].comodel_name, 'woow.wallet.topup')
